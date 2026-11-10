"""Pure-function tests: rights rules, duplicate helpers, signal windows, captions, slots."""

from datetime import date, datetime, timezone

import pytest
from PIL import Image

from vicepulse import commentary, dedupe, publish, render, signals
from vicepulse.rights import check_grant

TODAY = date(2026, 11, 10)


def grant(**kw):
    base = dict(licence_type="written_licence", platforms=["youtube", "instagram", "tiktok"], languages=["*"],
                starts_on=date(2026, 1, 1), expires_on=date(2027, 1, 1), monetisation_allowed=True,
                derivative_edits_allowed=True, document_url="https://example.invalid/licence.pdf",
                verified_at=datetime(2026, 2, 1, tzinfo=timezone.utc), revoked_at=None, cc_licence=None)
    return {**base, **kw}


def test_valid_grant_passes():
    assert check_grant(grant(), on=TODAY, platform="tiktok", language="pt-BR", monetised=True).ok


@pytest.mark.parametrize("change, expected", [
    (dict(verified_at=None), "not verified"),
    (dict(revoked_at=datetime(2026, 5, 1, tzinfo=timezone.utc)), "revoked"),
    (dict(document_url=None), "no signed licence"),
    (dict(expires_on=date(2026, 11, 9)), "expired"),
    (dict(starts_on=date(2026, 12, 1)), "starts"),
    (dict(platforms=["youtube"]), "does not cover tiktok"),
    (dict(languages=["en"]), "language pt-BR"),
    (dict(monetisation_allowed=False), "monetisation"),
    (dict(derivative_edits_allowed=False), "derivative"),
    (dict(licence_type="creative_commons", cc_licence="CC-BY-ND-4.0"), "NoDerivatives"),
    (dict(licence_type="creative_commons", cc_licence="CC-BY-NC-4.0"), "NonCommercial"),
])
def test_grant_fails_closed(change, expected):
    d = check_grant(grant(**change), on=TODAY, platform="tiktok", language="pt-BR", monetised=True)
    assert not d.ok and any(expected in r for r in d.reasons), d.reasons


def test_no_ledger_row_is_refused():
    d = check_grant(None)
    assert not d.ok and d.reasons == ["no rights ledger entry"]


def test_language_family_is_covered():
    assert check_grant(grant(languages=["pt"]), on=TODAY, language="pt-BR").ok


def test_overlap_share():
    assert dedupe.overlap_share((0, 10), (5, 15)) == 0.5
    assert dedupe.overlap_share((0, 10), (10, 20)) == 0
    assert dedupe.overlap_share((0, 30), (10, 15)) == 1.0


def test_dhash_same_image_and_different_image(tmp_path):
    a, b, c = tmp_path / "a.png", tmp_path / "b.png", tmp_path / "c.png"
    Image.linear_gradient("L").rotate(90).save(a)               # dark-to-light across
    Image.linear_gradient("L").rotate(90).resize((200, 200)).save(b)   # same picture, different size
    Image.linear_gradient("L").rotate(270).save(c)              # mirrored: a different picture
    assert dedupe.hamming(dedupe.dhash(a), dedupe.dhash(b)) <= 4
    assert dedupe.hamming(dedupe.dhash(a), dedupe.dhash(c)) > dedupe.HASH_DISTANCE
    assert dedupe.hashes_match([dedupe.dhash(a)], [dedupe.dhash(b)])
    assert not dedupe.hashes_match([dedupe.dhash(a)], [dedupe.dhash(c)])
    assert not dedupe.hashes_match([], [dedupe.dhash(a)])


def test_text_similarity():
    assert dedupe.text_similarity("Nobody expected this to work!", "nobody expected this to work") == 1.0
    assert dedupe.text_similarity("Nobody expected this to work", "The police AI is broken again") < 0.6


def test_loudness_peaks_and_windows():
    curve = [(t / 10, -40.0) for t in range(400)]
    for t in (100, 250):
        curve[t] = (t / 10, -12.0)
    peaks = signals.loudness_peaks(curve)
    assert [p[0] for p in peaks] == [10.0, 25.0]
    windows = signals.candidate_windows(40.0, cuts=[20.0], peaks=peaks, length_s=10)
    assert all(0 <= w["start_s"] < w["end_s"] <= 40 for w in windows)
    assert {(w["start_s"], w["end_s"]) for w in windows} == {(7.0, 17.0), (17.0, 27.0), (22.0, 32.0)}


def test_windows_without_any_signal_still_gives_one():
    assert len(signals.candidate_windows(30.0, [], [], length_s=20)) == 1


def test_caption_cues_cover_clip_in_order():
    script = {"hook": "Nobody expected this", "body": "one two three four five six seven eight nine", "closer": "Follow for more"}
    cues = commentary.caption_cues(script, 12.0)
    assert cues[0]["kind"] == "hook" and cues[0]["start"] == 0 and cues[0]["end"] <= 2.0
    assert cues[-1]["kind"] == "closer" and cues[-1]["end"] == 12.0
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(cues, cues[1:]))
    assert all(len(c["text"].split()) <= commentary.WORDS_PER_CUE for c in cues)


def test_ass_file_escapes_and_styles():
    ass = render.build_ass([{"start": 0, "end": 1.5, "text": "Olha {isso}\\N aqui", "kind": "hook"},
                            {"start": 1.5, "end": 3, "text": "ação e reação", "kind": "body"}], "tiktok")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:01.50,Hook,,0,0,0,,Olha (isso)N aqui" in ass
    assert "ação e reação" in ass and ",520,1" in ass


def test_slot_times_use_channel_timezone():
    cfg = {"timezone": "America/Sao_Paulo", "default_slots": ["10:00", "22:00"]}
    slots = publish.slot_times(cfg, date(2026, 11, 20))
    assert slots[0].utcoffset().total_seconds() == -3 * 3600 and slots[1].hour == 22
