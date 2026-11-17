"""Live-only Twitch clipping: spike detection, the live-only guard, capture -> edit flow, dashboard.

Twitch is faked; nothing reaches the network. One test runs the real edit pipeline on synthetic footage.
"""

from __future__ import annotations

import shutil
import threading
import time

import psycopg
import pytest
import requests

from vicepulse import live, review


def _rights(conn):
    from vicepulse.autosource import rights_for_file
    rid = rights_for_file(conn, "own")
    conn.commit()
    return rid


def _session(conn, status="waiting", channel="en"):
    row = conn.execute("""INSERT INTO live_sessions (platform, broadcaster_id, login, display_name, channel_key, rights_id, status)
                          VALUES ('twitch', '123', 'streamer', 'Streamer', %s, %s, %s) RETURNING *""",
                       (channel, _rights(conn), status)).fetchone()
    conn.commit()
    return row


def test_spike_detector_baseline_burst_merge_and_quiet_chat():
    d = live.SpikeDetector()
    t = 1000.0
    for i in range(150):                       # steady chat, 1 message per second
        d.add(t + i, f"normal message {i % 7}")
    assert d.evaluate(t + 150) is None
    for i in range(48):                        # 8 per second for 6 s, lots of KEKW and repeats
        d.add(t + 150 + i / 8, "KEKW" if i % 2 else "kkkkkkk que isso")
    spike = d.evaluate(t + 156)
    assert spike and not spike.get("merge") and spike["score"] >= 0.55 and spike["hype_share"] > 0.9
    for i in range(48):                        # another burst 20 s later -> merged into the same moment
        d.add(t + 170 + i / 8, "LUL")
    assert d.evaluate(t + 176).get("merge") is True

    quiet = live.SpikeDetector()
    hits = []
    for i in range(200):                       # a dead chat with an occasional "KEKW" never triggers
        quiet.add(t + i * 5, "KEKW")
        hits.append(quiet.evaluate(t + i * 5))
    assert all(h is None for h in hits)


def test_live_only_guard_in_the_database(conn):
    s = _session(conn, "waiting")
    with pytest.raises(psycopg.Error, match="live-only"):
        live.record_moment(conn, s["id"], {"score": 0.9})
    conn.rollback()
    conn.execute("UPDATE live_sessions SET status = 'live' WHERE id = %s", (s["id"],))
    conn.commit()
    assert live.record_moment(conn, s["id"], {"score": 0.9})
    conn.execute("UPDATE live_sessions SET status = 'ended' WHERE id = %s", (s["id"],))
    conn.commit()
    with pytest.raises(psycopg.Error, match="live-only"):
        live.record_moment(conn, s["id"], {"score": 0.9})
    conn.rollback()


def test_unauthorized_creator_cannot_be_watched(conn):
    cid = conn.execute("INSERT INTO creators (display_name, permission_status) VALUES ('Someone', 'needs_review') "
                       "RETURNING id").fetchone()["id"]
    conn.commit()
    with pytest.raises(ValueError, match="no recorded permission"):
        live.rights_for_session(conn, creator_id=cid, own=False)
    with pytest.raises(ValueError, match="--creator-id"):
        live.rights_for_session(conn, creator_id=None, own=False)


class FakeTwitch:
    def __init__(self, footage):
        self.footage, self.is_live, self.calls = footage, True, []

    def stream(self, bid):
        self.calls.append("stream")
        return {"id": "s1", "type": "live", "title": "GTA 6 day one", "started_at": "2026-11-19T12:00:00Z"} if self.is_live else None

    def create_clip(self, bid, duration=60, title=None):
        assert self.is_live, "Create Clip must never be called for an offline stream"
        self.calls.append(("create_clip", duration))
        return "ClipABC"

    def wait_clip(self, clip_id, timeout_s=60, every_s=3):
        return {"id": clip_id, "created_at": "2026-11-19T12:30:00Z"}

    def download_url(self, bid, clip_id):
        return "https://clips-media.example/ClipABC.mp4"

    def fetch(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.footage, dest)
        return dest


