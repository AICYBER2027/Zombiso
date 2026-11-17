"""Direct live feeds: stream-key check, audio spike detection, rolling-buffer cuts, reconnect grace,
the shared editing queue, and EN+BR live renders at 720x1280 with one encode per version.

The receiver (MediaMTX) is faked here; the real one was tested end to end with simulated OBS feeds
(see approvals/direct-feed-test.md).
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from vicepulse import feeds, live, worker
from vicepulse.media import probe


def _feed(conn, settings, slug="testfeed", also=None):
    return feeds.add_feed(conn, settings, slug, "Test Feed", "en", own=True, also_channels=also)


def _secret(row):
    return row["obs"]["stream_key"].split("pass=", 1)[1]


def test_stream_key_is_checked_and_stored_hashed(settings, conn):
    row = _feed(conn, settings)
    secret = _secret(row)
    assert row["key_hash"] != secret and secret not in json.dumps(row["key_hash"])
    ok = {"action": "publish", "path": "vp/testfeed", "user": "testfeed", "password": secret}
    assert feeds.check_publish(conn, ok)
    assert not feeds.check_publish(conn, {**ok, "password": secret + "x"})
    assert not feeds.check_publish(conn, {**ok, "path": "vp/otherfeed"})
    assert not feeds.check_publish(conn, {**ok, "path": "live/testfeed"})
    assert not feeds.check_publish(conn, {**ok, "user": "someone"})
    # encoders that only pass the query string
    assert feeds.check_publish(conn, {"action": "publish", "path": "vp/testfeed",
                                      "query": f"user=testfeed&pass={secret}"})
    conn.execute("UPDATE live_feeds SET enabled = false")
    conn.commit()
    assert not feeds.check_publish(conn, ok)
    # a new key replaces the old one
    conn.execute("UPDATE live_feeds SET enabled = true")
    conn.commit()
    new = feeds.new_key(conn, "testfeed", settings)["stream_key"].split("pass=", 1)[1]
    assert not feeds.check_publish(conn, ok) and feeds.check_publish(conn, {**ok, "password": new})


def test_feed_needs_rights(settings, conn):
    cid = conn.execute("INSERT INTO creators (display_name, permission_status) VALUES ('X', 'needs_review') RETURNING id"
                       ).fetchone()["id"]
    conn.commit()
    with pytest.raises(ValueError, match="no recorded permission"):
        feeds.add_feed(conn, settings, "xfeed", "X", "br", creator_id=cid)
    with pytest.raises(ValueError, match="feed name"):
        feeds.add_feed(conn, settings, "Bad Name!", "X", "br", own=True)


def test_worker_feed_auth_endpoint(settings, conn):
    secret = _secret(_feed(conn, settings))
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), worker.make_handler(settings))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/feed-auth"
    try:
        body = {"action": "publish", "path": "vp/testfeed", "user": "testfeed", "password": secret, "protocol": "rtmp"}
        assert requests.post(url, json=body).status_code == 200
        assert requests.post(url, json={**body, "password": "nope"}).status_code == 401
        assert requests.post(url, data=b"not json").status_code == 401
    finally:
        httpd.shutdown()


def test_audio_spike_detector():
    rnd = random.Random(1)
    d = feeds.AudioSpikeDetector()
    t = 1000.0
    for i in range(240):                                # 2 min of normal talk/game audio around -30 dB
        d.add(t + i * 0.5, -30 + rnd.uniform(-3, 3))
        assert d.evaluate(t + i * 0.5) is None
    for i in range(8):                                  # 4 s of screaming, ~ -10 dB
        d.add(t + 120 + i * 0.5, -10 + rnd.uniform(-1, 1))
    spike = d.evaluate(t + 123.5)
    assert spike and not spike.get("merge") and spike["rise_db"] > 15 and spike["score"] >= 0.55
    for i in range(60):                                 # back to normal, then another burst 30 s later -> merge
        d.add(t + 124 + i * 0.5, -30)
    for i in range(8):
        d.add(t + 154 + i * 0.5, -8)
    assert d.evaluate(t + 157.5).get("merge") is True

    quiet = feeds.AudioSpikeDetector()                  # silence with small blips never triggers (floor)
    for i in range(400):
        quiet.add(t + i * 0.5, -75 if i % 40 else -55)
        assert quiet.evaluate(t + i * 0.5) is None


def make_buffer(folder: Path, footage: Path, start: float) -> list[float]:
    """Turn footage into MediaMTX-style 6 s chunks named by their wall-clock start time."""
    tmp = folder.parent / "seg_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(footage), "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                    "-c:a", "aac", "-f", "segment", "-segment_time", "6", "-reset_timestamps", "0",
                    str(tmp / "s%03d.ts")], check=True)
    folder.mkdir(parents=True, exist_ok=True)
    t, starts = start, []
    for p in sorted(tmp.glob("s*.ts")):
        name = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f") + ".ts"
        p.rename(folder / name)
        starts.append(t)
        t += probe(folder / name)["duration"]
    return starts


def test_cut_window_from_rolling_buffer(tmp_path, footage):
    start = time.time() - 60
    starts = make_buffer(tmp_path / "rec" / "vp" / "f", footage, start)
    assert len(starts) >= 7 and len(feeds.segments(tmp_path / "rec" / "vp" / "f")) == len(starts)
    out = tmp_path / "m.mp4"
    info = feeds.cut_window(tmp_path / "rec" / "vp" / "f", start + 10, start + 35, out)
    assert 23 <= info["duration_s"] <= 27 and info["segments"] >= 4 and out.exists()
    with pytest.raises(ValueError, match="no video"):
        feeds.cut_window(tmp_path / "rec" / "vp" / "f", start + 500, start + 520, tmp_path / "x.mp4")


class FakeReceiver:
    def __init__(self, rec_dir):
        self.rec_dir, self.up = Path(rec_dir), True

    def ready(self, path):
        return self.up

    def audio_url(self, path):
        return f"rtsp://fake/{path}"

    def folder(self, path):
        return self.rec_dir / path


class NoReader:
    def __init__(self, url, on_level):
        self.alive = True

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False


def _feed_runner(settings, conn, footage, tmp_path, **kw):
    _feed(conn, settings)
    row = feeds.start_session(conn, settings, "testfeed")
    rx = FakeReceiver(tmp_path / "rec")
    return row, rx, feeds.FeedRunner(settings, row, receiver=rx, reader_factory=NoReader, **kw)


def _loud_moment(r, now):
    for i in range(240):
        r.level(now - 125 + i * 0.5, -30.0)
    for i in range(8):
        r.level(now - 3.5 + i * 0.5, -9.0)


def test_feed_spike_is_cut_from_the_buffer_and_edited(settings, conn, footage, tmp_path):
    row, rx, r = _feed_runner(settings, conn, footage, tmp_path, before_s=8, after_s=4, settle_s=0.5,
                              edit=lambda st, sess, mid, path: [99])
    now = time.time()
    make_buffer(rx.folder(row["broadcaster_id"]), footage, now - 30)
    assert r.poll() and r.s["status"] == "live"
    _loud_moment(r, now)
    mid = r.tick(now)
    assert mid and r.tick(now + 1) is None
    for t in r.captures:
        t.join(60)
    conn.rollback()
    m = conn.execute("SELECT * FROM live_moments WHERE id = %s", (mid,)).fetchone()
    assert m["status"] == "ready", m["status_reason"]
    assert m["clip_ids"] == [99] and m["signals"]["audio"]["rise_db"] > 15
    assert 9 <= m["timings"]["cut"]["duration_s"] <= 14 and "queued_s" in m["timings"]
    assert m["file_path"].endswith(f"moment_{mid}.mp4")


def test_feed_reconnect_within_grace_keeps_the_session_then_ends(settings, conn, footage, tmp_path):
    row, rx, r = _feed_runner(settings, conn, footage, tmp_path, grace_s=10)
    t = time.time()
    assert r.poll(t)
    rx.up = False
    r.poll(t + 2)
    assert r.s["status"] == "live" and r.tick(t + 2) is None       # dropped: no spikes meanwhile
    rx.up = True
    r.poll(t + 6)
    assert r.s["status"] == "live" and r.reconnects == 1 and r.s["detail"]["reconnects"] == 1
    rx.up = False
    r.poll(t + 7)
    r.poll(t + 18)
    assert r.s["status"] == "ended"
    with pytest.raises(Exception, match="live-only"):              # the database refuses new moments now
        with live.db.connect(settings) as c:
            live.record_moment(c, row["id"], {"score": 1.0}, kind="audio")


def test_one_shared_editing_slot_across_watchers(settings, conn):
    running, overlap, done = [], [], []
    lock = threading.Lock()

    def slow_edit(st, sess, mid, path):
        with lock:
            running.append(mid)
            if len(running) > 1:
                overlap.append(list(running))
        time.sleep(0.4)
        with lock:
            running.remove(mid)
        done.append(mid)
        return [mid]

    sid = conn.execute("""INSERT INTO live_sessions (platform, broadcaster_id, login, channel_key, rights_id, status)
                          VALUES ('feed', 'vp/x', 'x', 'en', %s, 'live') RETURNING *""",
                       (live.rights_for_session(conn, creator_id=None, own=True),)).fetchone()
    conn.commit()
    mids = [live.record_moment(conn, sid["id"], {"score": 0.9}, kind="audio") for _ in range(4)]
    threads = [threading.Thread(target=live.run_edit, args=(settings, sid, m, time.time(), Path("x.mp4"), {}, {}, 0.9),
                                kwargs={"min_combined": 0.0, "edit": slow_edit}) for m in mids]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert sorted(done) == sorted(mids) and overlap == []
    conn.rollback()
    waits = [r["timings"]["queued_s"] for r in conn.execute("SELECT timings FROM live_moments ORDER BY id").fetchall()]
    assert max(waits) >= 0.8                      # the last one waited for the others


def test_live_render_en_and_br_at_720p_with_one_encode_each(settings, conn, footage, monkeypatch):
    monkeypatch.setenv("VICEPULSE_LIVE_ENCODER", "libx264")
    _feed(conn, settings, also=["br"])
    s = feeds.start_session(conn, settings, "testfeed")
    conn.execute("UPDATE live_sessions SET status = 'live' WHERE id = %s", (s["id"],))
    conn.commit()
    mid = live.record_moment(conn, s["id"], {"score": 0.9}, kind="audio")
    dest = settings.dir("live", f"session_{s['id']}") / f"moment_{mid}.mp4"
    import shutil

    shutil.copy(footage, dest)
    t0 = time.time()
    ids = live.default_edit(settings, s, mid, dest)
    took = time.time() - t0
    conn.rollback()
    rows = conn.execute("""SELECT cl.id, ch.key, r.platform, r.width, r.height, r.file_path, r.file_sha256
                           FROM clips cl JOIN channels ch ON ch.id = cl.channel_id JOIN renders r ON r.clip_id = cl.id
                           WHERE cl.id = ANY(%s) ORDER BY cl.id, r.platform""", (ids,)).fetchall()
    assert {r["key"] for r in rows} == {"en", "br"}, rows
    assert all((r["width"], r["height"]) == (720, 1280) for r in rows)
    for cid in ids:                                # one encode per version, reused for all 3 platforms
        mine = [r for r in rows if r["id"] == cid]
        assert len(mine) == 3 and len({r["file_sha256"] for r in mine}) == 1
        assert len({os.stat(r["file_path"]).st_ino for r in mine}) == 1      # hard links, no extra disk
    ev = conn.execute("SELECT detail FROM events WHERE stage = 'render' ORDER BY id DESC LIMIT 1").fetchone()
    assert ev["detail"]["encodes"] == 1 and ev["detail"]["size"] == "720x1280"
    print(f"\nEN+BR live edit on this machine: {took:.1f} s")
