"""End-to-end dry run on synthetic footage, with Claude and the platforms mocked.

Proves, against a real (temporary) Postgres and real FFmpeg renders:
- a licensed synthetic clip goes from ingest to 1080x1920 files with burned captions, for EN and BR;
- unlicensed footage is quarantined and cannot be analysed, rendered or published;
- a repeat (same moment, same visuals from another file, same script) is blocked;
- nothing is sent to any platform unless live publishing is explicitly switched on.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from PIL import Image, ImageChops

from conftest import make_footage, verified_rights
from vicepulse import commentary, dedupe, ingest, media, policy, publish, render, selection, signals
from vicepulse.llm import MockLLM
from vicepulse.platforms import PlatformError, PublishResult

DAY = date(2026, 11, 20)
TT = {"privacy_level": "SELF_ONLY", "allow_comment": True, "allow_duet": False, "allow_stitch": False,
      "consent_confirmed": True}


class FakePublisher:
    def __init__(self, platform, log):
        self.platform, self.log = platform, log

    def publish(self, req):
        self.log.append(("publish", self.platform, req.file_path.name))
        return PublishResult(f"{self.platform}-{req.file_path.stem}", f"https://example.invalid/{self.platform}")

    def metrics(self, external_id):
        self.log.append(("metrics", self.platform, external_id))
        return {"views": 100, "likes": 5, "comments": 1, "shares": 2, "raw": {}}

    def take_down(self, external_id):
        self.log.append(("take_down", self.platform, external_id))
        return self.platform == "youtube"


def fake_factory(log):
    return lambda platform, channel_key, audited=False: FakePublisher(platform, log)


def run_to_approval_queue(conn, settings, llm, src_id, day=DAY):
    signals.extract(conn, settings, src_id, window_s=8)
    selection.triage_new(conn, settings, llm)
    picked = {ch: selection.select_for_channel(conn, settings, llm, ch, day) for ch in ("en", "br")}
    commentary.write_scripts(conn, settings, llm)
    rendered = render.render_pending(conn, settings)
    checked = policy.final_check(conn, settings, llm)
    return picked, rendered, checked


def region_diff(a_path, b_path, t, tmp_path):
    """Mean absolute pixel difference in the caption band between two videos at time t."""
    fa = media.extract_frame(a_path, t, tmp_path / "a.png", width=540)
    fb = media.extract_frame(b_path, t, tmp_path / "b.png", width=540)
    a, b = Image.open(fa).convert("L"), Image.open(fb).convert("L")
    box = (0, int(a.height * 0.55), a.width, int(a.height * 0.85))
    px = list(ImageChops.difference(a.crop(box), b.crop(box)).get_flattened_data())
    return sum(px) / len(px)


def test_full_dry_run_en_and_br(conn, settings, footage, tmp_path):
    llm = MockLLM()
    result = ingest.ingest_file(conn, settings, footage, origin="own_recording", rights_id=verified_rights(conn))
    assert result["status"] == "cleared"
    picked, rendered, checked = run_to_approval_queue(conn, settings, llm, result["source_id"])

    # --- two clips per channel were selected, scripted in the channel's language, and rendered
    assert len(picked["en"]["selected"]) == 2 and len(picked["br"]["selected"]) == 2
    clips = conn.execute("SELECT cl.*, ch.key FROM clips cl JOIN channels ch ON ch.id = cl.channel_id "
                         "WHERE cl.status = 'pending_approval' ORDER BY cl.id").fetchall()
    assert len(clips) == 4 and sorted(checked["pending_approval"]) == [c["id"] for c in clips]
    assert {c["language"] for c in clips} == {"en", "pt-BR"}
    br = next(c for c in clips if c["key"] == "br")
    assert br["script"]["language"] == "pt-BR" and br["script"]["closer"] == "Segue pra mais."
    assert set(br["titles"]) == {"youtube", "instagram", "tiktok"}

    # --- every render is 1080x1920 H.264 with audio, one file per platform
    renders = conn.execute("SELECT * FROM renders ORDER BY id").fetchall()
    assert len(renders) == 12 and len(rendered["files"]) == 12
    for r in renders:
        info = media.probe(r["file_path"])
        assert (info["width"], info["height"]) == (1080, 1920)
        assert info["video_codec"] == "h264" and info["has_audio"]
        assert 7.5 <= info["duration"] <= 8.5 and r["has_captions"] and not r["has_voice"]

    # --- captions are really burned in: same clip rendered with no captions differs in the caption band
    clip = clips[0]
    r0 = next(r for r in renders if r["clip_id"] == clip["id"] and r["platform"] == "youtube")
    src = conn.execute("SELECT s.* FROM sources s JOIN candidates c ON c.source_id = s.id WHERE c.id = %s",
                       (clip["candidate_id"],)).fetchone()
    info = media.probe(src["file_path"])
    plain = tmp_path / "plain.mp4"
    with_caps = tmp_path / "caps.mp4"
    common = dict(in_s=clip["in_s"], out_s=clip["out_s"], platform="youtube", voice=None, layout="blur",
                  centre=0.5, preset="ultrafast")
    render.render_clip_platform(src["file_path"], info, cues=[], out_path=plain, **common)
    render.render_clip_platform(src["file_path"], info, cues=clip["script"]["captions"], out_path=with_caps, **common)
    assert region_diff(plain, with_caps, 1.0, tmp_path) > 2.0
    assert clip["script"]["hook"].split()[0] in open(r0["file_path"].replace(".mp4", ".ass"), encoding="utf-8").read()

    # --- nothing can be scheduled before a person approves
    assert publish.schedule_approved(conn, settings)["scheduled_jobs"] == 0
    with pytest.raises(ValueError):
        policy.decide(conn, clip["id"], approve=True, by="")
    for c in clips:
        policy.decide(conn, c["id"], approve=True, by="owner", tiktok_options=TT)
    sched = publish.schedule_approved(conn, settings)
    assert sched["scheduled_jobs"] == 12
    assert publish.schedule_approved(conn, settings)["scheduled_jobs"] == 0          # re-run adds nothing
    slots = conn.execute("""SELECT count(DISTINCT scheduled_for) AS n FROM publish_jobs p JOIN clips c ON c.id = p.clip_id
                            WHERE c.channel_id = %s""", (clips[0]["channel_id"],)).fetchone()["n"]
    assert slots == 2                                                                 # one slot per clip

    # --- the unique (clip, platform) key refuses a second job for the same post
    job = conn.execute("SELECT * FROM publish_jobs LIMIT 1").fetchone()
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("""INSERT INTO publish_jobs (clip_id, platform, account_id, render_id, scheduled_for)
                        VALUES (%s, %s, %s, %s, now())""", (job["clip_id"], job["platform"], job["account_id"], job["render_id"]))
    conn.rollback()

    # --- with live publishing off, due jobs become dry runs and no platform client is even built
    calls = []
    later = datetime(2026, 11, 22, tzinfo=timezone.utc)
    out = publish.run_due(conn, settings, now=later, publisher_factory=fake_factory(calls))
    assert len(out["dry_run"]) == 12 and not out["published"] and calls == []
    assert publish.run_due(conn, settings, now=later, publisher_factory=fake_factory(calls))["dry_run"] == []
    assert publish.pull_analytics(conn, settings, publisher_factory=fake_factory(calls))["pulled"] == 0 and calls == []
    assert not any(name in ("anthropic",) for name, _ in llm.calls)
    assert {name for name, _ in llm.calls} == {"triage", "select", "script", "final_check"}


def test_unlicensed_footage_is_blocked_everywhere(conn, settings, other_footage, footage):
    # 1. no ledger entry at all
    r = ingest.ingest_file(conn, settings, other_footage, origin="creator_upload", rights_id=None)
    assert r["status"] == "quarantined" and "no rights ledger entry" in r["reasons"]
    with pytest.raises(PermissionError):
        signals.extract(conn, settings, r["source_id"])
    assert conn.execute("SELECT count(*) AS n FROM candidates").fetchone()["n"] == 0
    assert selection.triage_new(conn, settings, MockLLM()) == {"triaged": 0, "blocked": 0}
    assert render.render_pending(conn, settings)["rendered"] == []
    assert any("quarantined" in a for a in publish.health(conn, settings)["alerts"])

    # 2. a licence that nobody has verified yet is not enough; verifying it clears the source
    rid = ingest.add_rights(conn, owner_name="Some Creator", licence_type="written_licence",
                            platforms=["youtube", "instagram", "tiktok"], starts_on="2026-01-01",
                            expires_on="2027-01-01", monetisation_allowed=True, derivative_edits_allowed=True,
                            document_url="https://example.invalid/licence.pdf")
    r2 = ingest.ingest_file(conn, settings, footage, origin="creator_upload", rights_id=rid)
    assert r2["status"] == "quarantined" and any("not verified" in x for x in r2["reasons"])
    conn.execute("UPDATE rights_ledger SET verified_by = 'owner', verified_at = now() WHERE id = %s", (rid,))
    conn.commit()
    rechecked = {x["source_id"]: x["ok"] for x in ingest.recheck_quarantine(conn)}
    assert rechecked == {r["source_id"]: False, r2["source_id"]: True}

    # 3. an own-footage ledger entry cannot be used to wave through someone else's upload
    own = verified_rights(conn)
    conn.execute("UPDATE sources SET rights_id = %s WHERE id = %s", (own, r["source_id"]))
    conn.commit()
    assert not ingest.recheck_quarantine(conn)[0]["ok"]

    # 4. the same file cannot be ingested twice
    assert ingest.ingest_file(conn, settings, footage, origin="own_recording", rights_id=own)["duplicate_file"]


def test_repeats_are_blocked(conn, settings, footage, tmp_path):
    llm = MockLLM()
    rid = verified_rights(conn)
    src = ingest.ingest_file(conn, settings, footage, origin="own_recording", rights_id=rid)["source_id"]
    signals.extract(conn, settings, src, window_s=8)
    selection.triage_new(conn, settings, llm)
    first = selection.select_for_channel(conn, settings, llm, "en", DAY)
    assert len(first["selected"]) == 2

    # a) the same moment cut again from the same source (overlapping in/out points)
    clip = conn.execute("SELECT * FROM clips WHERE id = %s", (first["selected"][0],)).fetchone()
    other = conn.execute("""SELECT id FROM candidates WHERE status = 'triaged' AND id NOT IN
                            (SELECT candidate_id FROM clips) LIMIT 1""").fetchone()["id"]
    conn.execute("UPDATE candidates SET start_s = %s, end_s = %s WHERE id = %s", (clip["in_s"] + 1, clip["out_s"] + 1, other))
    conn.commit()

    again = selection.select_for_channel(conn, settings, llm, "en", DAY + timedelta(days=1))
    filtered = {f["candidate_id"]: f["reasons"] for f in again["filtered"]}
    assert other in filtered and "overlaps clip" in filtered[other][0]       # never even offered to the model
    assert other not in llm.calls[-1][1]["ids"]
    direct = dedupe.check_footage(conn, channel_id=clip["channel_id"], candidate_id=other,
                                  in_s=clip["in_s"] + 1, out_s=clip["out_s"] + 1)
    assert direct.duplicate                                                  # the same check guards the model's picks

    # b) the same visuals arriving as a different file (re-encoded copy, e.g. from another creator)
    copy = tmp_path / "reencoded_copy.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(footage), "-vf", "scale=480:270", "-crf", "30",
                    "-preset", "ultrafast", "-c:a", "aac", str(copy)], check=True)
    src2 = ingest.ingest_file(conn, settings, copy, origin="own_recording", rights_id=rid)
    assert src2["status"] == "cleared" and not src2["duplicate_file"]
    conn.execute("UPDATE candidates SET status = 'rejected' WHERE status = 'triaged'")   # only the copy's windows remain
    conn.commit()
    signals.extract(conn, settings, src2["source_id"], window_s=8)
    selection.triage_new(conn, settings, llm)
    used = {r["in_s"] for r in conn.execute("SELECT in_s FROM clips WHERE status = 'selected'").fetchall()}
    day3 = selection.select_for_channel(conn, settings, llm, "en", DAY + timedelta(days=2))
    reasons = [r for f in day3["filtered"] for r in f["reasons"]]
    assert any("perceptual hash" in r for r in reasons), reasons
    kept = {r["in_s"] for r in conn.execute("SELECT in_s FROM clips WHERE id = ANY(%s)", (day3["selected"],)).fetchall()}
    assert not kept & used                                                   # the already-used moments were not reused

    # c) the same script / angle written twice
    class SameScript(MockLLM):
        def script(self, channel, clip, frames, *, game):
            out = super().script(channel, clip, frames, game=game)
            out.update(hook="Nobody expected this to work.", body="Same take as always.", closer="Follow for more.")
            out["youtube"]["title"] = "Nobody expected this to work."
            return out

    res = commentary.write_scripts(conn, settings, SameScript(), clip_ids=first["selected"])
    assert len(res["scripted"]) == 1 and len(res["duplicate"]) == 1
    dup = conn.execute("SELECT status, status_reason FROM clips WHERE id = %s", (res["duplicate"][0],)).fetchone()
    assert dup["status"] == "duplicate" and "similar to clip" in dup["status_reason"]


def test_policy_flags_stop_clips(conn, settings, footage):
    src = ingest.ingest_file(conn, settings, footage, origin="own_recording", rights_id=verified_rights(conn))["source_id"]
    signals.extract(conn, settings, src, window_s=8)
    cands = [r["id"] for r in conn.execute("SELECT id FROM candidates ORDER BY id").fetchall()]
    llm = MockLLM(flag_candidates={cands[0]: ["story_spoiler"], cands[1]: ["graphic_violence_focus"]})
    counts = selection.triage_new(conn, settings, llm)
    assert counts["blocked"] == 1                       # spoiler is a blocked tag; the other tag is only recorded
    assert conn.execute("SELECT status FROM candidates WHERE id = %s", (cands[0],)).fetchone()["status"] == "blocked"
    sel = selection.select_for_channel(conn, settings, llm, "en", DAY)
    commentary.write_scripts(conn, settings, llm)
    render.render_pending(conn, settings, layout="blur")
    failing = MockLLM(fail_check_clips={sel["selected"][0]})
    out = policy.final_check(conn, settings, failing)
    assert out["failed_check"] == [sel["selected"][0]] and out["pending_approval"] == [sel["selected"][1]]
    assert cands[0] not in [r["candidate_id"] for r in conn.execute("SELECT candidate_id FROM clips").fetchall()]


def test_live_path_with_fake_platforms(conn, settings, footage, tmp_path):
    """The live code path, with fake platform clients: publish once, rights re-check, voice mix, takedown."""
    llm = MockLLM()
    rid = verified_rights(conn, licence_type="written_licence", owner_name="Licensed Creator",
                          expires_on="2027-01-01", document_url="https://example.invalid/licence.pdf")
    src = ingest.ingest_file(conn, settings, footage, origin="creator_upload", rights_id=rid)["source_id"]
    signals.extract(conn, settings, src, window_s=8)
    selection.triage_new(conn, settings, llm)
    sel = selection.select_for_channel(conn, settings, llm, "en", DAY)
    commentary.write_scripts(conn, settings, llm)

    # the owner's recorded voice for one clip: it is mixed in; the other clip renders with captions only
    settings.voice_dir.mkdir(parents=True)
    voiced = sel["selected"][0]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", "6", "-i", "sine=frequency=220",
                    str(settings.voice_dir / f"{voiced}.wav")], check=True)
    render.render_pending(conn, settings)
    has_voice = dict((r["clip_id"], r["has_voice"]) for r in conn.execute("SELECT clip_id, has_voice FROM renders").fetchall())
    assert has_voice == {sel["selected"][0]: True, sel["selected"][1]: False}

    policy.final_check(conn, settings, llm)
    policy.decide(conn, sel["selected"][0], approve=True, by="owner", tiktok_options=TT)
    policy.decide(conn, sel["selected"][1], approve=True, by="owner")           # no TikTok choices given
    publish.schedule_approved(conn, settings)

    settings.allow_live_publish = True
    calls = []
    later = datetime(2026, 11, 22, tzinfo=timezone.utc)
    # accounts are not enabled yet -> still a dry run even with the global switch on
    conn.execute("UPDATE platform_accounts SET enabled = TRUE WHERE platform = 'youtube'")
    conn.commit()
    out = publish.run_due(conn, settings, now=later, publisher_factory=fake_factory(calls))
    assert len(out["published"]) == 2 and len(out["dry_run"]) == 3 and len(out["blocked"]) == 1
    assert [c[1] for c in calls] == ["youtube", "youtube"]
    blocked = conn.execute("SELECT platform, last_error FROM publish_jobs WHERE status = 'blocked'").fetchone()
    assert blocked["platform"] == "tiktok" and "per-post choices" in blocked["last_error"]

    # re-running never posts the same clip twice
    assert publish.run_due(conn, settings, now=later, publisher_factory=fake_factory(calls))["published"] == []
    assert len(calls) == 2

    # analytics for what was published
    assert publish.pull_analytics(conn, settings, publisher_factory=fake_factory(calls))["pulled"] == 2
    assert conn.execute("SELECT sum(views) AS v FROM metrics").fetchone()["v"] == 200

    # gate 2: the licence lapses before a rescheduled post goes out -> blocked, not posted
    conn.execute("UPDATE publish_jobs SET status = 'scheduled' WHERE status = 'dry_run' AND platform = 'instagram'")
    conn.execute("UPDATE platform_accounts SET enabled = TRUE")
    conn.execute("UPDATE rights_ledger SET expires_on = %s WHERE id = %s", (later.date() - timedelta(days=1), rid))
    conn.commit()
    before = len(calls)
    out = publish.run_due(conn, settings, now=later, publisher_factory=fake_factory(calls))
    assert len(out["blocked"]) == 2 and not out["published"] and len(calls) == before
    assert "expired" in conn.execute("SELECT last_error FROM publish_jobs WHERE id = %s", (out["blocked"][0],)).fetchone()["last_error"]
    assert any("expires" in a for a in publish.health(conn, settings, today=later.date())["alerts"])

    # a retryable platform error is retried, a hard one fails for a person to look at
    class Flaky(FakePublisher):
        def publish(self, req):
            raise PlatformError("HTTP 503", retryable=True)

    conn.execute("UPDATE rights_ledger SET expires_on = '2027-01-01' WHERE id = %s", (rid,))
    conn.execute("UPDATE publish_jobs SET status = 'scheduled' WHERE status = 'blocked' AND platform = 'instagram'")
    conn.commit()
    flaky = lambda platform, channel_key, audited=False: Flaky(platform, calls)
    assert len(publish.run_due(conn, settings, now=later, publisher_factory=flaky)["retry"]) == 2
    publish.run_due(conn, settings, now=later, publisher_factory=flaky)
    assert len(publish.run_due(conn, settings, now=later, publisher_factory=flaky)["failed"]) == 2

    # takedown of the whole source: API removal where possible, a manual list where not, source blocked
    down = publish.take_down(conn, settings, source_id=src, reason="creator withdrew licence",
                             publisher_factory=fake_factory(calls))
    assert len(down["removed"]) == 2 and all(e["platform"] == "youtube" for e in down["removed"])
    assert conn.execute("SELECT status FROM sources WHERE id = %s", (src,)).fetchone()["status"] == "blocked"
    assert {r["status"] for r in conn.execute("SELECT status FROM clips").fetchall()} == {"taken_down"}
    assert not conn.execute("SELECT 1 FROM publish_jobs WHERE status IN ('scheduled', 'publishing')").fetchall()
    with pytest.raises(PermissionError):
        signals.extract(conn, settings, src)
