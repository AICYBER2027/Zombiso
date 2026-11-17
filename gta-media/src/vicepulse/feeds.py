"""Direct live feeds: creators send VicePulse a second copy of their live stream (an extra OBS output).

How it works, for one feed:
  1. The creator adds a second stream output in OBS: server ``rtmp://<vicepulse-host>:1935/vp`` and the stream
     key VicePulse gave them (``<feed>?user=<feed>&pass=<secret>``). Their normal YouTube stream is unchanged.
  2. MediaMTX (the receiver container) asks VicePulse whether the key is right (``check_publish``) and only then
     accepts the stream. It keeps a rolling buffer: 6-second recording chunks, deleted after a few minutes.
  3. VicePulse listens to the feed's audio only (no video decoding) and scores excitement: how far the
     loudness of the last seconds rises above the stream's own recent normal level, and for how long.
  4. On a spike it waits until 60 s after the moment, then cuts 20 s before to 60 s after out of the buffer
     (stream copy, no re-encode) and hands the file to the shared editing queue (vicepulse.live.run_edit),
     which runs the existing pipeline: rights gate, best 30-60 s, subtitles, 720x1280 render, final check.

Live-only: a moment is recorded only while the feed's session is live (the same database trigger as Twitch),
and the buffer only ever holds the last few minutes of the live feed, so there is no archive to clip from.
A short drop (OBS reconnecting) keeps the session live; a longer one ends it.
Nothing here publishes. Nothing here downloads from YouTube: the video comes from the creator's own encoder.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import subprocess
import threading
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from . import db, live
from .config import Settings

SLUG = re.compile(r"^[a-z0-9_-]{3,40}$")
PATH_PREFIX = "vp/"
SEGMENT = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6})\.(?:ts|mp4)$")


def _now() -> float:
    return time.time()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ---------------------------------------------------------------- feeds and stream keys

def add_feed(conn, settings: Settings, slug: str, display_name: str, channel_key: str, *,
             creator_id: int | None = None, own: bool = False, also_channels: list[str] | None = None) -> dict:
    """Register a feed. Returns the row plus the stream key, which is shown this once and stored only hashed."""
    slug = slug.strip().lower()
    if not SLUG.match(slug):
        raise ValueError("feed name: 3-40 lowercase letters, digits, - or _")
    channels = live.check_channels(settings, channel_key, also_channels)
    rid = live.rights_for_session(conn, creator_id=creator_id, own=own)
    secret = secrets.token_urlsafe(24)
    row = conn.execute(
        """INSERT INTO live_feeds (slug, display_name, channel_key, channels, creator_id, rights_id, key_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (slug, display_name, channel_key, channels, creator_id, rid, _hash(secret))).fetchone()
    db.log_event(conn, "feed", "live_feed", row["id"], "created", {"slug": slug, "channels": channels})
    conn.commit()
    return {**row, "obs": obs_settings(settings, slug, secret)}


def new_key(conn, slug: str, settings: Settings) -> dict:
    """Replace a feed's stream key (the old one stops working at the next connection)."""
    secret = secrets.token_urlsafe(24)
    row = conn.execute("UPDATE live_feeds SET key_hash = %s WHERE slug = %s RETURNING id", (_hash(secret), slug)).fetchone()
    if not row:
        raise ValueError(f"no feed called {slug}")
    db.log_event(conn, "feed", "live_feed", row["id"], "new_key")
    conn.commit()
    return obs_settings(settings, slug, secret)


def obs_settings(settings: Settings, slug: str, secret: str) -> dict:
    import os

    host = os.environ.get("VICEPULSE_FEED_HOST", "127.0.0.1")
    return {"server": f"rtmp://{host}:1935/vp", "stream_key": f"{slug}?user={slug}&pass={secret}",
            "note": "OBS > Settings > Stream is for YouTube; add this as a second output (e.g. the free "
                    "'Multiple RTMP outputs' plugin), so the YouTube stream is untouched."}