class NoChat:
    def __init__(self, login, on_message):
        self.stop = threading.Event()

    def start(self):
        pass


def _wait(pred, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _burst(runner, now):
    for i in range(150):
        runner.detector_add(now - 160 + i, "chat")
    for i in range(48):
        runner.detector_add(now - 6 + i / 8, "KEKW")


def test_spike_is_captured_downloaded_and_edited_while_monitoring_continues(settings, conn, footage):
    s = _session(conn)
    tw = FakeTwitch(footage)
    edited = []

    def fake_edit(st, sess, mid, path):
        time.sleep(1.0)                        # editing takes time; monitoring must keep going meanwhile
        edited.append((mid, path.name))
        return [4242]

    r = live.LiveRunner(settings, s, tw=tw, capture_delay_s=0.2, min_combined=0.0, chat_factory=NoChat, edit=fake_edit)
    assert r.poll_live() and r.s["status"] == "live"
    now = time.time()
    _burst(r, now)
    mid = r.tick(now)
    assert mid
    assert r.tick(now + 1) is None             # monitoring goes on (no duplicate moment)
    def status():
        conn.rollback()                        # see the worker threads' commits
        return conn.execute("SELECT status FROM live_moments WHERE id = %s", (mid,)).fetchone()["status"]

    assert _wait(lambda: status() in ("ready", "failed", "skipped")) and status() == "ready", status()
    m = conn.execute("SELECT * FROM live_moments WHERE id = %s", (mid,)).fetchone()
    assert m["twitch_clip_id"] == "ClipABC" and m["clip_ids"] == [4242] and m["latency_s"] is not None
    assert "audio_excitement" in m["signals"] and ("create_clip", 60) in tw.calls
    assert m["file_path"].endswith(f"moment_{mid}.mp4") and edited == [(mid, f"moment_{mid}.mp4")]

    # the stream ends: the session closes and no new moment can start
    tw.is_live = False
    assert r.poll_live() is False and r.s["status"] == "ended"
    _burst(r, now + 100)
    assert r.tick(now + 100) is None
    r.edits.shutdown(wait=True)


def test_stream_ending_before_the_capture_is_skipped_not_clipped_from_the_archive(settings, conn, footage):
    s = _session(conn)
    tw = FakeTwitch(footage)
    r = live.LiveRunner(settings, s, tw=tw, capture_delay_s=0.5, chat_factory=NoChat, edit=lambda *a: [1])
    r.poll_live()
    now = time.time()
    _burst(r, now)
    mid = r.tick(now)
    tw.is_live = False                         # stream ends during the wait
    for t in r.captures:
        t.join(10)
    conn.rollback()
    m = conn.execute("SELECT status, status_reason FROM live_moments WHERE id = %s", (mid,)).fetchone()
    assert m["status"] == "skipped" and "live-only" in m["status_reason"]
    assert not any(isinstance(c, tuple) and c[0] == "create_clip" for c in tw.calls)
    r.edits.shutdown(wait=True)


def test_real_edit_pipeline_puts_the_live_clip_in_the_review_queue_and_dashboard(settings, conn, footage):
    s = _session(conn, "live")
    conn.execute("INSERT INTO live_moments (session_id, score) VALUES (%s, 0.8)", (s["id"],))
    conn.commit()
    mid = conn.execute("SELECT id FROM live_moments").fetchone()["id"]
    dest = settings.dir("live", f"session_{s['id']}") / f"moment_{mid}.mp4"
    shutil.copy(footage, dest)
    ids = live.default_edit(settings, s, mid, dest)
    assert ids
    conn.rollback()
    cl = conn.execute("SELECT status FROM clips WHERE id = %s", (ids[0],)).fetchone()
    assert cl["status"] in ("pending_approval", "failed_check")
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0

    httpd = review.make_server(settings, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        page = requests.get(f"http://127.0.0.1:{httpd.server_address[1]}/live").text
        assert "Streamer" in page and "LIVE" in page and "SPIKE DETECTED" in page and "location.reload" in page
    finally:
        httpd.shutdown()
