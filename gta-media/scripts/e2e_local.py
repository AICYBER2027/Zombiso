"""VicePulse local end-to-end test, run INSIDE the VicePulse worker container.

    docker compose cp scripts/e2e_local.py vicepulse:/tmp/e2e_local.py
    docker compose exec -T vicepulse python /tmp/e2e_local.py

What it does (no publishing, no paid APIs; it refuses to run otherwise):
  1. safety and health checks (mock Claude, live publishing off, database reachable, worker answers)
  2. generates a 24-second synthetic "gameplay" video with FFmpeg (test patterns + tone; nothing downloaded)
  3. registers it as verified own test footage in the rights ledger
  4. runs the pipeline: ingest -> signals -> daily (selection, EN + pt-BR scripts and captions, 9:16 render,
     final check) -> approval queue
  5. checks every rendered file: 1080x1920 H.264 + AAC, full decode with no errors, burned captions in the
     right language, preview frames
  6. copies the finished files to <data>/e2e/<run>/ and writes report.json there

Only rows for this test are added. Earlier e2e test clips still waiting for approval are rejected (they are
test data) so they do not block a rerun as duplicates. Nothing is deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DATA = Path(os.environ.get("VICEPULSE_DATA_DIR", "/data"))
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
OUT = DATA / "e2e" / STAMP
FOOTAGE = DATA / "incoming" / f"vp-e2e-{STAMP}.mp4"
REVIEWER = "e2e-test (synthetic footage)"
os.environ.setdefault("VICEPULSE_X264_PRESET", "veryfast")   # quicker renders for the test

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return bool(ok)


def vp(*args: str) -> dict:
    """Run a vicepulse CLI command and return its JSON output."""
    p = subprocess.run(["vicepulse", *args], capture_output=True, text=True)
    out = (p.stdout or p.stderr).strip().splitlines()
    try:
        data = json.loads(out[-1]) if out else {}
    except json.JSONDecodeError:
        data = {"ok": False, "error": (p.stdout + p.stderr)[-800:]}
    if p.returncode != 0 or not data.get("ok"):
        raise RuntimeError(f"vicepulse {' '.join(args)} failed: {data.get('error') or p.stderr[-800:]}")
    return data


def ffprobe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


def finish(code: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"run": STAMP, "passed": all(ok for _, ok, _ in results) and code == 0,
              "checks": [{"check": n, "ok": ok, "detail": d} for n, ok, d in results]}
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (DATA / "e2e" / "LATEST").write_text(STAMP)
    failed = [n for n, ok, _ in results if not ok]
    print("\n==== RESULT: " + ("ALL CHECKS PASSED" if report["passed"] else f"FAILED ({len(failed)}): " + "; ".join(failed)))
    print(f"output folder in container: {OUT}")
    sys.exit(0 if report["passed"] else 1)


def main() -> None:
    # ---------------------------------------------------------------- 1. safety + health
    print("== 1. safety and health checks")
    llm = os.environ.get("LLM_MODE", "mock")
    live = os.environ.get("VICEPULSE_ALLOW_LIVE_PUBLISH", "0")
    if not check("Claude is in mock mode (LLM_MODE=mock)", llm == "mock", f"LLM_MODE={llm}"):
        print("Refusing to run: this test must not call paid APIs."); finish(2)
    if not check("live publishing is off", live != "1", f"VICEPULSE_ALLOW_LIVE_PUBLISH={live}"):
        print("Refusing to run: live publishing must be off."); finish(2)
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    tables = conn.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'").fetchone()["n"]
    if not check("VicePulse database reachable and migrated", tables >= 13, f"{tables} tables"):
        finish(2)
    enabled = conn.execute("SELECT count(*) AS n FROM platform_accounts WHERE enabled").fetchone()["n"]
    check("all platform accounts disabled", enabled == 0, f"{enabled} enabled")
    token = os.environ.get("VICEPULSE_WORKER_TOKEN", "")
    try:
        req = urllib.request.Request("http://127.0.0.1:8088/run", data=json.dumps({"args": ["health"]}).encode(),
                                     headers={"Authorization": f"Bearer {token}"})
        h = json.loads(urllib.request.urlopen(req, timeout=60).read())
        check("worker HTTP API answers with the token", h.get("ok") is True and h.get("live_publish") is False
              and h.get("llm_mode") == "mock", f"live_publish={h.get('live_publish')}, llm_mode={h.get('llm_mode')}")
    except Exception as e:  # the pipeline itself can still be tested through the CLI
        check("worker HTTP API answers with the token", False, str(e))
    for tool in ("ffmpeg", "ffprobe"):
        check(f"{tool} available", shutil.which(tool) is not None)

    # leftovers from earlier e2e runs: reject their pending clips, retire unused candidates (test data only)
    old = conn.execute("""SELECT cl.id FROM clips cl JOIN candidates c ON c.id = cl.candidate_id
                          JOIN sources s ON s.id = c.source_id
                          WHERE s.file_path LIKE %s AND cl.status = 'pending_approval'""",
                       (str(DATA / "incoming" / "vp-e2e-%"),)).fetchall()
    for r in old:
        vp("decide", "--clip", str(r["id"]), "--decision", "reject", "--by", REVIEWER,
           "--reason", "superseded by a newer e2e test run")
    retired = conn.execute("""UPDATE candidates SET status = 'rejected', status_reason = 'superseded by a newer e2e test run'
                              WHERE status IN ('new', 'triaged') AND source_id IN
                                (SELECT id FROM sources WHERE file_path LIKE %s) RETURNING id""",
                           (str(DATA / "incoming" / "vp-e2e-%"),)).fetchall()
    if old or retired:
        print(f"   earlier e2e test data: rejected {len(old)} pending clips, retired {len(retired)} unused candidates")

    # ---------------------------------------------------------------- 2. synthetic footage
    print("== 2. generate synthetic test footage")
    FOOTAGE.parent.mkdir(parents=True, exist_ok=True)
    pats = ["testsrc2", "smptebars", "mandelbrot", "rgbtestsrc"]
    seg = 6
    args = ["ffmpeg", "-v", "error", "-y"]
    for p in pats:
        args += ["-f", "lavfi", "-t", str(seg), "-i", f"{p}=size=1280x720:rate=30"]
    args += ["-f", "lavfi", "-t", str(seg * len(pats)), "-i", "sine=frequency=440:sample_rate=48000"]
    bursts = "+".join(f"between(t,{i * seg + 2},{i * seg + 4})" for i in range(len(pats)))
    label = f"VicePulse E2E {STAMP}"
    graph = ("".join(f"[{i}:v]" for i in range(len(pats))) + f"concat=n={len(pats)}:v=1:a=0,"
             f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='{label}':x=20:y=20:"
             f"fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5,format=yuv420p[v];"
             f"[{len(pats)}:a]volume='if({bursts},1,0.03)':eval=frame[a]")
    subprocess.run(args + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                           "-preset", "veryfast", "-crf", "23", "-c:a", "aac", str(FOOTAGE)], check=True)
    info = ffprobe(FOOTAGE)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    check("synthetic 16:9 test video generated", v["width"] == 1280 and v["height"] == 720,
          f"{FOOTAGE.name}, {float(info['format']['duration']):.1f}s, {v['width']}x{v['height']}")

    # ---------------------------------------------------------------- 3. rights
    print("== 3. register as authorized test footage")
    today = datetime.now(timezone.utc).date().isoformat()
    rid = vp("rights-add", "--json", json.dumps({
        "owner_name": "VicePulse E2E test (synthetic FFmpeg footage)", "licence_type": "own",
        "platforms": ["youtube", "instagram", "tiktok"], "starts_on": today, "monetisation_allowed": True,
        "derivative_edits_allowed": True, "notes": f"synthetic test footage generated by e2e_local.py, run {STAMP}"}))["rights_id"]
    ver = vp("rights-verify", "--id", str(rid), "--by", REVIEWER)
    check("rights ledger entry added and verified", ver["verified"], f"rights_id={rid}")

    # ---------------------------------------------------------------- 4. pipeline
    print("== 4. run the pipeline (mock Claude)")
    ing = vp("ingest", "--file", str(FOOTAGE), "--origin", "own_recording", "--rights-id", str(rid))
    sid = ing["source_id"]
    if not check("ingest + rights gate: cleared", ing["status"] == "cleared", f"source_id={sid}, status={ing['status']}"):
        finish(1)
    sig = vp("signals", "--source-id", str(sid))["sources"][0]
    check("signal extraction found scenes, loudness peaks and candidates",
          sig["candidates"] > 0 and sig["scene_cuts"] > 0 and sig["peaks"] > 0,
          f"{sig['scene_cuts']} scene cuts, {sig['peaks']} peaks, {sig['candidates']} candidates")
    print("   rendering (EN + BR, 3 platform files each) - this can take a few minutes ...", flush=True)
    daily = vp("daily", "--date", today)
    sel = {c["channel"]: c for c in daily["select"]["channels"]}
    clips = conn.execute("""SELECT cl.*, ch.key FROM clips cl JOIN channels ch ON ch.id = cl.channel_id
                            JOIN candidates c ON c.id = cl.candidate_id WHERE c.source_id = %s ORDER BY ch.id""",
                         (sid,)).fetchall()
    by_ch = {c["key"]: c for c in clips}
    check("highlight selected for VicePulse EN", "en" in by_ch, f"selected={sel.get('en', {}).get('selected')}")
    check("highlight selected for VicePulse BR", "br" in by_ch, f"selected={sel.get('br', {}).get('selected')}")
    for key, lang, closer in (("en", "en", "Follow for more."), ("br", "pt-BR", "Segue pra mais.")):
        c = by_ch.get(key)
        if not c:
            continue
        check(f"{key.upper()} script written in {lang}", c["language"] == lang and c["script"]
              and c["script"].get("closer") == closer, f"hook: {c['script']['hook'] if c['script'] else '-'}")
        check(f"{key.upper()} clip is in the approval queue", c["status"] == "pending_approval",
              f"status={c['status']} {c['status_reason'] or ''}".strip())
    queue = vp("queue")["pending"]
    check("approval queue lists the new clips", all(any(q["id"] == c["id"] for q in queue) for c in clips) and clips,
          f"{len(queue)} waiting in total")

    # ---------------------------------------------------------------- 5. verify output files
    print("== 5. verify the rendered files")
    OUT.mkdir(parents=True, exist_ok=True)
    for c in clips:
        rows = conn.execute("SELECT * FROM renders WHERE clip_id = %s ORDER BY platform", (c["id"],)).fetchall()
        check(f"{c['key'].upper()}: one render per platform", len(rows) == 3, ", ".join(r["platform"] for r in rows))
        for r in rows:
            src = Path(r["file_path"])
            tag = f"{c['key'].upper()} {r['platform']}"
            if not check(f"{tag}: file exists", src.is_file(), str(src)):
                continue
            pi = ffprobe(src)
            vs = next((s for s in pi["streams"] if s["codec_type"] == "video"), {})
            au = next((s for s in pi["streams"] if s["codec_type"] == "audio"), {})
            dur = float(pi["format"].get("duration", 0))
            check(f"{tag}: 1080x1920 H.264 with AAC audio",
                  (vs.get("width"), vs.get("height"), vs.get("codec_name"), au.get("codec_name")) == (1080, 1920, "h264", "aac"),
                  f"{vs.get('width')}x{vs.get('height')} {vs.get('codec_name')} / {au.get('codec_name')}, {dur:.1f}s")
            check(f"{tag}: length matches the clip", abs(dur - (c["out_s"] - c["in_s"])) < 0.6,
                  f"{dur:.1f}s vs {c['out_s'] - c['in_s']:.1f}s")
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-f", "null", "-"], capture_output=True, text=True)
            check(f"{tag}: plays (full decode, no errors)", dec.returncode == 0 and not dec.stderr.strip(),
                  dec.stderr.strip()[:200] or "clean")
            ass = src.with_suffix(".ass")
            text = ass.read_text(encoding="utf-8") if ass.is_file() else ""
            check(f"{tag}: burned captions contain the {c['language']} script",
                  c["script"]["hook"].split()[0] in text and c["script"]["closer"].split()[0] in text, ass.name)
            nice = OUT / f"VicePulse-{c['key'].upper()}-{r['platform']}.mp4"
            shutil.copyfile(src, nice)
            if r["platform"] == "youtube":
                for t in (0.8, max(1.0, dur - 1.2)):
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                                    "-vf", "scale=540:-2", str(OUT / f"preview-{c['key'].upper()}-{t:04.1f}s.png")], check=True)
    jobs = conn.execute("SELECT count(*) AS n FROM publish_jobs pj JOIN clips cl ON cl.id = pj.clip_id "
                        "JOIN candidates c ON c.id = cl.candidate_id WHERE c.source_id = %s", (sid,)).fetchone()["n"]
    check("nothing was scheduled or published", jobs == 0, f"{jobs} publish jobs")
    finish(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        check("pipeline ran without errors", False, f"{type(e).__name__}: {e}")
        finish(1)