def check_publish(conn, body: dict) -> bool:
    """MediaMTX's question 'may this connection do this?'. Only publishing is checked; reads stay internal."""
    action, path = body.get("action"), str(body.get("path") or "")
    if action != "publish":
        return action in ("read", "playback", "api", "metrics", "pprof")   # never reachable from outside
    if not path.startswith(PATH_PREFIX):
        return False
    slug = path[len(PATH_PREFIX):]
    user, password = str(body.get("user") or ""), str(body.get("password") or "")
    if not password:                                  # some encoders only send the query string
        q = dict(p.split("=", 1) for p in str(body.get("query") or "").split("&") if "=" in p)
        user, password = q.get("user", ""), q.get("pass", "")
    row = conn.execute("SELECT id, key_hash, enabled FROM live_feeds WHERE slug = %s", (slug,)).fetchone()
    ok = bool(row and row["enabled"] and user == slug and hmac.compare_digest(row["key_hash"], _hash(password)))
    if row:
        conn.execute("UPDATE live_feeds SET last_auth_at = now(), last_auth_ok = %s WHERE id = %s", (ok, row["id"]))
        conn.commit()
    return ok


def start_session(conn, settings: Settings, slug: str) -> dict:
    feed = conn.execute("SELECT * FROM live_feeds WHERE slug = %s", (slug,)).fetchone()
    if not feed:
        raise ValueError(f"no feed called {slug} (add it with feed-add)")
    if not feed["enabled"]:
        raise ValueError(f"feed {slug} is switched off")
    row = conn.execute(
        """INSERT INTO live_sessions (platform, broadcaster_id, login, display_name, channel_key, channels, creator_id,
                                      rights_id, feed_id)
           VALUES ('feed', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (PATH_PREFIX + slug, slug, feed["display_name"], feed["channel_key"], feed["channels"], feed["creator_id"],
         feed["rights_id"], feed["id"])).fetchone()
    db.log_event(conn, "live", "live_session", row["id"], "watching", {"feed": slug})
    conn.commit()
    return row


# ---------------------------------------------------------------- the receiver (MediaMTX)

class Receiver:
    """MediaMTX's control API (internal network only): is a feed publishing right now?"""

    def __init__(self, api: str = "http://mediamtx:9997", rtsp: str = "rtsp://mediamtx:8554",
                 rec_dir: Path | str = "/data/feeds/rec", session: requests.Session | None = None):
        self.api, self.rtsp, self.rec_dir = api.rstrip("/"), rtsp.rstrip("/"), Path(rec_dir)
        self.http = session or requests.Session()

    def ready(self, path: str) -> bool:
        r = self.http.get(f"{self.api}/v3/paths/list", params={"itemsPerPage": 1000}, timeout=10)
        r.raise_for_status()
        return any(p.get("name") == path and p.get("ready") for p in r.json().get("items") or [])

    def audio_url(self, path: str) -> str:
        return f"{self.rtsp}/{path}"

    def folder(self, path: str) -> Path:
        return self.rec_dir / path


# ---------------------------------------------------------------- audio excitement (pure, testable)

@dataclass
class AudioSpikeDetector:
    """Loudness of the last few seconds against the stream's own recent normal. add() levels, evaluate() each second."""

    short_s: float = 3.0          # "right now"
    base_s: float = 120.0         # recent normal before it
    warmup_s: float = 30.0
    min_rise_db: float = 8.0      # must be at least this much louder than normal
    floor_db: float = -45.0       # and actually audible
    threshold: float = 0.55
    cooldown_s: float = 45.0
    started: float | None = None
    last_trigger: float = -1e9
    levels: deque = field(default_factory=deque)

    def add(self, t: float, level_db: float) -> None:
        if self.started is None:
            self.started = t
        self.levels.append((t, level_db))

    def stats(self, now: float) -> dict:
        while self.levels and self.levels[0][0] < now - self.base_s - self.short_s:
            self.levels.popleft()
        short = [v for t, v in self.levels if now - self.short_s < t <= now]
        base = sorted(v for t, v in self.levels if t <= now - self.short_s)
        if not short:
            return {"level_db": None, "baseline_db": None, "rise_db": 0.0, "sustained": 0.0, "score": 0.0}
        level = 10 * math.log10(sum(10 ** (v / 10) for v in short) / len(short))
        baseline = base[len(base) // 2] if base else level
        rise = level - baseline
        sustained = sum(1 for v in short if v > baseline + 0.75 * self.min_rise_db) / len(short)
        score = 0.6 * min(max((rise - self.min_rise_db / 2) / self.min_rise_db, 0.0), 1.0) + 0.4 * sustained
        return {"level_db": round(level, 1), "baseline_db": round(baseline, 1), "rise_db": round(rise, 1),
                "sustained": round(sustained, 2), "score": round(score, 3)}

    def evaluate(self, now: float) -> dict | None:
        st = self.stats(now)
        if self.started is None or now - self.started < self.warmup_s or st["level_db"] is None:
            return None
        hot = st["level_db"] >= self.floor_db and st["rise_db"] >= self.min_rise_db and st["score"] >= self.threshold
        if not hot:
            return None
        if now - self.last_trigger < self.cooldown_s:
            return {**st, "merge": True}
        self.last_trigger = now
        return st


class AudioReader(threading.Thread):
    """Reads a feed's audio (mono 16 kHz, no video decoding) and calls on_level(t, dBFS) every window_s."""

    def __init__(self, url: str, on_level: Callable[[float, float], None], *, window_s: float = 0.5):
        super().__init__(daemon=True, name=f"audio-{url.rsplit('/', 1)[-1]}")
        self.url, self.on_level, self.window_s = url, on_level, window_s
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.restarts = 0

    def run(self) -> None:
        chunk = int(16000 * self.window_s) * 2
        backoff = 1.0
        while not self.stop.is_set():
            args = ["ffmpeg", "-nostdin", "-v", "error"]
            if self.url.startswith("rtsp://"):
                args += ["-rtsp_transport", "tcp"]
            args += ["-i", self.url, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
            try:
                self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                while not self.stop.is_set():
                    data = self.proc.stdout.read(chunk)
                    if not data or len(data) < 2:
                        break
                    s = array("h", data[: len(data) // 2 * 2])
                    rms = math.sqrt(sum(x * x for x in s) / len(s)) / 32768.0
                    self.on_level(_now(), 20 * math.log10(max(rms, 1e-6)))
                    backoff = 1.0
            except OSError:
                pass
            finally:
                self._kill()
            if not self.stop.is_set():
                self.restarts += 1
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def close(self) -> None:
        self.stop.set()
        self._kill()


# ---------------------------------------------------------------- rolling buffer -> moment file

def segments(folder: Path) -> list[tuple[float, Path]]:
    """Recorded chunks in the buffer folder, oldest first, with their start time (UTC, from the file name)."""
    out = []
    if folder.is_dir():
        for p in folder.iterdir():
            m = SEGMENT.search(p.name)
            if m:
                t = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S-%f").replace(tzinfo=timezone.utc).timestamp()
                out.append((t, p))
    return sorted(out)


def cut_window(folder: Path, t0: float, t1: float, out: Path) -> dict:
    """Cut [t0, t1] (wall clock) out of the buffer into one MP4, without re-encoding."""
    from .media import probe, run

    segs = segments(folder)
    ends = [segs[i + 1][0] for i in range(len(segs) - 1)]
    if segs:                                  # the newest chunk may still be growing: measure what is there
        try:
            ends.append(segs[-1][0] + probe(segs[-1][1])["duration"])
        except Exception:
            ends.append(segs[-1][0] + 6.0)
    picked = [(t, p) for (t, p), e in zip(segs, ends) if t < t1 and e > t0]
    if not picked:
        raise ValueError("the buffer has no video for this moment (feed dropped, or the moment is too old)")
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for _, p in picked), encoding="utf-8")
    offset = max(0.0, t0 - picked[0][0])
    length = t1 - max(t0, picked[0][0])
    run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-f", "concat", "-safe", "0", "-ss", f"{offset:.3f}", "-i", str(lst),
         "-t", f"{length:.3f}", "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(out)])
    lst.unlink(missing_ok=True)
    info = probe(out)
    return {"segments": len(picked), "buffer_start_gap_s": round(max(0.0, picked[0][0] - t0), 1),
            "duration_s": round(info["duration"], 1), "size": f"{info['width']}x{info['height']}"}


# ---------------------------------------------------------------- one feed's watcher

class FeedRunner:
    """Watches one feed: publishing state (with reconnect grace), audio spikes, buffer cuts, shared-queue edits."""

    def __init__(self, settings: Settings, session_row: dict, *, receiver: Receiver | None = None,
                 detector: AudioSpikeDetector | None = None, before_s: float = 20.0, after_s: float = 60.0,
                 settle_s: float = 3.0, grace_s: float = 30.0, poll_s: float = 2.0, min_score: float = 0.0,
                 reader_factory=AudioReader, edit: Callable | None = None):
        self.settings, self.s = settings, dict(session_row)
        self.rx = receiver or Receiver()
        self.detector = detector or AudioSpikeDetector()
        self.before_s, self.after_s, self.settle_s = before_s, after_s, settle_s
        self.grace_s, self.poll_s, self.min_score = grace_s, poll_s, min_score
        self.reader_factory, self.reader = reader_factory, None
        self.edit = edit or live.default_edit
        self.lock = threading.Lock()
        self.captures: list[threading.Thread] = []
        self.last_moment: int | None = None
        self.dropped_at: float | None = None
        self.reconnects = 0
        self.stop = threading.Event()
        self.path = self.s["broadcaster_id"]

    def _update(self, **cols) -> None:
        with db.connect(self.settings) as conn:
            sets = ", ".join(f"{k} = %s" for k in cols)
            conn.execute(f"UPDATE live_sessions SET {sets} WHERE id = %s",
                         (*[db.Jsonb(v) if isinstance(v, dict) else v for v in cols.values()], self.s["id"]))
            conn.commit()
        self.s.update(cols)

    def level(self, t: float, level_db: float) -> None:
        with self.lock:
            self.detector.add(t, level_db)

    def _start_reader(self) -> None:
        if self.reader:
            self.reader.close()
        self.reader = self.reader_factory(self.rx.audio_url(self.path), self.level)
        self.reader.start()

    def poll(self, now: float | None = None) -> bool:
        now = now or _now()
        ready = self.rx.ready(self.path)
        stamp = datetime.now(timezone.utc)
        if ready and self.s["status"] != "live":
            self.detector = AudioSpikeDetector(**{k: getattr(self.detector, k) for k in (
                "short_s", "base_s", "warmup_s", "min_rise_db", "floor_db", "threshold", "cooldown_s")})
            self.dropped_at, self.reconnects = None, 0
            self._update(status="live", started_at=stamp, last_seen_at=stamp, ended_at=None,
                         stream_title="direct feed")
            self._start_reader()
        elif ready:
            if self.dropped_at is not None:          # OBS reconnected within the grace period: same session
                self.dropped_at = None
                self.reconnects += 1
                self._update(detail={**(self.s.get("detail") or {}), "reconnects": self.reconnects})
            if self.reader is None or not self.reader.is_alive():
                self._start_reader()
            self._update(last_seen_at=stamp)
        elif self.s["status"] == "live":
            if self.dropped_at is None:
                self.dropped_at = now
            elif now - self.dropped_at >= self.grace_s:
                self.end_live()
        return ready

    def end_live(self) -> None:
        self._update(status="ended", ended_at=datetime.now(timezone.utc))
        if self.reader:
            self.reader.close()
            self.reader = None

    def tick(self, now: float | None = None) -> int | None:
        if self.s["status"] != "live" or self.dropped_at is not None:
            return None
        now = now or _now()
        with self.lock:
            spike = self.detector.evaluate(now)
            st = self.detector.stats(now)
        # The Live page shows the audio level (dBFS) in the chat columns for feeds.
        self._update(chat_rate=st["level_db"], chat_baseline=st["baseline_db"], last_score=st["score"])
        if not spike:
            return None
        if spike.get("merge"):
            if self.last_moment:
                with db.connect(self.settings) as conn:
                    conn.execute("""UPDATE live_moments SET score = GREATEST(score, %s),
                                    signals = jsonb_set(signals, '{merged}', to_jsonb(COALESCE((signals->>'merged')::int, 0) + 1))
                                    WHERE id = %s""", (spike["score"], self.last_moment))
                    conn.commit()
            return None
        try:
            with db.connect(self.settings) as conn:
                mid = live.record_moment(conn, self.s["id"], spike, kind="audio")
        except Exception as ex:
            if "live-only" in str(ex):
                return None
            raise
        self.last_moment = mid
        t = threading.Thread(target=self.capture, args=(mid, now), daemon=True, name=f"capture-{mid}")
        self.captures.append(t)
        t.start()
        return mid

    def capture(self, mid: int, detected: float) -> None:
        # The loud part started up to short_s before detection; the window is anchored there.
        moment = detected - self.detector.short_s
        timings: dict = {"plan": {"before_s": self.before_s, "after_s": self.after_s}}
        folder = self.settings.dir("live", f"session_{self.s['id']}")
        try:
            live._set(self.settings, mid, "capturing")
            end = moment + self.after_s
            # Wait for the "after" part to be recorded; stop early if the feed ended (keep what was recorded).
            while _now() < end + self.settle_s and not self.stop.is_set():
                if self.s["status"] != "live":
                    timings["after"] = "feed ended during the after part; kept what was recorded"
                    break
                self.stop.wait(0.5)
            t1 = min(end, _now())
            timings["cut"] = cut_window(self.rx.folder(self.path), moment - self.before_s, t1,
                                        folder / f"moment_{mid}.mp4")
            timings["captured_s"] = round(_now() - detected, 1)
            path = folder / f"moment_{mid}.mp4"
            live._set(self.settings, mid, "editing", file_path=str(path), captured_at=datetime.now(timezone.utc),
                      timings=timings)
        except Exception as ex:
            live._set(self.settings, mid, "failed", f"capture: {ex}"[:300], timings=timings)
            return
        with db.connect(self.settings) as conn:
            sig = conn.execute("SELECT signals FROM live_moments WHERE id = %s", (mid,)).fetchone()["signals"]
        score = float(sig.get("audio", {}).get("score", 0))
        live.run_edit(self.settings, self.s, mid, detected, path, timings, {**sig, "combined": score}, score,
                      min_combined=self.min_score, edit=self.edit)

    def run(self, *, until_end: bool = False, max_hours: float = 24) -> dict:
        stop_at = _now() + max_hours * 3600
        next_poll, seen_live = 0.0, False
        try:
            while not self.stop.is_set() and _now() < stop_at:
                if _now() >= next_poll:
                    try:
                        seen_live = self.poll() or seen_live
                    except requests.RequestException:
                        pass                       # receiver restarting: keep the current state
                    next_poll = _now() + self.poll_s
                    if until_end and seen_live and self.s["status"] == "ended":
                        break
                self.tick()
                self.stop.wait(1.0)
        finally:
            if self.s["status"] == "live" and self.stop.is_set():
                self._update(status="stopped", ended_at=datetime.now(timezone.utc))
            if self.reader:
                self.reader.close()
            for t in self.captures:
                t.join(timeout=600)
        with db.connect(self.settings) as conn:
            rows = conn.execute("SELECT status, count(*) AS n FROM live_moments WHERE session_id = %s GROUP BY 1",
                                (self.s["id"],)).fetchall()
        return {"session": self.s["id"], "feed": self.s["login"], "moments": {r["status"]: r["n"] for r in rows}}
