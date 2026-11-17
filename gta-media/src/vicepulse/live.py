"""Live-only clipping for Twitch, through Twitch's official API.

How it works, for one watched broadcaster:
  1. Every few seconds Helix ``streams`` says whether the broadcaster is live. The session row follows
     (waiting -> live -> ended).
  2. While live, the public chat is read (anonymous IRC, read-only) and a spike detector compares the chat
     speed of the last few seconds with its own recent baseline, plus hype words, laughter and repeated
     messages ("KEKW", "kkkk", the same message spammed).
  3. On a spike, VicePulse asks Twitch for clips (Create Clip; Twitch only allows it while the stream is live).
     One Twitch clip is at most 60 s and ends ~5 s after the request, so the 20 s before + 60 s after segment
     is two clips taken ~27 s and ~47 s after the spike (60 s and 25 s long), joined on their 5 s overlap by
     matching the audio. If the stream ends before the second clip, the first one is used on its own.
  4. The clip file is fetched with Get Clips Download. Twitch only allows this when VicePulse's Twitch account
     is an editor of that channel, so the broadcaster's own setting is the permission check.
  5. The clip's own audio is scored (loudness jump), so a moment is never kept on chat alone; then the
     existing pipeline runs (rights gate, best 30-60 s, Whisper subtitles, vertical render, final check) and
     the clip lands in the review queue. Monitoring keeps running while clips are edited.

Live-only: a moment can only be recorded while its session is live (also enforced by a database trigger),
Twitch refuses Create Clip for offline channels, and there is no path that reads a VOD or archived stream.
A moment detected while live may finish downloading and editing after the stream ends.
Nothing here publishes. Publishing stays behind VICEPULSE_ALLOW_LIVE_PUBLISH and human approval.
"""

from __future__ import annotations

import math
import random
import re
import socket
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from . import accounts, db, ingest, signals
from .config import Settings

HELIX = "https://api.twitch.tv/helix"
TWITCH_TAIL_S = 5.0        # a Twitch clip ends about this long after the Create Clip request (Twitch docs)
HYPE = re.compile(r"^(?:kekw|lulw?|omegalul|lmf?ao+|lo+l+|xd+|pog+(?:champ|gers|u)?|w+|gg+|wtf+|monkas|holy|clip+(?:it)?|"
                  r"clipa+|clipou|k{3,}|(?:ha){2,}h?|(?:rs){2,}|caralho|nossa+|meu+deus|mds+|vish+|eita+|kkkk+)$", re.I)
WORD = re.compile(r"[\w']+", re.U)


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------- spike detection (pure, testable)

@dataclass
class SpikeDetector:
    """Chat speed against its own baseline + hype share. Feed messages with add(), call evaluate() each second."""

    short_s: float = 6.0          # "right now" window
    base_s: float = 120.0         # baseline window before it
    warmup_s: float = 45.0        # need this much chat history first
    min_rate: float = 0.8         # messages per second in the short window
    min_ratio: float = 2.0        # short rate must be at least this x baseline
    threshold: float = 0.55
    cooldown_s: float = 45.0      # spikes closer than this merge into the previous moment
    started: float | None = None
    last_trigger: float = -1e9
    events: deque = field(default_factory=deque)

    def add(self, t: float, text: str) -> None:
        if self.started is None:
            self.started = t
        self.events.append((t, text))

    def stats(self, now: float) -> dict:
        while self.events and self.events[0][0] < now - self.base_s - self.short_s:
            self.events.popleft()
        short = [txt for t, txt in self.events if now - self.short_s < t <= now]
        base_n = sum(1 for t, _ in self.events if now - self.base_s - self.short_s < t <= now - self.short_s)
        covered = min(self.base_s, max(1.0, now - (self.started or now) - self.short_s))
        base_rate = base_n / covered
        rate = len(short) / self.short_s
        lam = base_rate * self.short_s
        z = (len(short) - lam) / math.sqrt(max(lam, 1.0))
        norm = [" ".join(WORD.findall(s.lower()))[:60] for s in short]
        repeats = Counter(norm)
        hype = sum(1 for s, n in zip(short, norm)
                   if repeats[n] >= 3 or any(HYPE.match(w) for w in WORD.findall(s)))
        hype_share = hype / len(short) if short else 0.0
        score = 0.55 * min(max(z, 0.0) / 6.0, 1.0) + 0.45 * hype_share
        return {"rate": round(rate, 2), "baseline": round(base_rate, 3), "z": round(z, 2),
                "hype_share": round(hype_share, 2), "score": round(score, 3), "messages": len(short)}

    def evaluate(self, now: float) -> dict | None:
        """A spike dict when a new moment starts; {"merge": True, ...} for a spike inside the cooldown."""
        st = self.stats(now)
        if self.started is None or now - self.started < self.warmup_s:
            return None
        hot = (st["rate"] >= self.min_rate and st["score"] >= self.threshold
               and st["rate"] >= self.min_ratio * max(st["baseline"], 0.05))
        if not hot:
            return None
        if now - self.last_trigger < self.cooldown_s:
            return {**st, "merge": True}
        self.last_trigger = now
        return st


