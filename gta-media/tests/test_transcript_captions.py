"""Accurate subtitles: captions from the clip's real speech (local Whisper word timings)."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess

import pytest

from conftest import verified_rights
from vicepulse import cli, commentary, media, render
from vicepulse.signals import Segment

SPOKEN = ("Watch this car jump over the bridge. Nobody expected that landing. "
          "The police are right behind him now. What a crazy escape.")


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_transcript_cues_follow_word_timings():
    seg = Segment(0.5, 4.0, "Watch this car jump over the bridge. Nobody expected that.",
                  [w(0.5, 0.8, "Watch"), w(0.8, 1.0, "this"), w(1.0, 1.3, "car"), w(1.3, 1.6, "jump"),
                   w(1.6, 1.8, "over"), w(1.8, 1.9, "the"), w(1.9, 2.3, "bridge."),
                   w(3.5, 3.8, "Nobody"), w(3.8, 4.2, "expected"), w(4.2, 4.6, "that.")])
    cues = commentary.transcript_cues([seg], 10.0)
    assert [c["text"] for c in cues] == ["Watch this car jump", "over the bridge.", "Nobody expected that."]
    assert cues[0]["start"] == 0.5 and cues[0]["end"] == 1.6           # shown while the words are spoken
    assert cues[1]["end"] <= cues[2]["start"]                           # no overlaps
    assert all(c["kind"] == "speech" for c in cues)


def test_transcript_cues_clamp_to_clip_and_minimum_time():
    seg = Segment(0, 6, "", [w(-0.5, 0.05, "cut"), w(5.9, 6.0, "late"), w(9.5, 9.7, "outside")])
    cues = commentary.transcript_cues([seg], 6.0)
    assert [c["text"] for c in cues] == ["cut", "late"] and cues[0]["start"] == 0.0
    assert cues[0]["end"] >= 0.7 and cues[-1]["end"] <= 6.0


def test_hook_title_card_sits_at_the_top():
    ass = render.build_ass([{"start": 0, "end": 2, "text": "HOOK", "kind": "hook_top"},
                            {"start": 0.5, "end": 1.5, "text": "said", "kind": "speech"}], "youtube")
    assert re.search(r"Style: HookTop,.*,8,80,80,260,1", ass)          # alignment 8 = top centre
    assert "HookTop,,0,0,0,,HOOK" in ass and "Cap,,0,0,0,,said" in ass


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, start=None, duration=None):
        self.calls.append((start, duration))
        return [Segment(1.0, 2.0, "hello there", [w(1.0, 1.4, "hello"), w(1.4, 2.0, "there.")])]


def test_auto_mode_uses_speech_only_when_whisper_is_on(settings):
    clip = {"in_s": 12.0, "out_s": 42.0, "file_path": "/x.mp4"}
    script = {"hook": "WAIT FOR IT", "body": "b " * 8, "closer": "c"}
    caps, src = commentary.build_captions(settings, clip, script, FakeTranscriber())
    assert src == "script"                                              # transcriber=none
    fake = FakeTranscriber()
    caps, src = commentary.build_captions(dataclasses.replace(settings, transcriber="whisper"), clip, script, fake)
    assert src == "transcript" and fake.calls == [(12.0, 30.0)]         # only the clip window is transcribed
    assert [c["text"] for c in caps] == ["hello there."]                # mock hooks are generic: not shown
    caps, _ = commentary.build_captions(dataclasses.replace(settings, transcriber="whisper", llm_mode="anthropic"),
                                        clip, script, FakeTranscriber())
    assert caps[0] == {"start": 0.0, "end": 2.0, "text": "WAIT FOR IT", "kind": "hook_top"}
    assert [c["text"] for c in caps[1:]] == ["hello there."]


class Silent:
    def transcribe(self, path, start=None, duration=None):
        return []


def test_transcript_mode_never_falls_back_to_invented_text(settings):
    clip = {"in_s": 0.0, "out_s": 30.0, "file_path": "/x.mp4"}
    script = {"hook": "H", "body": "made up words", "closer": "c"}
    caps, src = commentary.build_captions(dataclasses.replace(settings, captions="transcript"), clip, script, Silent())
    assert caps == [] and src.startswith("none")
    caps, src = commentary.build_captions(dataclasses.replace(settings, transcriber="whisper"), clip, script, Silent())
    assert src == "script"                                              # auto mode: script captions as before


def speech_video(path, tmp_path, visual="testsrc2=size=640x360:rate=30"):
    wav = tmp_path / "speech.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"flite=text='{SPOKEN}':voice=slt",
                    "-ar", "16000", str(wav)], check=True, capture_output=True)
    d = media.probe(wav)["duration"]
    total = round(d + 8, 1)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", str(total), "-i", visual,
                    "-i", str(wav), "-filter_complex", "[1:a]adelay=4000:all=1,apad[a]", "-map", "0:v", "-map", "[a]",
                    "-t", str(total), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)
    return path


def _norm(text):
    return re.findall(r"[a-z]+", text.lower())


def test_real_whisper_subtitles_match_the_speech(settings, conn, monkeypatch, capsys, tmp_path):
    pytest.importorskip("faster_whisper")
    try:
        video = speech_video(tmp_path / "talk.mp4", tmp_path)
    except subprocess.CalledProcessError:
        pytest.skip("ffmpeg built without flite")
    s = dataclasses.replace(settings, transcriber="whisper", whisper_model="base")
    monkeypatch.setattr(cli, "Settings", lambda: s)
    rid = verified_rights(conn)
    assert cli.main(["ingest", "--file", str(video), "--origin", "own_recording", "--rights-id", str(rid)]) == 0
    capsys.readouterr()
    clip_s = min(60, int(media.probe(video)["duration"]))
    assert cli.main(["signals", "--clip-seconds", str(max(15, clip_s))]) == 0
    assert cli.main(["daily", "--channel", "en", "--max-clips", "1"]) == 0
    json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    clip = conn.execute("SELECT * FROM clips").fetchone()
    assert clip["script"]["caption_source"] == "transcript"
    speech = [c for c in clip["script"]["captions"] if c["kind"] == "speech"]
    heard = _norm(" ".join(c["text"] for c in speech))
    said = _norm(SPOKEN)
    hits = sum(1 for word in said if word in heard)
    assert hits / len(said) >= 0.8, heard                               # subtitles say what was said
    assert all(len(c["text"].split()) <= commentary.WORDS_PER_CUE for c in speech)
    offset = 4.0 - clip["in_s"]                                         # speech starts 4 s into the source
    assert speech[0]["start"] >= offset - 0.6
    r = conn.execute("SELECT * FROM renders WHERE clip_id = %s AND platform = 'youtube'", (clip["id"],)).fetchone()
    info = media.probe(r["file_path"])
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]


def test_highlight_command_one_file(settings, conn, footage, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    code = cli.main(["highlight", "--file", str(footage), "--rights", "cc", "--licence", "CC BY-NC 4.0",
                     "--author", "A", "--url", "https://example.invalid/l"])
    assert code == 1 and "cannot be used" in capsys.readouterr().err
    assert cli.main(["highlight", "--file", str(footage), "--rights", "own", "--clip-seconds", "30"]) == 0
    out = json.loads(capsys.readouterr().out)
    [f] = out["finished_files"]
    info = media.probe(f)
    assert (info["width"], info["height"]) == (1080, 1920) and 29.5 <= info["duration"] <= 30.5
    assert out["details"][0]["status"] == "pending_approval" and out["details"][0]["subtitles"] == "script"
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0
    # running it again on the same file hands back the same clip instead of making a new one
    assert cli.main(["highlight", "--file", str(footage), "--rights", "own"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["already_done"] and again["clips"] == out["clips"]


def test_highlight_explains_a_duplicate_and_ignores_the_daily_cap(settings, conn, footage, tmp_path, monkeypatch, capsys):
    from conftest import make_footage
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    rid = verified_rights(conn)
    cli.main(["ingest", "--file", str(footage), "--origin", "own_recording", "--rights-id", str(rid)])
    cli.main(["signals"])
    cli.main(["daily", "--channel", "en"])                                # fills today's automatic slots
    capsys.readouterr()
    same_look = make_footage(tmp_path / "reencoded.mp4", crf=30)          # same visuals, different file
    out = json.loads(capsys.readouterr().out or "{}") if cli.main(
        ["highlight", "--file", str(same_look), "--rights", "own", "--clip-seconds", "20"]) == 0 else None
    assert out and out["clips"] == []
    [why] = out["why_no_clip"]
    assert why["note"] != "day already full"                             # a requested highlight is not capped
    assert any("same visuals" in r for f in why["filtered"] for r in f["reasons"])


class CountingTranscriber:
    """Different speech for every clip, like real videos."""
    n = 0

    def transcribe(self, path, start=None, duration=None):
        CountingTranscriber.n += 1
        lines = [["Welcome", "to", "the", "farm", "house."], ["The", "rocket", "launch", "starts", "now."],
                 ["Cats", "sleep", "all", "day", "long."], ["Rain", "falls", "on", "quiet", "streets."]]
        words = lines[CountingTranscriber.n % len(lines)]
        return [Segment(1.0, 4.0, " ".join(words), [w(1 + i * 0.5, 1.4 + i * 0.5, t) for i, t in enumerate(words)])]


def test_mock_commentary_does_not_make_different_videos_duplicates(settings, conn, tmp_path, monkeypatch):
    from conftest import make_footage
    from vicepulse import autosource, signals
    from vicepulse.llm import MockLLM
    monkeypatch.setattr(signals, "make_transcriber", lambda s: CountingTranscriber())
    s = dataclasses.replace(settings, transcriber="whisper")
    a = make_footage(tmp_path / "a.mp4", patterns=["testsrc2", "smptebars"], seg_s=10)
    b = make_footage(tmp_path / "b.mp4", patterns=["mandelbrot", "rgbtestsrc"], seg_s=10)
    c = make_footage(tmp_path / "c.mp4", patterns=["pal75bars", "testsrc"], seg_s=10)
    outs = [autosource.highlight(conn, s, MockLLM(), f, rights="own", clip_seconds=15) for f in (a, b, c)]
    assert [o["details"][0]["status"] for o in outs] == ["pending_approval"] * 3, outs

    # a clip wrongly marked as a script duplicate by an older version gets another go on the next run
    cid = outs[2]["clips"][0]
    conn.execute("DELETE FROM renders WHERE clip_id = %s", (cid,))
    conn.execute("UPDATE clips SET status = 'duplicate', status_reason = %s WHERE id = %s",
                 ("script 82% similar to clip 1", cid))
    conn.commit()
    again = autosource.highlight(conn, s, MockLLM(), c, rights="own", clip_seconds=15)
    assert again["clips"] == [cid] and again["details"][0]["status"] == "pending_approval" and again["finished_files"]
