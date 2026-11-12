"""One 30-second clip on demand: --clip-seconds (15-60) and --max-clips."""

from __future__ import annotations

import json

from conftest import verified_rights
from vicepulse import cli, media


def run(capsys, *args):
    code = cli.main(list(args))
    out = capsys.readouterr()
    return code, (json.loads(out.out) if code == 0 else out.err)


def test_one_30_second_vertical_clip(settings, conn, footage, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    rid = verified_rights(conn)
    code, ing = run(capsys, "ingest", "--file", str(footage), "--origin", "own_recording", "--rights-id", str(rid))
    assert code == 0 and ing["status"] == "cleared"

    code, err = run(capsys, "signals", "--clip-seconds", "10")
    assert code == 1 and "between 15 and 60" in err

    code, sig = run(capsys, "signals", "--clip-seconds", "30")
    assert code == 0 and sig["sources"][0]["candidates"] > 0
    lengths = {round(r["end_s"] - r["start_s"]) for r in conn.execute("SELECT start_s, end_s FROM candidates").fetchall()}
    assert lengths == {30}

    code, daily = run(capsys, "daily", "--channel", "en", "--max-clips", "1")
    assert code == 0
    assert len(daily["select"]["channels"]) == 1 and len(daily["select"]["channels"][0]["selected"]) == 1
    clip = conn.execute("SELECT * FROM clips").fetchone()
    assert clip["status"] == "pending_approval" and clip["language"] == "en"
    r = conn.execute("SELECT * FROM renders WHERE clip_id = %s AND platform = 'youtube'", (clip["id"],)).fetchone()
    info = media.probe(r["file_path"])
    assert (info["width"], info["height"], info["video_codec"]) == (1080, 1920, "h264") and info["has_audio"]
    assert 29.5 <= info["duration"] <= 30.5
    assert clip["script"]["captions"]                                  # subtitles were generated and burned in
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0


def test_cc_licensed_footage_carries_attribution(settings, conn, other_footage, monkeypatch, capsys, tmp_path):
    """CC BY footage (e.g. Wikimedia Commons): licence recorded from a JSON file, credit in every description."""
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    lic = tmp_path / "licence.json"
    lic.write_text(json.dumps({
        "owner_name": "Xbox México", "licence_type": "creative_commons", "cc_licence": "CC-BY-4.0",
        "platforms": ["youtube", "instagram", "tiktok"], "starts_on": "2026-11-12", "monetisation_allowed": True,
        "derivative_edits_allowed": True, "credit_text": "Gameplay: Xbox México (CC BY 4.0), via Wikimedia Commons",
        "document_url": "https://commons.wikimedia.org/wiki/File:Minecraft_Alpha_Gameplay.webm"}, ensure_ascii=False),
        encoding="utf-8")
    code, out = run(capsys, "rights-add", "--json-file", str(lic))
    assert code == 0
    rid = out["rights_id"]
    assert run(capsys, "rights-verify", "--id", str(rid), "--by", "test")[0] == 0
    code, ing = run(capsys, "ingest", "--file", str(other_footage), "--origin", "creator_upload", "--rights-id", str(rid))
    assert code == 0 and ing["status"] == "cleared", ing
    assert run(capsys, "signals", "--clip-seconds", "15")[0] == 0
    code, daily = run(capsys, "daily", "--channel", "en", "--max-clips", "1", "--source-id", str(ing["source_id"]))
    assert code == 0
    clip = conn.execute("SELECT * FROM clips").fetchone()
    src = conn.execute("SELECT c.source_id FROM candidates c WHERE c.id = %s", (clip["candidate_id"],)).fetchone()
    assert src["source_id"] == ing["source_id"]
    for p in ("youtube", "instagram", "tiktok"):
        assert clip["titles"][p]["description"].endswith("Gameplay: Xbox México (CC BY 4.0), via Wikimedia Commons")
    assert run(capsys, "rights-add")[0] == 1                       # needs --json or --json-file