# ---------------------------------------------------------------- Twitch chat (anonymous, read-only)

class TwitchChat(threading.Thread):
    """Reads one channel's public chat as an anonymous viewer and calls on_message(t, text)."""

    def __init__(self, login: str, on_message: Callable[[float, str], None], *, host: str = "irc.chat.twitch.tv",
                 port: int = 6667):
        super().__init__(daemon=True, name=f"chat-{login}")
        self.login, self.on_message, self.host, self.port = login.lower(), on_message, host, port
        self.stop = threading.Event()
        self.connected = False
        self.error: str | None = None

    def run(self) -> None:
        backoff = 2
        while not self.stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=30) as s:
                    s.settimeout(1.0)
                    s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{self.login}\r\n".encode())
                    self.connected, self.error, backoff = True, None, 2
                    buf, last_data = b"", _now()
                    while not self.stop.is_set():
                        try:
                            chunk = s.recv(65536)
                        except socket.timeout:
                            if _now() - last_data > 360:          # Twitch pings about every 5 minutes
                                raise ConnectionError("chat went quiet")
                            continue
                        if not chunk:
                            raise ConnectionError("chat closed the connection")
                        last_data = _now()
                        buf += chunk
                        *lines, buf = buf.split(b"\r\n")
                        for raw in lines:
                            line = raw.decode("utf-8", "replace")
                            if line.startswith("PING"):
                                s.sendall(b"PONG :tmi.twitch.tv\r\n")
                            elif " PRIVMSG #" in line:
                                self.on_message(_now(), line.split(" :", 1)[1] if " :" in line else "")
            except (OSError, ConnectionError) as ex:
                self.connected, self.error = False, f"{type(ex).__name__}: {ex}"[:200]
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------- Twitch Helix calls

