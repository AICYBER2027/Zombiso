"""Edit styles: event-driven plans (no random effects, nothing invented), caption sync, and real renders."""

from __future__ import annotations

import shutil

import pytest

from vicepulse import live, media, render, styles
from vicepulse.styles import Event


def _analysis(events, dur=30.0):
    return {"duration": dur, "events": events, "motion": {"keys": [(0.5, 0.5), (dur - 0.5, 0.6)], "spread": 0.3},
            "src_fps": 60.0}


BANG = Event(18.0, "loud", 0.8, "+16 dB over the clip's normal level")


def test_style_names_and_aliases():
    assert styles.style_name("Funny") == "viral" and styles.style_name("clean-professional") == "clean"
    with pytest.raises(ValueError):
        styles.style_name("random")
    assert render.edit_style("classic") is None and render.edit_style("cinematic") == "cinematic"


def test_cinematic_plan_teaser_context_slowmo_and_a_reason_for_every_effect():
    plan = styles.plan_edit("cinematic", _analysis([Event(5.0, "cut", 0.5, "scene change"), BANG]))
    roles = [s.role for s in plan.segments]
    assert roles[0] == "teaser" and "slowmo" in roles
    teaser = plan.segments[0]
    assert teaser.start <= 18.0 <= teaser.end                       # the teaser is the real payoff
    slow = next(s for s in plan.segments if s.role == "slowmo")
    assert slow.speed == 0.5 and slow.start < 18.0 < slow.end
    assert plan.segments[1].start == pytest.approx(6.0)             # 12 s of context before the payoff
    assert plan.duration == pytest.approx(sum(s.out_len for s in plan.segments))
    assert plan.effects and all(f.reason for f in plan.effects)
    push = next(f for f in plan.effects if f.kind == "push")
    assert "+16 dB" in push.reason and push.t < plan.to_out(18.0) < push.t + push.dur


def test_no_slowmo_over_speech_and_voiceover_keeps_the_whole_clip_in_order():
    speech = [{"start": 17.5, "end": 19.0, "text": "no way", "kind": "speech"}]
    plan = styles.plan_edit("cinematic", _analysis([BANG]), speech)
    assert all(s.speed == 1.0 for s in plan.segments)
    assert any("speaking" in n for n in plan.notes)
    plan = styles.plan_edit("viral", _analysis([BANG]), voice=True)
    assert [(s.start, s.end, s.role) for s in plan.segments] == [(0.0, 30.0, "story")]


def test_without_a_real_payoff_there_are_no_payoff_effects():
    plan = styles.plan_edit("viral", _analysis([Event(5.0, "cut", 0.5, "scene change"),
                                                Event(9.0, "loud", 0.1, "+5 dB over the clip's normal level")]))
    assert plan.payoff is None and len(plan.segments) == 1
    assert not [f for f in plan.effects if f.kind in ("zoom", "shake", "flash", "push", "hit")]


def test_viral_punch_ins_sit_on_real_loud_moments_only():
    events = [Event(t, "loud", st, f"+{10 + t:.0f} dB") for t, st in
              [(10.0, 0.4), (10.8, 0.9), (14.0, 0.2), (18.0, 0.8), (22.0, 0.6)]]
    plan = styles.plan_edit("viral", _analysis(events))
    zooms = sorted(f.t for f in plan.effects if f.kind == "zoom")
    wanted = sorted(plan.to_out(t) for t, st in [(10.8, 0.9), (18.0, 0.8), (22.0, 0.6)])
    assert zooms == wanted        # 14.0 is too weak; 10.0 is too close to the stronger 10.8


def test_reactions_only_come_from_real_speech():
    cues = [{"start": 1, "end": 2, "text": "no way", "kind": "body"},          # the channel's own script
            {"start": 5, "end": 6, "text": "NO WAY bro", "kind": "speech"}]
    ev = styles.reaction_events(cues)
    assert [e.t for e in ev] == [5]