class TwitchLive:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        cid, sec = accounts.twitch_app()
        if not (cid and sec):
            raise ValueError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set in .env")
        self.settings, self.cid, self.sec = settings, cid, sec
        self.http = session or requests.Session()
        self._app: tuple[str, float] | None = None

    def _app_token(self) -> str:
        if not self._app or _now() > self._app[1] - 60:
            r = self.http.post(accounts.TW_TOKEN_URL, data={"client_id": self.cid, "client_secret": self.sec,
                                                            "grant_type": "client_credentials"}, timeout=30)
            r.raise_for_status()
            b = r.json()
            self._app = (b["access_token"], _now() + float(b.get("expires_in", 3600)))
        return self._app[0]

    def _h(self, token: str) -> dict:
        return {"Client-Id": self.cid, "Authorization": f"Bearer {token}"}

    def user(self, login: str) -> dict | None:
        r = self.http.get(f"{HELIX}/users", params={"login": login.lower()}, headers=self._h(self._app_token()), timeout=30)
        r.raise_for_status()
        d = r.json().get("data") or []
        return d[0] if d else None

    def stream(self, broadcaster_id: str) -> dict | None:
        r = self.http.get(f"{HELIX}/streams", params={"user_id": broadcaster_id, "type": "live"},
                          headers=self._h(self._app_token()), timeout=30)
        r.raise_for_status()
        d = r.json().get("data") or []
        return d[0] if d and d[0].get("type") == "live" else None

    def create_clip(self, broadcaster_id: str, *, duration: float = 60, title: str | None = None) -> str:
        _, tok = accounts.twitch_user_token(self.settings.data_dir, self.http)
        params = {"broadcaster_id": broadcaster_id, "duration": f"{max(5.0, min(60.0, duration)):.1f}"}
        if title:
            params["title"] = title[:100]
        r = self.http.post(f"{HELIX}/clips", params=params, headers=self._h(tok), timeout=30)
        if r.status_code != 202:
            raise ValueError(f"Twitch would not make the clip ({r.status_code}): {_msg(r)}")
        return r.json()["data"][0]["id"]

    def wait_clip(self, clip_id: str, timeout_s: float = 60, every_s: float = 3) -> dict:
        _, tok = accounts.twitch_user_token(self.settings.data_dir, self.http)
        end = _now() + timeout_s
        while True:
            r = self.http.get(f"{HELIX}/clips", params={"id": clip_id}, headers=self._h(tok), timeout=30)
            d = (r.json().get("data") or []) if r.status_code == 200 else []
            if d:
                return d[0]
            if _now() > end:
                raise ValueError("Twitch did not finish the clip within 60 s")
            time.sleep(every_s)

    def download_url(self, broadcaster_id: str, clip_id: str) -> str:
        uid, tok = accounts.twitch_user_token(self.settings.data_dir, self.http)
        r = self.http.get(f"{HELIX}/clips/downloads", params={"editor_id": uid, "broadcaster_id": broadcaster_id,
                                                              "clip_id": clip_id}, headers=self._h(tok), timeout=30)
        if r.status_code == 403:
            raise ValueError("VicePulse is not an editor of this channel; ask the streamer to add VicePulse's "
                             "Twitch account as an editor (Creator dashboard > Community > Roles manager)")
        if r.status_code != 200:
            raise ValueError(f"Twitch would not give the clip file ({r.status_code}): {_msg(r)}")
        d = (r.json().get("data") or [{}])[0]
        url = d.get("landscape_download_url") or d.get("portrait_download_url")
        if not url:
            raise ValueError("Twitch gave no download link for this clip (is VicePulse an editor of the channel?)")
        return url

    def fetch(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        with self.http.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(dest)
        return dest


def _msg(r) -> str:
    try:
        return str(r.json().get("message") or r.text[:200])
    except ValueError:
        return r.text[:200]


# ---------------------------------------------------------------- sessions and moments

def rights_for_session(conn, *, creator_id: int | None, own: bool) -> int:
    """The rights record that covers this broadcast: an authorized creator, or the owner's own stream."""
    if own:
        from .autosource import rights_for_file
        rid = rights_for_file(conn, "own")
        conn.commit()
        return rid
    if not creator_id:
        raise ValueError("say whose stream this is: --creator-id (an authorized creator) or --own")
    cr = conn.execute("SELECT display_name, permission_status, rights_id FROM creators WHERE id = %s",
                      (creator_id,)).fetchone()
    if cr is None:
        raise ValueError(f"creator {creator_id} not found")
    if cr["permission_status"] != "authorized" or not cr["rights_id"]:
        raise ValueError(f"{cr['display_name']} has no recorded permission yet; authorize them on the Creators tab "
                         "with the proof first")
    return cr["rights_id"]


def check_channels(settings: Settings, channel_key: str, also: list[str] | None) -> list[str]:
    known = {c["key"] for c in settings.channels()}
    for ch in [channel_key, *(also or [])]:
        if ch not in known:
            raise ValueError(f"unknown VicePulse channel {ch!r}")
    return [ch for ch in dict.fromkeys([channel_key, *(also or [])])]


def start_session(conn, settings: Settings, tw: TwitchLive, login: str, channel_key: str, *,
                  creator_id: int | None = None, own: bool = False, also_channels: list[str] | None = None) -> dict:
    channels = check_channels(settings, channel_key, also_channels)
    login = login.strip().lower().removeprefix("https://www.twitch.tv/").removeprefix("twitch.tv/").strip("/")
    if not re.fullmatch(r"\w{3,25}", login):
        raise ValueError("give the Twitch login (the name in twitch.tv/<login>)")
    rid = rights_for_session(conn, creator_id=creator_id, own=own)
    u = tw.user(login)
    if not u:
        raise ValueError(f"no Twitch channel called {login}")
    row = conn.execute(
        """INSERT INTO live_sessions (platform, broadcaster_id, login, display_name, channel_key, channels, creator_id,
                                      rights_id)
           VALUES ('twitch', %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (u["id"], u["login"], u.get("display_name"), channel_key, channels, creator_id, rid)).fetchone()
    db.log_event(conn, "live", "live_session", row["id"], "watching", {"login": u["login"], "channel": channel_key})
    conn.commit()
    return row


def record_moment(conn, session_id: int, spike: dict, *, kind: str = "chat") -> int:
    """Insert a moment; the database refuses it unless the session is live right now."""
    mid = conn.execute("INSERT INTO live_moments (session_id, score, signals) VALUES (%s, %s, %s) RETURNING id",
                       (session_id, spike["score"], db.Jsonb({kind: spike}))).fetchone()["id"]
    conn.commit()
    return mid


# ---------------------------------------------------------------- one shared editing queue

EDIT_LOCK_KEY = 7_406_771       # any fixed number; every VicePulse process uses the same one


@contextmanager
def edit_slot(settings: Settings):
    """Hold the single editing slot shared by every watcher (Twitch and direct feeds, in any process).

    A Postgres advisory lock: waiters are served in arrival order and the slot frees itself if a process dies,
    so two heavy edits never run at once on this PC. Yields how many seconds were spent waiting.
    """
    t0 = _now()
    conn = db.connect(settings)
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (EDIT_LOCK_KEY,))
        conn.commit()
        yield round(_now() - t0, 1)
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (EDIT_LOCK_KEY,))
            conn.commit()
        finally:
            conn.close()


def run_edit(settings: Settings, session: dict, mid: int, detected: float, path: Path, timings: dict, sig: dict,
             combined: float, *, min_combined: float, edit: Callable) -> list[int] | None:
    """Score gate -> wait for the shared editing slot -> edit -> mark ready (or skipped / failed)."""
    try:
        if combined < min_combined:
            _set(settings, mid, "skipped", f"not exciting enough (score {combined})", signals=sig, timings=timings)
            return None
        with edit_slot(settings) as waited:
            timings["queued_s"] = waited
            t_edit = _now()
            clip_ids = edit(settings, session, mid, path)
            timings["edit_only_s"] = round(_now() - t_edit, 1)
        timings["edited_s"] = round(_now() - detected, 1)
        if not clip_ids:
            _set(settings, mid, "skipped", "the editor found no usable moment (or it duplicates an earlier clip)",
                 signals=sig, timings=timings)
            return None
        latency = round(_now() - detected, 1)
        _set(settings, mid, "ready", signals=sig, timings=timings, clip_ids=clip_ids,
             ready_at=datetime.now(timezone.utc), latency_s=latency)
        notify_ready(settings, session, clip_ids, latency, combined)
        return clip_ids
    except Exception as ex:
        _set(settings, mid, "failed", f"editing: {ex}"[:300], timings=timings)
        return None


def notify_ready(settings: Settings, session: dict, clip_ids: list[int], latency: float, score: float) -> None:
    from . import notify

    if not notify.enabled():
        return
    try:
        with db.connect(settings) as conn:
            r = conn.execute("""SELECT r.file_path FROM renders r WHERE r.clip_id = %s
                                ORDER BY (r.platform = 'youtube') DESC LIMIT 1""", (clip_ids[0],)).fetchone()
        video = Path(r["file_path"]) if r else None
        notify.send(f"New VicePulse {session['channel_key'].upper()} clip from {session['display_name'] or session['login']} "
                    f"(live). Ready {latency:.0f} s after the moment, score {score}. Approve it on the review page.",
                    video)
    except Exception:
        pass                                   # a notification must never break clipping


def _set(settings: Settings, mid: int, status: str, reason: str | None = None, **cols) -> None:
    with db.connect(settings) as conn:
        sets = "".join(f", {k} = %s" for k in cols)
        conn.execute(f"UPDATE live_moments SET status = %s, status_reason = %s{sets} WHERE id = %s",
                     (status, reason, *[db.Jsonb(v) if isinstance(v, dict) else v for v in cols.values()], mid))
        db.log_event(conn, "live", "live_moment", mid, status, {"reason": reason} if reason else None)
        conn.commit()


def find_join(a: Path, b: Path, *, expected_overlap_s: float, search_s: float = 4.0) -> tuple[float, str]:
    """Where in clip A clip B starts (seconds from A's start), found by matching B's first seconds in A's audio.

    Twitch clip timing drifts by a second or two, so the expected overlap is only a starting guess. Falls back to
    the guess when the audio is silent, numpy is missing or the match is weak.
    """
    from .media import probe

    a_dur = probe(a)["duration"]
    guess = max(0.0, a_dur - expected_overlap_s)
    try:
        import numpy as np

        sr = 16000
        tail_start = max(0.0, guess - search_s)
        tail = signals.decode_audio_16k(a, start=tail_start, duration=a_dur - tail_start)
        head = signals.decode_audio_16k(b, duration=min(2.0, expected_overlap_s))
        if len(head) < sr // 2 or len(tail) < len(head) or float(np.std(head)) < 1e-3:
            return guess, "expected overlap (no usable audio to match)"
        n = len(tail) + len(head)
        size = 1 << (n - 1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(tail, size) * np.conj(np.fft.rfft(head, size)), size)[: len(tail) - len(head) + 1]
        energy = np.sqrt(np.convolve(tail ** 2, np.ones(len(head)), "valid")) * np.linalg.norm(head) + 1e-9
        norm = corr / energy
        lag = int(np.argmax(norm))
        if norm[lag] < 0.5:
            return guess, f"expected overlap (weak audio match {norm[lag]:.2f})"
        return tail_start + lag / sr, f"audio match {norm[lag]:.2f}"
    except ImportError:
        return guess, "expected overlap (numpy not installed)"


def stitch(a: Path, b: Path, out: Path, *, expected_overlap_s: float) -> dict:
    """Join two consecutive Twitch clips into one segment: A up to where B starts, then all of B."""
    from .media import probe, run

    cut, how = find_join(a, b, expected_overlap_s=expected_overlap_s)
    info = probe(a)
    w, h = info["width"], info["height"]
    graph = (f"[0:v]trim=0:{cut:.3f},setpts=PTS-STARTPTS,fps=30,format=yuv420p[v0];"
             f"[0:a]atrim=0:{cut:.3f},asetpts=PTS-STARTPTS,aresample=48000[a0];"
             f"[1:v]scale={w}:{h},setsar=1,fps=30,format=yuv420p[v1];[1:a]aresample=48000[a1];"
             "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(a), "-i", str(b), "-filter_complex", graph,
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
         "-b:a", "160k", "-movflags", "+faststart", str(out)])
    return {"cut_at_s": round(cut, 2), "join": how, "duration_s": round(probe(out)["duration"], 1)}


def audio_excitement(path: Path) -> float:
    """0..1: how far the loudest moments rise above the clip's typical level (0 if there is no audio)."""
    curve = [v for _, v in signals.loudness_curve(path) if v > -70]
    if len(curve) < 20:
        return 0.0
    curve.sort()
    median, p95 = curve[len(curve) // 2], curve[int(len(curve) * 0.95)]
    return round(min(max((p95 - median) / 12.0, 0.0), 1.0), 3)


class LiveRunner:
    """Watches one session: live status, chat, spikes, captures (threads) and edits (one at a time)."""

    def __init__(self, settings: Settings, session_row: dict, *, tw: TwitchLive | None = None,
                 detector: SpikeDetector | None = None, before_s: float = 20.0, after_s: float = 60.0,
                 chat_latency_s: float = 8.0, overlap_s: float = 5.0, call_a_s: float | None = None,
                 call_b_s: float | None = None, clip_seconds: float = 45,
                 min_combined: float = 0.45, poll_s: float = 15.0, chat_factory=TwitchChat,
                 edit: Callable[[Settings, dict, int, Path], list[int]] | None = None):
        self.settings, self.s = settings, dict(session_row)
        self.tw = tw or TwitchLive(settings)
        self.detector = detector or SpikeDetector()
        self.clip_seconds, self.min_combined, self.poll_s = clip_seconds, min_combined, poll_s
        # A Twitch clip is at most 60 s and ends about TWITCH_TAIL_S after the request, so the 20 s before + 60 s
        # after segment is two clips: A = [-before, 60 - before], B = [60 - before - overlap, +after], joined on
        # the overlap. The moment itself happened about chat_latency_s before chat reacted.
        tail = TWITCH_TAIL_S
        first_end = 60.0 - before_s
        self.call_a_s = call_a_s if call_a_s is not None else max(0.0, first_end - tail - chat_latency_s)
        self.call_b_s = call_b_s if call_b_s is not None else max(self.call_a_s, after_s - tail - chat_latency_s)
        self.dur_b = max(5.0, min(60.0, after_s - first_end + overlap_s)) if after_s > first_end else 0.0
        self.overlap_s = overlap_s
        self.chat_factory, self.chat = chat_factory, None
        self.edit = edit or default_edit
        self.edits = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edit")
        self.captures: list[threading.Thread] = []
        self.lock = threading.Lock()
        self.last_moment: int | None = None
        self.stop = threading.Event()

    # -- status
    def _update_session(self, **cols) -> None:
        with db.connect(self.settings) as conn:
            sets = ", ".join(f"{k} = %s" for k in cols)
            conn.execute(f"UPDATE live_sessions SET {sets} WHERE id = %s", (*cols.values(), self.s["id"]))
            conn.commit()
        self.s.update(cols)

    def poll_live(self) -> bool:
        st = self.tw.stream(self.s["broadcaster_id"])
        now = datetime.now(timezone.utc)
        if st and self.s["status"] != "live":
            self._update_session(status="live", stream_id=st["id"], stream_title=st.get("title"),
                                 started_at=st.get("started_at"), last_seen_at=now, ended_at=None)
            self.detector = SpikeDetector(**{k: getattr(self.detector, k) for k in (
                "short_s", "base_s", "warmup_s", "min_rate", "min_ratio", "threshold", "cooldown_s")})
            self.chat = self.chat_factory(self.s["login"], self.detector_add)
            self.chat.start()
        elif st:
            self._update_session(last_seen_at=now, stream_title=st.get("title"))
        elif self.s["status"] == "live":
            self.end_live()
        return bool(st)

    def end_live(self) -> None:
        self._update_session(status="ended", ended_at=datetime.now(timezone.utc))
        if self.chat:
            self.chat.stop.set()
            self.chat = None

    def detector_add(self, t: float, text: str) -> None:
        with self.lock:
            self.detector.add(t, text)

    # -- spikes
    def tick(self, now: float | None = None) -> int | None:
        if self.s["status"] != "live":
            return None
        now = now or _now()
        with self.lock:
            spike = self.detector.evaluate(now)
            st = self.detector.stats(now)
        self._update_session(chat_rate=st["rate"], chat_baseline=st["baseline"], last_score=st["score"])
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
                mid = record_moment(conn, self.s["id"], spike)
        except Exception as ex:          # the live-only trigger refused it
            if "live-only" in str(ex):
                return None
            raise
        self.last_moment = mid
        t = threading.Thread(target=self.capture, args=(mid, now), daemon=True, name=f"capture-{mid}")
        self.captures.append(t)
        t.start()
        return mid

    # -- capture -> download -> edit
    def _at(self, t: float) -> bool:
        """Wait until wall time t; False if the watcher was stopped meanwhile."""
        wait = t - _now()
        return not (wait > 0 and self.stop.wait(wait))

    def capture(self, mid: int, detected: float) -> None:
        timings: dict = {"plan": {"clip_a_at_s": self.call_a_s, "clip_b_at_s": self.call_b_s, "clip_b_len_s": self.dur_b}}
        bid = self.s["broadcaster_id"]
        folder = self.settings.dir("live", f"session_{self.s['id']}")
        try:
            if not self._at(detected + self.call_a_s):
                _set(self.settings, mid, "skipped", "watcher stopped before the capture", timings=timings)
                return
            if not self.tw.stream(bid):
                _set(self.settings, mid, "skipped", "the stream ended before the capture (live-only: no clipping "
                                                    "from the archive)", timings=timings)
                return
            _set(self.settings, mid, "capturing")
            clip_a = self.tw.create_clip(bid, duration=60)
            timings["clip_a_requested_s"] = round(_now() - detected, 1)
            clip_b = None
            if self.dur_b and self._at(detected + self.call_b_s):
                if self.tw.stream(bid):        # live-only: the second half is only taken while still live
                    clip_b = self.tw.create_clip(bid, duration=self.dur_b)
                    timings["clip_b_requested_s"] = round(_now() - detected, 1)
                else:
                    timings["clip_b"] = "stream ended; kept the first half only"
            info = self.tw.wait_clip(clip_a)
            _set(self.settings, mid, "downloading", twitch_clip_id=clip_a, captured_at=info.get("created_at"))
            path_a = self.tw.fetch(self.tw.download_url(bid, clip_a), folder / f"moment_{mid}_a.mp4")
            path = path_a
            if clip_b:
                try:
                    self.tw.wait_clip(clip_b)
                    path_b = self.tw.fetch(self.tw.download_url(bid, clip_b), folder / f"moment_{mid}_b.mp4")
                    timings["join"] = stitch(path_a, path_b, folder / f"moment_{mid}.mp4",
                                             expected_overlap_s=self.overlap_s)
                    path = folder / f"moment_{mid}.mp4"
                except Exception as ex:          # the first half is still a usable moment
                    timings["clip_b"] = f"not used: {ex}"[:200]
            timings["downloaded_s"] = round(_now() - detected, 1)
            _set(self.settings, mid, "editing", file_path=str(path), timings=timings)
        except Exception as ex:
            _set(self.settings, mid, "failed", str(ex)[:300], timings=timings)
            return
        self.edits.submit(self._edit, mid, detected, path, timings)

    def _edit(self, mid: int, detected: float, path: Path, timings: dict) -> None:
        try:
            with db.connect(self.settings) as conn:
                chat = conn.execute("SELECT signals FROM live_moments WHERE id = %s", (mid,)).fetchone()["signals"]
            audio = audio_excitement(path)
        except Exception as ex:
            _set(self.settings, mid, "failed", f"editing: {ex}"[:300], timings=timings)
            return
        combined = round(0.6 * float(chat.get("chat", {}).get("score", 0)) + 0.4 * audio, 3)
        sig = {**chat, "audio_excitement": audio, "combined": combined}
        run_edit(self.settings, self.s, mid, detected, path, timings, sig, combined, min_combined=self.min_combined,
                 edit=self.edit)

    # -- main loop
    def run(self, *, until_end: bool = True, max_hours: float = 16) -> dict:
        stop_at = _now() + max_hours * 3600
        next_poll = 0.0
        seen_live = False
        try:
            while not self.stop.is_set() and _now() < stop_at:
                if _now() >= next_poll:
                    try:
                        live = self.poll_live()
                        seen_live = seen_live or live
                    except requests.RequestException:
                        live = self.s["status"] == "live"
                    next_poll = _now() + self.poll_s
                    if until_end and seen_live and not live:
                        break
                self.tick()
                self.stop.wait(1.0)
        finally:
            if self.s["status"] == "live" and self.stop.is_set():
                self._update_session(status="stopped", ended_at=datetime.now(timezone.utc))
            if self.chat:
                self.chat.stop.set()
            for t in self.captures:
                t.join(timeout=180)
            self.edits.shutdown(wait=True)
        with db.connect(self.settings) as conn:
            rows = conn.execute("SELECT status, count(*) AS n FROM live_moments WHERE session_id = %s GROUP BY 1",
                                (self.s["id"],)).fetchall()
        return {"session": self.s["id"], "login": self.s["login"], "moments": {r["status"]: r["n"] for r in rows}}


def live_render_opts() -> dict:
    """Live clips render small and fast: 720x1280, the GPU encoder when reachable, one encode for all platforms."""
    import os

    return {"size": os.environ.get("VICEPULSE_LIVE_RENDER_SIZE", "720x1280"),
            "encoder": os.environ.get("VICEPULSE_LIVE_ENCODER", "auto"),
            "preset": os.environ.get("VICEPULSE_LIVE_X264_PRESET", "veryfast"),
            "reuse": os.environ.get("VICEPULSE_LIVE_RENDER_REUSE", "1") == "1"}


def session_channels(session: dict) -> list[str]:
    """Every VicePulse channel that gets a version of this session's moments (main channel first)."""
    out = [session["channel_key"]]
    for ch in session.get("channels") or []:
        if ch not in out:
            out.append(ch)
    return out


def default_edit(settings: Settings, session: dict, mid: int, path: Path) -> list[int]:
    """The existing pipeline: rights gate, best window, subtitles, vertical render, final check -> review queue.

    One version per channel in the session (e.g. EN and BR), each with captions in that channel's language.
    """
    from .autosource import _clip_and_render
    from .llm import make_llm

    with db.connect(settings) as conn:
        lic = conn.execute("SELECT licence_type FROM rights_ledger WHERE id = %s", (session["rights_id"],)).fetchone()
        origin = "own_recording" if lic and lic["licence_type"] == "own" else "creator_upload"
        res = ingest.ingest_file(conn, settings, path, origin=origin, rights_id=session["rights_id"])
        conn.execute("UPDATE live_moments SET source_id = %s WHERE id = %s", (res["source_id"], mid))
        conn.commit()
        if res["status"] not in ("cleared", "analysed"):
            raise ValueError("rights gate refused the clip: " + "; ".join(res.get("reasons") or []))
        return _clip_and_render(conn, settings, make_llm(settings), res["source_id"], channels=session_channels(session),
                                clip_seconds=45, max_clips=1, ignore_day_limit=True, render_opts=live_render_opts())


def status(conn, *, limit: int = 30) -> dict:
    sessions = conn.execute("SELECT * FROM live_sessions ORDER BY id DESC LIMIT 10").fetchall()
    moments = conn.execute(
        """SELECT m.*, s.login, s.channel_key FROM live_moments m JOIN live_sessions s ON s.id = m.session_id
           ORDER BY m.id DESC LIMIT %s""", (limit,)).fetchall()
    return {"sessions": sessions, "moments": moments}