def test_speech_captions_stay_in_sync_through_teaser_and_slow_motion():
    cues = [{"start": 0.0, "end": 2.0, "text": "Hook", "kind": "hook"},
            {"start": 8.0, "end": 9.0, "text": "early words", "kind": "speech"},
            {"start": 22.0, "end": 23.0, "text": "after the bang", "kind": "speech"}]
    plan = styles.plan_edit("cinematic", _analysis([BANG]), cues)
    assert plan.segments[-1].end == pytest.approx(23.3)          # the clip waits for the sentence to finish
    out = {c["text"]: c for c in styles.retime_cues(cues, plan)}
    assert out["Hook"]["start"] == 0.0
    assert out["early words"]["start"] == pytest.approx(plan.to_out(8.0))
    assert out["after the bang"]["end"] - out["after the bang"]["start"] == pytest.approx(1.0)
    assert out["after the bang"]["start"] == pytest.approx(plan.to_out(22.0))


def test_caption_files_differ_by_style_and_keep_brand_lines():
    plan = styles.plan_edit("clean", _analysis([BANG]))
    cues = [{"start": 3.0, "end": 4.5, "text": "watch this", "kind": "speech"}]
    ass = styles.build_styled_ass(cues, plan, "shared", {"handle": "@creator", "outro_text": "Follow for more"})
    assert "@creator" in ass and "Follow for more" in ass and ",3,14,0,2," in ass     # boxed captions
    viral = styles.build_styled_ass(cues, styles.plan_edit("viral", _analysis([BANG])), "tiktok")
    assert viral.count("WATCH") == 2                                                  # one line per word


def test_three_styles_render_from_one_analysis(footage, tmp_path):
    info = media.probe(footage)
    res = render.compare_styles(str(footage), in_s=0.0, out_s=24.0, cues=[], styles_=["cinematic", "viral", "clean"],
                                out_dir=str(tmp_path / "cmp"), size="360x640", encoder="libx264")
    assert info["fps"] == 30.0
    by = {r["style"]: r for r in res["styles"]}
    assert set(by) == {"cinematic", "viral", "clean"}
    for r in by.values():
        assert r["qc"]["ok"], r["qc"]
        assert (r["qc"]["width"], r["qc"]["height"]) == (360, 640)
    assert any(s["role"] == "slowmo" for s in by["cinematic"]["plan"]["segments"])     # +30 dB bang: half speed
    assert any(f["kind"] == "zoom" for f in by["viral"]["plan"]["effects"])
    assert media.probe(res["side_by_side"])["width"] == 3 * 360


def test_live_edit_uses_the_chosen_style_and_logs_why(settings, conn, footage, monkeypatch):
    from vicepulse import feeds

    monkeypatch.setenv("VICEPULSE_LIVE_ENCODER", "libx264")
    monkeypatch.setenv("VICEPULSE_LIVE_EDIT_STYLE", "viral")
    monkeypatch.setenv("VICEPULSE_LIVE_RENDER_SIZE", "360x640")
    feeds.add_feed(conn, settings, "stylefeed", "Style feed", "en", own=True)
    s = feeds.start_session(conn, settings, "stylefeed")
    conn.execute("UPDATE live_sessions SET status = 'live' WHERE id = %s", (s["id"],))
    conn.commit()
    mid = live.record_moment(conn, s["id"], {"score": 0.9}, kind="audio")
    dest = settings.dir("live", f"session_{s['id']}") / f"moment_{mid}.mp4"
    shutil.copy(footage, dest)
    ids = live.default_edit(settings, s, mid, dest)
    conn.rollback()
    assert ids
    ev = conn.execute("SELECT detail FROM events WHERE stage = 'render' ORDER BY id DESC LIMIT 1").fetchone()["detail"]
    assert ev["style"] == "viral" and ev["qc"]["ok"], ev.get("qc")
    assert ev["plan"]["effects"] and all(f["reason"] for f in ev["plan"]["effects"])
