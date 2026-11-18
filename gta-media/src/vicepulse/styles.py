"""Edit styles: Cinematic, Funny/Viral and Clean Professional, driven by what really happens in the clip.

Three steps, each testable on its own:

1. ``analyse``   - one cheap pass over the clip: loudness spikes, scene cuts, reaction words in the real
                   speech, and where the motion is over time (for a moving 9:16 crop). Shared by every
                   style and platform, so it runs once per clip.
2. ``plan_edit`` - turns those events into an edit plan for one style: the story order (optional teaser
                   of the payoff, context, payoff, natural end), slow motion, zooms, shake, flashes,
                   transitions, sound effects, colour and caption look. Every effect records the event
                   that caused it; nothing is placed at random, and no event or line of dialogue is
                   invented (captions only show the clip's real speech or the channel's own script).
3. ``render_styled`` - one FFmpeg run per output (NVENC when available, CPU otherwise).

Plus ``quality_check`` for the finished file (size, length, audio level, black screen).
"""

from __future__ import annotations

import os
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import media, signals
from .render import CAPTION_MARGIN_V, FPS, OUT_H, OUT_W, _ass_time, _esc, _video_codec_args

STYLES = ("cinematic", "viral", "clean", "pro")
ALIASES = {"funny": "viral", "funny_viral": "viral", "professional": "clean", "clean_professional": "clean",
           "streamer": "pro", "streamer_pro": "pro"}
LABELS = {"cinematic": "Cinematic", "viral": "Funny/Viral", "clean": "Clean Professional", "pro": "Streamer Pro"}

_EBU = re.compile(r"t:\s*([0-9.]+)\s+TARGET:.*?M:\s*(-?[0-9.]+|-inf)")
_PTS = re.compile(r"pts_time:([0-9.]+)")
_SUMMARY_I = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?[0-9.]+|-inf) LUFS")
_BLACK = re.compile(r"black_duration:\s*([0-9.]+)")


def style_name(value: str | None) -> str:
    v = (value or "").strip().lower().replace("-", "_").replace("/", "_")
    v = ALIASES.get(v, v)
    if v not in STYLES:
        raise ValueError(f"unknown edit style {value!r}: use cinematic, viral (funny), clean or pro (streamer)")
    return v


# --------------------------------------------------------------------------- analysis


@dataclass
class Event:
    t: float            # seconds from the clip start
    kind: str           # loud | cut | reaction | action
    strength: float     # 0..1
    detail: str


def loudness_events(path, in_s: float, dur: float, *, min_gap_s: float = 2.0) -> tuple[list[Event], float]:
    """Moments clearly louder than the clip's own normal level. Returns (events, median LUFS)."""
    proc = media.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                      "-vn", "-af", "ebur128", "-f", "null", "-"])
    curve = [(float(t), -120.0 if m == "-inf" else float(m)) for t, m in _EBU.findall(proc.stderr)]
    curve = [(t, v) for t, v in curve if v > -70]
    if len(curve) < 5:
        return [], -120.0
    median = statistics.median(v for _, v in curve)
    loud = sorted((p for p in curve if p[1] >= median + 6.0), key=lambda p: -p[1])
    picked: list[tuple[float, float]] = []
    loud = loud + sorted((p for p in curve if median + 3.5 <= p[1] < median + 6.0), key=lambda p: -p[1])
    for t, v in loud:
        if all(abs(t - pt) >= min_gap_s for pt, _ in picked):
            picked.append((t, v))
    events = [Event(round(max(0.0, t - 0.2), 2), "loud", round(max(0.05, min(1.0, (v - median - 4.0) / 14.0)), 2),
                    f"+{v - median:.0f} dB over the clip's normal level") for t, v in sorted(picked[:12])]
    values = sorted(v for _, v in curve)
    p90, p95, top = values[int(0.90 * (len(values) - 1))], values[int(0.95 * (len(values) - 1))], values[-1]
    if p95 - median < 3.0 and not any(e.strength >= 0.3 for e in events) and top - p90 > 0.5:
        # The sound was squashed flat (re-uploads, phone recordings, messaging apps): nothing is "much louder",
        # so rank the clip's own loudest moments against its own range instead.
        ranked: list[tuple[float, float]] = []
        for t, v in sorted((p for p in curve if p[1] >= values[int(0.98 * (len(values) - 1))]), key=lambda p: -p[1]):
            if all(abs(t - pt) >= 4.0 for pt, _ in ranked) and len(ranked) < 5:
                ranked.append((t, v))
        rel = [Event(round(max(0.0, t - 0.2), 2), "loud", round(0.35 + 0.45 * (v - p90) / (top - p90), 2),
                     f"one of the loudest moments in this clip (+{v - median:.1f} dB; its sound is heavily compressed)")
               for t, v in ranked]
        events = sorted([e for e in events if all(abs(e.t - r.t) >= 2.0 for r in rel)] + rel, key=lambda e: e.t)
    return events, median   # strength < 0.15: a smaller moment (counts for the story, gets no effect)


def cut_events(path, in_s: float, dur: float, threshold: float = 0.35) -> list[Event]:
    proc = media.run(["ffmpeg", "-hide_banner", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", str(path), "-an",
                      "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    return [Event(round(float(t), 2), "cut", 0.5, "scene change") for t in sorted({float(m) for m in _PTS.findall(proc.stderr)})]


def reaction_events(cues: list[dict]) -> list[Event]:
    """Reaction words actually spoken in the clip (only from real speech cues, never from the script)."""
    out = []
    for c in cues:
        if c.get("kind") != "speech":
            continue
        low = c["text"].lower()
        hits = sum(len(p.findall(low)) for p in signals._PHRASES) + len(signals._LAUGH.findall(c["text"]))
        if hits:
            out.append(Event(round(c["start"], 2), "reaction", round(min(1.0, 0.4 + 0.2 * hits), 2), f'said "{c["text"]}"'))
    return out


def motion_track(path, in_s: float, dur: float, src_w: int, src_h: int, *, fps: int = 4, width: int = 96,
                 crop: dict | None = None, mask: dict | None = None) -> dict:
    """Where the action is, horizontally, over time: smoothed keyframes [(t, centre 0..1)] and overall spread.

    crop: analyse only the picture inside the black bars. mask: ignore this area (the facecam), so the
    crop follows the game, not the streamer moving their head."""
    if crop:
        src_w, src_h = crop["w"], crop["h"]
    h = max(2, int(round(width * src_h / max(src_w, 1) / 2)) * 2)
    pre = f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}," if crop else ""
    raw = media.run_bytes(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                           "-an", "-vf", f"{pre}fps={fps},scale={width}:{h},format=gray", "-f", "rawvideo", "-"])
    mx0 = mx1 = my0 = my1 = -1
    if mask:
        mx0, mx1 = int(mask["x"] * width), int((mask["x"] + mask["w"]) * width) + 1
        my0, my1 = int(mask["y"] * h), int((mask["y"] + mask["h"]) * h) + 1
    import numpy as np

    n = width * h
    count = len(raw) // n
    if count < 2:
        return {"keys": [(0.0, 0.5)], "spread": 1.0, "centre": 0.5, "energy": []}
    frames = np.frombuffer(raw[: count * n], np.uint8).reshape(count, h, width).astype(np.int16)
    diff = np.abs(frames[1:] - frames[:-1])[:, ::2, :]            # every other row: same answer, half the work
    if mask:
        rows = np.arange(0, h, 2)
        sel = (rows >= my0) & (rows < my1)
        diff[:, sel, max(0, mx0):max(0, mx1)] = 0
    colsum = diff.sum(axis=1).astype(np.float64)                 # (pairs, width)
    colsum[colsum <= 3 * (h // 2)] = 0.0                          # ignore sensor/compression noise
    secs = (np.arange(1, count) / fps).astype(int)
    xs = np.arange(width)
    bins: dict[int, list[float]] = {}
    for sec in np.unique(secs):
        part = colsum[secs == sec]
        bins[int(sec)] = [float((part * xs).sum()), float(part.sum())]
    cols_all = colsum.sum(axis=0).tolist()
    total = sum(cols_all)
    if total <= 0:
        return {"keys": [(0.0, 0.5)], "spread": 1.0, "centre": 0.5, "energy": []}
    centre = sum(x * v for x, v in enumerate(cols_all)) / total
    spread = min(1.0, (sum(((x - centre) ** 2) * v for x, v in enumerate(cols_all)) / total) ** 0.5 / (width / 2))
    mean_e = total / max(1, len(bins))
    keys, last = [], centre / (width - 1)
    for sec in range(0, int(dur) + 1):
        num, e = bins.get(sec, (0.0, 0.0))
        c = (num / e) / (width - 1) if e > 0.15 * mean_e else last      # quiet second: hold the framing
        keys.append([sec + 0.5, c])
        last = c
    for i in range(len(keys)):                                       # smooth (3-second average)
        win = [keys[j][1] for j in range(max(0, i - 1), min(len(keys), i + 2))]
        keys[i].append(sum(win) / len(win))
    out, prev = [], None
    for t, _, c in keys:                                             # pan no faster than 15 % of the width per second
        c = c if prev is None else max(prev - 0.15, min(prev + 0.15, c))
        out.append((round(min(t, dur), 2), round(c, 3)))
        prev = c
    energy = [round(bins.get(sec, (0.0, 0.0))[1] / max(mean_e, 1e-9), 3) for sec in range(0, int(dur) + 1)]
    return {"keys": out, "spread": round(spread, 3), "centre": round(centre / (width - 1), 3), "energy": energy}


def action_events(motion: dict, *, min_ratio: float = 2.0, min_gap_s: float = 3.0) -> list[Event]:
    """Bursts of on-screen movement (gunfire, crashes, explosions): seconds with far more movement than usual.
    The facecam is masked out before this, so a streamer moving their head does not count."""
    energy = motion.get("energy") or []
    if len(energy) < 5:
        return []
    usual = statistics.median(energy) or 1e-9
    peaks = sorted(((e / usual, sec) for sec, e in enumerate(energy)
                    if e / usual >= min_ratio and e >= max(energy[max(0, sec - 1):sec + 2])), reverse=True)
    out: list[Event] = []
    for ratio, sec in peaks:
        if all(abs(sec + 0.5 - e.t) >= min_gap_s for e in out) and len(out) < 6:
            out.append(Event(round(sec + 0.5, 2), "action", round(min(0.7, 0.3 + 0.1 * (ratio - min_ratio)), 2),
                             f"burst of on-screen action ({ratio:.1f}x the clip's usual movement)"))
    return sorted(out, key=lambda e: e.t)


def analyse(path, info: dict, in_s: float, out_s: float, cues: list[dict] | None = None, *,
            facecam: str | dict | None = None, autocrop: bool = True) -> dict:
    """facecam: None (none), 'auto' (detect it), or a dict {x, y, w, h, mode} in fractions of the picture."""
    from . import facecam as fc

    dur = out_s - in_s
    t0 = time.monotonic()
    active = fc.active_area(path, in_s, dur) if autocrop else None
    cam = fc.detect_facecam(path, in_s, out_s, crop=active) if facecam == "auto" else facecam
    loud, median = loudness_events(path, in_s, dur) if info.get("has_audio") else ([], -120.0)
    cuts = cut_events(path, in_s, dur)
    react = reaction_events(cues or [])
    motion = motion_track(path, in_s, dur, info["width"], info["height"], crop=active, mask=cam)
    action = action_events(motion)
    return {"duration": round(dur, 3), "events": sorted(loud + cuts + react + action, key=lambda e: e.t),
            "median_lufs": median, "motion": motion, "src_fps": info.get("fps") or 30.0,
            "active": active, "facecam": cam, "facecam_detected": facecam == "auto" and cam is not None,
            "seconds": round(time.monotonic() - t0, 2)}


# --------------------------------------------------------------------------- planning


@dataclass
class Segment:
    start: float          # clip seconds
    end: float
    speed: float = 1.0
    role: str = "story"   # teaser | story | slowmo

    @property
    def out_len(self) -> float:
        return (self.end - self.start) / self.speed


@dataclass
class Effect:
    kind: str             # zoom | push | shake | flash | whoosh | hit | fade_out | pulse | tag
    t: float              # output seconds
    dur: float
    amount: float
    reason: str


@dataclass
class EditPlan:
    style: str
    segments: list[Segment]
    effects: list[Effect] = field(default_factory=list)
    transition: str = "cut"          # between teaser and story: black | white | cut
    layout: str = "crop"             # crop | blur
    payoff: dict | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return round(sum(s.out_len for s in self.segments), 3)

    def to_out(self, t: float) -> float | None:
        """Clip time -> output time, through the story (not the teaser). None when that moment was cut."""
        off = 0.0
        for s in self.segments:
            if s.role != "teaser" and s.start <= t <= s.end:
                return round(off + (t - s.start) / s.speed, 3)
            off += s.out_len
        return None

    def story_offset(self) -> float:
        return round(sum(s.out_len for s in self.segments if s.role == "teaser"), 3)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d


def _speech_at(cues: list[dict], t0: float, t1: float) -> bool:
    return any(c.get("kind") == "speech" and c["start"] < t1 and c["end"] > t0 for c in cues)


def _cue_containing(cues: list[dict], t: float) -> dict | None:
    return next((c for c in cues if c.get("kind") == "speech" and c["start"] < t < c["end"]), None)


def pick_payoff(events: list[Event], dur: float) -> Event | None:
    """The moment the clip is about: the strongest loud spike, helped by a spoken reaction right after it."""
    best, best_score = None, 0.0
    for e in events:
        if e.kind == "loud":
            bonus = 0.25 if any(r.kind == "reaction" and 0 <= r.t - e.t <= 2.5 for r in events) else 0.0
            bonus += 0.15 if any(r.kind == "action" and abs(r.t - e.t) <= 1.5 for r in events) else 0.0
            score = e.strength + bonus
        elif e.kind == "action":
            score = e.strength * 0.8
        else:
            continue
        if e.t < 3.0:
            score -= 0.15          # no room for any build-up before it
        if score > best_score:
            best, best_score = e, score
    if best is None:                                       # no loud moment: a spoken reaction can still carry it
        reacts = [e for e in events if e.kind == "reaction"]
        best = max(reacts, key=lambda e: e.strength, default=None)
        best_score = best.strength if best else 0.0
    return best if best is not None and best_score >= 0.3 and best.t < dur - 1.0 else None


STYLE_RULES = {
    #             teaser_s  context_s  slowmo  punches  transition
    "cinematic": dict(teaser=1.6, context=12.0, slowmo=True, punches=False, transition="black"),
    "viral":     dict(teaser=1.2, context=8.0, slowmo=False, punches=True, transition="white"),
    "clean":     dict(teaser=0.0, context=12.0, slowmo=False, punches=False, transition="cut"),
    "pro":       dict(teaser=1.2, context=10.0, slowmo=False, punches=False, transition="black"),
}


def plan_edit(style: str, analysis: dict, cues: list[dict] | None = None, *, voice: bool = False,
              layout: str = "auto", min_len: float = 10.0) -> EditPlan:
    style = style_name(style)
    rules = STYLE_RULES[style]
    cues = cues or []
    dur = analysis["duration"]
    events: list[Event] = [e if isinstance(e, Event) else Event(**e) for e in analysis["events"]]
    payoff = pick_payoff(events, dur)
    notes: list[str] = []
    spread = analysis["motion"]["spread"]
    if layout == "auto" or style == "clean":
        # Clean always shows the whole picture: nothing is cut off, and the facecam stays where it was.
        layout = "blur" if (style == "clean" or spread >= 0.55) else "crop"

    if payoff is None:
        notes.append("no clear payoff moment (no loud spike or spoken reaction): kept in order, no effects on it")
        plan = EditPlan(style, [Segment(0.0, dur)], transition="cut", layout=layout, notes=notes)
        _finish(plan, style, events, payoff=None)
        return plan

    p = payoff.t
    # Story: enough context before the payoff, and never start or end in the middle of someone's sentence.
    start = max(0.0, p - rules["context"])
    if (c := _cue_containing(cues, start)) is not None:
        start = max(0.0, c["start"] - 0.2)
    # Natural end: let the payoff land, and keep what follows it while the action keeps going (each next loud
    # moment, reaction or burst of action within 6 s of the last), plus a short beat after the last of those.
    last = p
    for e in sorted(events, key=lambda e: e.t):
        if e.t > last and e.kind in ("loud", "reaction", "action"):
            if e.t - last > 6.0:
                break
            last = e.t
    end = max(p + 4.0, last + 2.5)
    # Someone is talking at (or within a second of) the end: let them finish the sentence.
    talking = [c["end"] for c in cues if c.get("kind") == "speech" and c["start"] < end + 1.0 and c["end"] > end]
    if talking:
        end = max(talking) + 0.3
    end = min(dur, end)
    if dur - end < 3.0:                                    # only trim a tail that is clearly dead air
        end = dur
    if end - start < min_len:
        start = max(0.0, end - min_len)
        end = min(dur, max(end, start + min_len))
    if voice and (start > 0 or end < dur):
        start, end = 0.0, dur
        notes.append("kept the whole clip: the recorded voice-over is timed to it")
    if start > 0:
        notes.append(f"starts {start:.1f}s in: {p - start:.1f}s of context before the payoff")
    if end < dur:
        notes.append(f"ends {dur - end:.1f}s early: the action after the payoff stops at {end - 2.5:.1f}s")

    segs: list[Segment] = []
    teaser_ok = rules["teaser"] > 0 and not voice and p - start >= 6.0
    if teaser_ok:
        t0 = max(start, p - 0.5)
        segs.append(Segment(round(t0, 3), round(min(end, t0 + rules["teaser"]), 3), 1.0, "teaser"))
        notes.append(f"opens with a {rules['teaser']:.1f}s teaser of the payoff at {p:.1f}s, then rewinds for context")
    elif rules["teaser"] > 0:
        notes.append("no teaser: " + ("voice-over must stay in sync" if voice else "the payoff is already near the start"))

    sm0, sm1 = p - 0.6, p + 0.9
    slowmo_ok = (rules["slowmo"] and not voice and payoff.kind == "loud" and payoff.strength >= 0.5
                 and sm0 > start + 1 and sm1 < end - 1 and not _speech_at(cues, sm0, sm1))
    if slowmo_ok:
        segs += [Segment(round(start, 3), round(sm0, 3)), Segment(round(sm0, 3), round(sm1, 3), 0.5, "slowmo"),
                 Segment(round(sm1, 3), round(end, 3))]
        notes.append(f"payoff at {p:.1f}s plays at half speed for {sm1 - sm0:.1f}s")
    else:
        segs.append(Segment(round(start, 3), round(end, 3)))
        if rules["slowmo"]:
            notes.append("no slow motion: " + ("someone is speaking at the payoff" if _speech_at(cues, sm0, sm1)
                                               else "voice-over must stay in sync" if voice
                                               else "the payoff is not strong enough or too close to an edge"))
    plan = EditPlan(style, segs, transition=rules["transition"] if teaser_ok else "cut", layout=layout,
                    payoff=asdict(payoff), notes=notes)
    _finish(plan, style, events, payoff)
    return plan


def _finish(plan: EditPlan, style: str, events: list[Event], payoff: Event | None) -> None:
    """Place the effects for this style on the output timeline, each tied to a real event."""
    fx = plan.effects
    total = plan.duration
    if plan.story_offset() > 0:
        b = plan.story_offset()
        fx.append(Effect("whoosh", max(0.0, b - 0.25), 0.45, 0.6, "transition from the teaser back to the build-up"))
    p_out = plan.to_out(payoff.t) if payoff else None
    if style == "cinematic" and p_out is not None:
        start = max(plan.story_offset(), p_out - 2.5)
        fx.append(Effect("push", round(start, 3), round(p_out + 1.6 - start, 3), 0.10,
                         f"slow push-in building to the payoff ({payoff.detail})"))
        fx.append(Effect("hit", max(0.0, p_out - 0.05), 0.6, 0.5, f"low impact under the payoff ({payoff.detail})"))
    if style == "viral":
        used: list[float] = []
        for e in sorted(events, key=lambda e: -e.strength):
            if e.kind not in ("loud", "reaction", "action") or e.strength < 0.35:
                continue
            t = plan.to_out(e.t)
            if t is None or any(abs(t - u) < 1.5 for u in used) or len(used) >= 5:
                continue
            used.append(t)
            fx.append(Effect("zoom", t, 0.45, round(0.10 + 0.15 * e.strength, 3), f"punch-in on {e.kind}: {e.detail}"))
        if p_out is not None:
            fx.append(Effect("shake", p_out, 0.4, 1.0, f"shake on the payoff ({payoff.detail})"))
            fx.append(Effect("flash", p_out, 0.12, 0.55, f"flash on the payoff ({payoff.detail})"))
            fx.append(Effect("hit", max(0.0, p_out - 0.03), 0.4, 0.8, f"impact sound on the payoff ({payoff.detail})"))
    if style == "pro" and p_out is not None:
        fx.append(Effect("zoom", p_out, 0.5, 0.09, f"punch-in on the payoff ({payoff.detail})"))
        fx.append(Effect("pulse", p_out, 0.45, 1.0, f"frames flash on the payoff ({payoff.detail})"))
        fx.append(Effect("tag", p_out, 2.2, 1.0, f"MOMENT tag drops in on the payoff ({payoff.detail})"))
        fx.append(Effect("hit", max(0.0, p_out - 0.03), 0.45, 0.7, f"impact sound on the payoff ({payoff.detail})"))
    if style == "clean" and p_out is not None:
        start = max(0.0, p_out - 1.5)
        fx.append(Effect("push", round(start, 3), 3.5, 0.04, f"subtle push on the key moment ({payoff.detail})"))
    if style in ("cinematic", "clean"):
        d = 0.6 if style == "cinematic" else 0.4
        fx.append(Effect("fade_out", round(total - d, 3), d, 1.0, "gentle ending"))
    fx.sort(key=lambda f: f.t)


# --------------------------------------------------------------------------- captions


def _ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _clean_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def retime_cues(cues: list[dict], plan: EditPlan) -> list[dict]:
    """Captions onto the edited timeline. Real speech stays exactly in sync (split where the edit cuts);
    the channel's own script captions are spread over the story; the hook stays at the very start."""
    out: list[dict] = []
    story0 = plan.story_offset()
    story_len = plan.duration - story0
    orig_len = max((c["end"] for c in cues), default=0.0) or 1.0
    for c in cues:
        kind = c.get("kind") or "body"
        if kind in ("hook", "hook_top"):
            out.append({**c, "start": 0.0, "end": round(min(c["end"] - c["start"] + story0, plan.duration), 3)})
        elif kind == "speech":
            off = 0.0
            for s in plan.segments:
                if s.role != "teaser":
                    a, b = max(c["start"], s.start), min(c["end"], s.end)
                    if b - a > 0.15:
                        out.append({**c, "start": round(off + (a - s.start) / s.speed, 3),
                                    "end": round(off + (b - s.start) / s.speed, 3)})
                off += s.out_len
        else:
            k = story_len / orig_len
            out.append({**c, "start": round(story0 + c["start"] * k, 3), "end": round(story0 + c["end"] * k, 3)})
    return sorted(out, key=lambda c: c["start"])


DEFAULT_BRAND = {"name": "VicePulse", "handle": "", "colour": "#FFD600", "intro_text": "", "outro_text": "", "credit": ""}


def build_styled_ass(cues: list[dict], plan: EditPlan, platform: str, brand: dict | None = None, *,
                     top_margin: int = 300, caption_margin: int | None = None, graphics: bool = True) -> str:
    """Captions and titles for one style (1080x1920 canvas). top_margin: title position; caption_margin: caption
    distance from the bottom when the layout needs it (never inside the apps' button area)."""
    brand = {**DEFAULT_BRAND, **(brand or {})}
    from .graphics import CAPTION_FONT, TITLE_FONT, theme

    th = theme(brand)
    margin = max(CAPTION_MARGIN_V.values()) if platform == "shared" else CAPTION_MARGIN_V[platform]
    if caption_margin:
        margin = max(margin, caption_margin)
    accent, primary, dark = _ass_colour(th["accent"]), _ass_colour(th["primary"]), _ass_colour(th["dark"])
    plain = os.environ.get("VICEPULSE_CAPTION_FONT", "DejaVu Sans")
    title_f = TITLE_FONT if graphics else plain
    cap_f = CAPTION_FONT if graphics else plain
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
           "MarginR, MarginV, Encoding")
    S = plan.style
    if S == "cinematic":
        styles = [f"Style: Cap,{cap_f},54,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,0,0,100,100,1,0,1,2,3,2,110,110,{margin},1",
                  f"Style: Hook,{title_f},96,&H00F4F4F4,&H00FFFFFF,&H00000000,&HA0000000,0,0,0,0,100,100,9,0,1,2,4,8,90,90,{top_margin},1"]
    elif S == "viral":
        styles = [f"Style: Cap,{cap_f},70,{dark},&H00FFFFFF,{accent},&HB4000000,0,0,0,0,100,100,0,0,3,16,7,2,80,80,{margin + 40},1",
                  f"Style: Hook,{title_f},112,&H00FFFFFF,&H00FFFFFF,{primary},&H96000000,0,0,0,0,100,100,1,0,1,9,6,8,60,60,{top_margin},1"]
    elif S == "pro":
        styles = [f"Style: Cap,{cap_f},64,&H00FFFFFF,&H00FFFFFF,{dark},&H96000000,0,0,0,0,100,100,0,0,1,6,4,2,80,80,{margin},1",
                  f"Style: Hook,{title_f},90,&H00FFFFFF,&H00FFFFFF,{dark},&H96000000,0,0,0,0,100,100,1,0,1,6,4,8,80,80,{top_margin},1"]
    else:
        box = _ass_colour(th["dark"], 0x30)
        styles = [f"Style: Cap,{plain},58,&H00FFFFFF,&H00FFFFFF,{box},&H00000000,-1,0,0,0,100,100,0,0,3,14,0,2,110,110,{margin},1",
                  f"Style: Hook,{cap_f},62,&H00FFFFFF,&H00FFFFFF,{box},&H00000000,0,0,0,0,100,100,0,0,3,18,0,8,110,110,{top_margin},1"]
    styles += [f"Style: Tag,{plain},40,&H50FFFFFF,&H00FFFFFF,&H80000000,&H00000000,-1,0,0,0,100,100,1,0,1,2,0,7,70,70,230,1",
               f"Style: Outro,{title_f},84,&H00FFFFFF,&H00FFFFFF,{dark},&H80000000,0,0,0,0,100,100,2,0,1,6,3,5,90,90,0,1"]
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {OUT_W}\nPlayResY: {OUT_H}\nWrapStyle: 0\n"
            f"ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n{fmt}\n" + "\n".join(styles) +
            "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines: list[str] = []

    def add(start, end, style, text, layer=0):
        if end > start:
            lines.append(f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{style},,0,0,0,,{text}")

    reaction_words = {w for c in cues if c.get("kind") == "speech" for w in c["text"].lower().split()
                      if any(p.search(w) for p in signals._PHRASES)}

    def word_pop(c, st, colour, prefix=""):
        # Word-by-word highlight. The cue's words are spread evenly over the cue (the cue itself is in sync).
        words = _clean_text(c["text"]).upper().split()
        step = (c["end"] - c["start"]) / max(1, len(words))
        for i in range(len(words)):
            parts = []
            for j, w in enumerate(words):
                if j == i:
                    col = "&H004040FF&" if w.lower().strip(".,!?") in reaction_words else colour + "&"
                    parts.append(r"{\c" + col + r"\fscx110\fscy110}" + w + r"{\r}")
                else:
                    parts.append(w)
            pop = r"{\fscx80\fscy80\t(0,90,\fscx100\fscy100)}" if i == 0 else ""
            add(c["start"] + i * step, c["start"] + (i + 1) * step, st, prefix + pop + " ".join(parts))

    for c in cues:
        text = _clean_text(c["text"])
        is_hook = c.get("kind") in ("hook", "hook_top")
        st = "Hook" if is_hook else "Cap"
        if S == "pro" and is_hook and graphics:
            continue                                   # the animated title bar carries the hook
        if S == "cinematic":
            if is_hook:
                add(c["start"], c["end"], st, r"{\fad(350,250)\blur8\t(0,350,\blur0)}" + text.upper())
            else:
                add(c["start"], c["end"], st, r"{\fad(220,200)}" + text)
        elif S == "clean":
            add(c["start"], c["end"], st, r"{\fad(140,140)}" + text)
        elif S == "viral":
            if is_hook:
                add(c["start"], c["end"], st, r"{\fax-0.12\fscx135\fscy135\t(0,160,\fscx100\fscy100)}" + text.upper())
            else:
                word_pop(c, st, primary, prefix=r"{\fax-0.1}")
        else:
            word_pop(c, st, accent)
    total = plan.duration
    tag = brand.get("credit") or brand.get("handle")
    if tag and S == "clean" and not graphics:
        add(0.0, total, "Tag", _clean_text(tag), layer=1)
    if brand.get("intro_text") and not any(c.get("kind") in ("hook", "hook_top") for c in cues):
        add(0.0, min(1.4, total), "Hook", r"{\fad(150,200)}" + _clean_text(brand["intro_text"]), layer=1)
    if brand.get("outro_text") and total > 6:
        add(total - 1.8, total, "Outro", r"{\fad(250,0)\fscx85\fscy85\t(0,250,\fscx100\fscy100)}"
            + _clean_text(brand["outro_text"]).upper(), layer=2)
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- render


def _n(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") or "0"


def _lerp_expr(keys: list[tuple[float, float]]) -> str:
    """Piecewise-linear function of t through the keyframes, as an FFmpeg expression."""
    if len(keys) == 1:
        return _n(keys[0][1])
    expr = _n(keys[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(zip(keys, keys[1:]))):
        dt = max(t1 - t0, 1e-3)
        expr = f"if(lt(t,{_n(t1)}),{_n(v0)}+({_n(v1 - v0)})*(t-{_n(t0)})/{_n(dt)},{expr})"
    return f"if(lt(t,{_n(keys[0][0])}),{_n(keys[0][1])},{expr})"


def output_keys(plan: EditPlan, keys: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Motion keyframes (clip time) onto the edited timeline, holding the framing across each cut."""
    out: list[tuple[float, float]] = []
    off = 0.0

    def at(t):
        if t <= keys[0][0]:
            return keys[0][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t <= t1:
                return v0 + (v1 - v0) * (t - t0) / max(t1 - t0, 1e-3)
        return keys[-1][1]

    for s in plan.segments:
        pts = [s.start] + [t for t, _ in keys if s.start < t < s.end] + [s.end]
        for i, t in enumerate(pts):
            ot = off + (t - s.start) / s.speed
            if i == 0 and out:
                ot += 0.001                    # jump cut: new framing starts with the new shot
            out.append((round(ot, 3), round(at(t), 3)))
        off += s.out_len
    return out


def _zoom_terms(plan: EditPlan) -> str:
    terms = []
    for f in plan.effects:
        if f.kind == "zoom":       # fast in (60 ms), decays out over the rest
            terms.append(f"{_n(f.amount)}*clip((t-{_n(f.t)})/0.06,0,1)*clip(({_n(f.t + f.dur)}-t)/{_n(f.dur * 0.7)},0,1)")
        elif f.kind == "push":     # slow ramp up over most of the window, quick ease back at the end
            ramp = max(f.dur - 0.6, 0.5)
            terms.append(f"{_n(f.amount)}*clip((t-{_n(f.t)})/{_n(ramp)},0,1)*clip(({_n(f.t + f.dur)}-t)/0.6,0,1)")
    return "+".join(terms)


def _shake_terms(plan: EditPlan, w: int) -> tuple[str, str]:
    sx, sy = [], []
    amp = max(4, round(14 * w / OUT_W))
    for f in plan.effects:
        if f.kind == "shake":
            env = f"between(t,{_n(f.t)},{_n(f.t + f.dur)})*(1-(t-{_n(f.t)})/{_n(f.dur)})"
            sx.append(f"{amp}*sin(t*71)*{env}")
            sy.append(f"{amp}*cos(t*53)*{env}")
    return "+".join(sx) or "0", "+".join(sy) or "0"


def _sfx_sources(plan: EditPlan) -> list[str]:
    """Sound effects generated by FFmpeg itself (original, licence-free): whoosh = filtered noise, hit = low thump."""
    out = []
    for i, f in enumerate(e for e in plan.effects if e.kind in ("whoosh", "hit")):
        ms = int(f.t * 1000)
        if f.kind == "whoosh":
            src = (f"anoisesrc=d={_n(f.dur)}:c=pink:a=0.6:r=48000,highpass=f=500,lowpass=f=5000,"
                   f"afade=t=in:d={_n(f.dur * 0.7)}:curve=exp,afade=t=out:st={_n(f.dur * 0.7)}:d={_n(f.dur * 0.3)},"
                   f"volume={_n(f.amount * 0.5)}")
        else:
            src = (f"sine=f=52:d={_n(f.dur)}:r=48000,afade=t=out:d={_n(f.dur)}:curve=exp,volume={_n(f.amount * 1.1)}")
        out.append(f"{src},aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=delays={ms}:all=1[sfx{i}]")
    return out


GRADES = {
    # Warm, contrasty look with darker corners. All in YUV (lookup tables): an RGB colour-balance filter looked
    # about the same but doubled the render time on a 4-core laptop.
    "cinematic": "eq=contrast=1.08:saturation=1.12:gamma=0.97,lutyuv=u='val-3':v='val+4',vignette=angle=PI/5:eval=init",
    "viral": "eq=contrast=1.06:saturation=1.25",
    "clean": "eq=contrast=1.03:saturation=1.04",
    "pro": "eq=contrast=1.05:saturation=1.15",
}
AUDIO_POLISH = {
    "cinematic": "equalizer=f=90:t=q:w=1:g=3,acompressor=threshold=0.125:ratio=2.5:attack=15:release=250",
    "viral": "acompressor=threshold=0.1:ratio=4:attack=5:release=120",
    "clean": "highpass=f=60,acompressor=threshold=0.18:ratio=2:attack=20:release=300",
    "pro": "acompressor=threshold=0.12:ratio=3:attack=10:release=200",
}

PIP_TOP = 0.11          # facecam picture-in-picture: top right, below the apps' top bar
PIP_WIDTH = 0.36
SPLIT_CAM = 0.36        # Funny/Viral: share of the height for the camera on top


@dataclass
class Layout:
    """Where everything goes for one style (output pixels). Worked out once, used by the graph and captions."""
    W: int
    H: int
    mode: str                      # full (crop/blur over the screen) | split | pro
    cam: dict | None = None        # facecam shown on its own (None = it stays where it is in the picture)
    cam_box: tuple | None = None   # x, y, w, h of the separate camera
    game_box: tuple | None = None  # x, y, w, h of the gameplay
    game_src: tuple | None = None  # x, y, w, h of the source region used as gameplay (pro)
    crop_range: tuple | None = None  # (min, max) x of the moving crop, keeping the facecam out of it
    title_y: int = 300             # caption canvas (1920 high): titles
    caption_margin: int | None = None
    notes: list = field(default_factory=list)


def _crop_avoiding(cam: dict, scaled_w: float, window: int) -> tuple | None:
    """x range for a moving crop of width ``window`` that never shows the facecam, or None if impossible."""
    left_edge, right_edge = cam["x"] * scaled_w, (cam["x"] + cam["w"]) * scaled_w
    if scaled_w - right_edge >= window:                    # room to the right of the camera
        return right_edge, scaled_w - window
    if left_edge >= window:                                # room to the left
        return 0.0, left_edge - window
    return None


def plan_layout(plan: EditPlan, size: tuple[int, int], sw: int, sh: int, facecam: dict | None) -> Layout:
    W, H = size
    k = W / OUT_W
    st = plan.style
    if st == "pro":
        lay = Layout(W, H, "pro", title_y=0)
        side = facecam and (facecam["x"] + facecam["w"] <= 0.45 or facecam["x"] >= 0.55)
        if facecam and side:
            # Gameplay = the part of the picture beside the camera (no painting-over), camera in its own frame.
            gx0 = (facecam["x"] + facecam["w"]) * sw if facecam["x"] < 0.5 else 0.0
            gx1 = sw if facecam["x"] < 0.5 else facecam["x"] * sw
            gw = int(gx1 - gx0) // 2 * 2
            box_w = round(1000 * k) // 2 * 2
            box_h = min(round(860 * k), round(box_w * sh / gw)) // 2 * 2
            src_h = min(sh, round(gw * box_h / box_w)) // 2 * 2
            lay.game_src = (int(gx0) // 2 * 2, (sh - src_h) // 2 // 2 * 2, gw, src_h)
            cam_px_w, cam_px_h = facecam["w"] * sw, facecam["h"] * sh
            a = cam_px_h / max(cam_px_w, 1)
            cw = round(min(640, 430 / max(a, 0.4)) * k) // 2 * 2
            ch = round(cw * a) // 2 * 2
            lay.cam, lay.cam_box = facecam, ((W - cw) // 2, round(270 * k), cw, ch)
            gy = round(270 * k) + ch + round(44 * k)
            lay.game_box = ((W - box_w) // 2, gy, box_w, box_h)
            lay.notes.append("Streamer Pro: camera in its own frame, gameplay beside it (nothing painted over)")
        else:
            box_w = round(1000 * k) // 2 * 2
            box_h = min(round(1100 * k), round(box_w * sh / sw)) // 2 * 2
            lay.game_box = ((W - box_w) // 2, round(300 * k), box_w, box_h)
            if facecam:
                lay.notes.append("Streamer Pro: camera is not at a side, kept inside the gameplay picture")
        gb = lay.game_box
        lay.caption_margin = max(round((H - gb[1] - gb[3]) / k) + 40, 360)
        return lay
    if st == "viral" and facecam and plan.layout == "crop":
        cam_h = round(H * SPLIT_CAM / 2) * 2
        gh = H - cam_h
        scaled_w = sw * gh / sh
        rng = _crop_avoiding(facecam, scaled_w, W)
        if rng:
            lay = Layout(W, H, "split", cam=facecam, cam_box=(0, 0, W, cam_h), game_box=(0, cam_h, W, gh),
                         crop_range=rng, title_y=round(OUT_H * SPLIT_CAM) + 40)
            lay.notes.append("camera on top; the gameplay crop steers around the camera's corner")
            return lay
    lay = Layout(W, H, "full", game_box=(0, 0, W, H))
    if facecam and plan.layout == "crop" and st == "cinematic":
        rng = _crop_avoiding(facecam, sw * H / sh, W)
        if rng:
            d = round(W * 0.34 / 2) * 2
            lay.cam, lay.cam_box, lay.crop_range = facecam, (W - d - round(W * 0.05), round(H * PIP_TOP), d, d), rng
            lay.title_y = round(OUT_H * PIP_TOP + d / k + 60)
            lay.notes.append("round camera window; the gameplay crop steers around the camera's corner")
            return lay
    if facecam:
        lay.notes.append("camera kept where it is in the original picture (it cannot be moved without painting over)")
    return lay


def build_graph(plan: EditPlan, info: dict, ass: Path, size: tuple[int, int], motion: dict, *,
                facecam: dict | None = None, voice: bool = False, active: dict | None = None,
                gfx=None, inputs: list[str] | None = None, brand: dict | None = None,
                layout: Layout | None = None) -> str:
    """Input i is story segment i, already cut to that segment (see ``segment_inputs``); input n is the voice;
    graphics images are appended to ``inputs`` as further inputs.

    Each segment is read from the file on its own. Splitting one input instead makes FFmpeg hold every
    frame between the teaser and the rest of the story in memory (over 1 GB at 720p60) - too much for this PC.
    """
    W, H = size
    k = W / OUT_W
    segs = plan.segments
    n = len(segs)
    total = plan.duration
    g: list[str] = []
    inputs = inputs if inputs is not None else []
    next_idx = [n + (1 if voice else 0)]
    brand = brand or {}

    png_n = [0]

    def png(path: str) -> str:
        # Decoded once and repeated from memory (looping the image input would decode it again every frame).
        inputs.extend(["-i", path])
        idx = next_idx[0]
        next_idx[0] += 1
        png_n[0] += 1
        lbl = f"[img{png_n[0]}]"
        g.append(f"[{idx}:v]loop=loop=-1:size=1:start=0,setpts=N/{FPS}/TB,trim=duration={_n(total + 0.5)}{lbl}")
        return lbl

    def seq(pattern: str, rate: int = 8) -> str:
        """A looping image sequence (animated border): only `rate` small images decoded per second."""
        inputs.extend(["-loop", "1", "-framerate", str(rate), "-t", f"{total + 0.5:.3f}", "-i", pattern])
        idx = next_idx[0]
        next_idx[0] += 1
        png_n[0] += 1
        lbl = f"[img{png_n[0]}]"
        g.append(f"[{idx}:v]fps={FPS},format=yuva420p{lbl}")
        return lbl

    has_audio = bool(info.get("has_audio"))
    fade_col = "white" if plan.transition == "white" else "black"
    for i, s in enumerate(segs):
        seg_len = s.end - s.start
        pre = f"crop={active['w']}:{active['h']}:{active['x']}:{active['y']}," if active else ""
        v = f"[{i}:v]{pre}trim=duration={_n(seg_len)},setpts=(PTS-STARTPTS)/{_n(s.speed)}"
        a = (f"[{i}:a]atrim=duration={_n(seg_len)},asetpts=PTS-STARTPTS" if has_audio
             else f"anullsrc=r=48000:cl=stereo,atrim=duration={_n(seg_len)}")
        if s.speed != 1.0:
            a += f",atempo={_n(s.speed)}"
        a += ",aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        if plan.transition in ("black", "white") and s.role == "teaser":
            v += f",fade=t=out:st={_n(s.out_len - 0.18)}:d=0.18:color={fade_col}"
            a += f",afade=t=out:st={_n(s.out_len - 0.15)}:d=0.15"
        elif plan.transition in ("black", "white") and i > 0 and segs[i - 1].role == "teaser":
            v += f",fade=t=in:st=0:d=0.18:color={fade_col}"
            a += ",afade=t=in:d=0.15"
        g.append(v + f"[v{i}]")
        g.append(a + f"[a{i}]")
    g.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca]")

    sw, sh = (active["w"], active["h"]) if active else (info["width"], info["height"])
    lay = layout or plan_layout(plan, size, sw, sh, facecam)
    cam = lay.cam
    keys = output_keys(plan, [tuple(kk) for kk in motion["keys"]])
    g.append(f"[cv]fps={FPS},setsar=1" + ("[cvx];[cvx]split=2[cvg][cvc]" if cam else "[cvg]"))

    # ---- gameplay picture [gm] at its box size
    gx, gy, gw, gh = lay.game_box
    if lay.mode == "pro":
        if lay.game_src:
            x, y, w, h = lay.game_src
            g.append(f"[cvg]crop={w}:{h}:{x}:{y},scale={gw}:{gh}[gm]")
        else:
            g.append(f"[cvg]scale={gw}:{gh}:force_original_aspect_ratio=increase,crop={gw}:{gh}[gm]")
    elif plan.layout == "crop":
        scaled_w = sw * gh / sh
        lo, hi = lay.crop_range or (0.0, max(0.0, scaled_w - gw))
        xkeys = [(t, min(hi, max(lo, scaled_w * c - gw / 2))) for t, c in keys]
        g.append(f"[cvg]scale=-2:{gh},crop={gw}:{gh}:x='{_lerp_expr(xkeys)}':y=0[gm]")
    else:
        # Whole picture on a blurred fill. Clean: a little smaller, rounded corners, thin frame.
        bw, bh = max(2, gw // 4 // 2 * 2), max(2, gh // 4 // 2 * 2)
        fw = (gw - round(72 * k)) // 2 * 2 if plan.style == "clean" else gw
        fh = round(fw * sh / sw) // 2 * 2
        g.append(f"[cvg]split=2[bgs][fgs];[bgs]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                 f"boxblur={max(2, round(5 * H / OUT_H))}:3,scale={gw}:{gh}[bg];[fgs]scale={fw}:{fh}[fg0]")
        if plan.style == "clean" and gfx:
            m = png(gfx.rounded_mask(fw, fh, radius=26))
            g.append(f"{m}format=gray,scale={fw}:{fh}[fm];[fg0]format=yuva420p[fga];[fga][fm]alphamerge[fg]")
        else:
            g.append("[fg0]null[fg]")
        g.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[gm]")
        if plan.style == "clean" and gfx:
            fr, pad = gfx.frame(fw, fh, name="clean_frame", radius=26, stroke=5, brackets=False, glow=False)
            fi = png(fr)
            g.append(f"{fi}format=yuva420p[fr0];[gm][fr0]overlay={(gw - fw) // 2 - pad}:{(gh - fh) // 2 - pad}[gm_]")
        else:
            g.append("[gm]null[gm_]")
    gm = "[gm_]" if plan.layout != "crop" and lay.mode != "pro" else "[gm]"

    # ---- zoom / shake on the gameplay only
    z = _zoom_terms(plan)
    sx, sy = _shake_terms(plan, W)
    if z or sx != "0":
        zexpr = f"min(1.4,1+{z})" if z else "1.06"          # shake alone needs a little room to move in
        g.append(f"{gm}scale=w='2*trunc({gw}*({zexpr})/2)':h='2*trunc({gh}*({zexpr})/2)':eval=frame,"
                 f"crop={gw}:{gh}:x='clip((iw-{gw})/2+{sx},0,iw-{gw})':y='clip((ih-{gh})/2+{sy},0,ih-{gh})'[gz]")
    else:
        g.append(f"{gm}null[gz]")

    # ---- composition
    t = gfx.t if gfx else None
    if lay.mode == "pro":
        pat, period = gfx.pattern()
        pi = png(pat)
        g.append(f"{pi}crop={W}:{H}:x='mod(t*{max(1, round(26 * k))},{period})':y=0,format=yuv420p[bgp]")
        g.append(f"[gz]{GRADES['pro']}[gzg]")
        g.append(f"[bgp][gzg]overlay={gx}:{gy}[c1]")
        last = "[c1]"
        if cam:
            cx, cy, cw, ch = lay.cam_box
            px, py = round(cam["x"] * sw), round(cam["y"] * sh)
            pw, ph = max(2, round(cam["w"] * sw) // 2 * 2), max(2, round(cam["h"] * sh) // 2 * 2)
            m = png(gfx.rounded_mask(cw, ch, radius=26, name="cam_mask"))
            g.append(f"[cvc]crop={pw}:{ph}:{px}:{py},scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                     f"crop={cw}:{ch},format=yuva420p[camv];{m}format=gray[cmk];[camv][cmk]alphamerge[cam]")
            g.append(f"{last}[cam]overlay={cx}:{cy}[c2]")
            last = "[c2]"
            tag = brand.get("creator_name") or brand.get("handle") or "LIVE"
            fr, pad = gfx.frame(cw, ch, name="pro_cam_frame", tag=tag.upper(), frames=16)
            fi = seq(fr)
            g.append(f"{last}{fi}overlay={cx - pad}:{cy - pad}[c3]")
            last = "[c3]"
        fr, pad = gfx.frame(gw, gh, name="pro_game_frame", stroke=6, brackets=not cam, frames=16)
        fi = seq(fr)
        g.append(f"{last}{fi}overlay={gx - pad}:{gy - pad}[c4]")
        last = "[c4]"
        for f in plan.effects:
            if f.kind == "pulse":
                for name, box in (("pulse_game", lay.game_box), ("pulse_cam", lay.cam_box)):
                    if not box:
                        continue
                    pp, ppad = gfx.pulse(box[2], box[3], name=name)
                    pin = png(pp)
                    lbl = f"[pl{name}]"
                    g.append(f"{pin}format=yuva420p,fade=t=in:st={_n(f.t)}:d=0.08:alpha=1,"
                             f"fade=t=out:st={_n(f.t + 0.12)}:d={_n(f.dur)}:alpha=1{lbl};"
                             f"{last}{lbl}overlay={box[0] - ppad}:{box[1] - ppad}:enable='between(t,{_n(f.t)},{_n(f.t + f.dur + 0.2)})'[c_{name}]")
                    last = f"[c_{name}]"
            if f.kind == "tag":
                ti = png(gfx.sticker("MOMENT", name="moment"))
                ty = (lay.game_box[1] + round(30 * k))
                g.append(f"{last}{ti}overlay=x={gx + round(30 * k)}:y='if(lt(t,{_n(f.t)}),-h,"
                         f"min({ty},-h+(t-{_n(f.t)})*{round(ty / 0.25)}))':enable='between(t,{_n(f.t)},{_n(f.t + f.dur)})'[c_tag]")
                last = "[c_tag]"
        title = next((c for c in plan_cues_hook(plan)), None) or brand.get("creator_name") or brand.get("name") or "VicePulse"
        tb = png(gfx.title_bar(title, max_w=820))
        tx, ty = round(40 * k), round(150 * k)
        g.append(f"{last}{tb}overlay=x='if(lt(t,0.15),-w,min({tx},-w+(t-0.15)*(w+{tx})/0.35))':y={ty}[c5]")
        bd = png(gfx.badge())
        g.append(f"[c5]{bd}overlay=x=W-w-{round(40 * k)}:y={round(148 * k)}[comp]")
        post = []
    else:
        last = "[gz]"
        if lay.mode == "split":
            cx, cy, cw, ch = lay.cam_box
            px, py = round(cam["x"] * sw), round(cam["y"] * sh)
            pw, ph = max(2, round(cam["w"] * sw) // 2 * 2), max(2, round(cam["h"] * sh) // 2 * 2)
            g.append(f"[cvc]crop={pw}:{ph}:{px}:{py},scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                     f"crop={cw}:{ch}[cam];[cam][gz]vstack[c1]")
            last = "[c1]"
            if gfx:
                inset = round(22 * k)
                tag = brand.get("creator_name") or brand.get("handle") or "LIVE"
                fr, pad = gfx.frame(cw - 2 * inset, ch - 2 * inset, name="viral_cam_frame", stroke=12, tag=tag.upper(),
                                    frames=12)
                fi = seq(fr, rate=12)
                g.append(f"{last}{fi}overlay={inset - pad}:{inset - pad}[c2]")
                last = "[c2]"
        elif cam and lay.cam_box:                          # cinematic round window
            cx, cy, d, _ = lay.cam_box
            fx, fy = cam.get("face", {}).get("x"), cam.get("face", {}).get("y")
            px, py = round(cam["x"] * sw), round(cam["y"] * sh)
            pw, ph = max(2, round(cam["w"] * sw) // 2 * 2), max(2, round(cam["h"] * sh) // 2 * 2)
            side = min(pw, ph) // 2 * 2
            if fx is not None:                              # centre the circle on the face
                fsz = cam["face"].get("size", 0) * sw
                px = int(min(max(px, fx * sw + fsz / 2 - side / 2), px + pw - side))
                py = int(min(max(py, fy * sh + fsz / 2 - side / 2), py + ph - side))
            m = png(gfx.circle_mask(d))
            g.append(f"[cvc]crop={side}:{side}:{px}:{py},scale={d}:{d},format=yuva420p[camv];{m}format=gray[cmk];"
                     f"[camv][cmk]alphamerge[cam];[gz][cam]overlay={cx}:{cy}[c1]")
            fr, pad = gfx.frame(d, d, name="cine_ring", stroke=7, round_=True, brackets=False)
            fi = png(fr)
            g.append(f"[c1]{fi}overlay={cx - pad}:{cy - pad}[c2]")
            last = "[c2]"
        post = [GRADES[plan.style]]
        g.append(f"{last}null[comp]")

    # ---- finishing: grade, style graphics, flash, captions
    chain = ["format=yuv420p"] + post
    if plan.style == "cinematic":
        chain.append("noise=c0s=6:c0f=t")                   # light film grain
    g.append("[comp]" + ",".join(chain) + "[f0]")
    last = "[f0]"
    if gfx and plan.style == "cinematic":
        bar = round(H * 0.05) // 2 * 2
        g.append(f"color=c=black:s={W}x{bar}:r={FPS}:d={_n(total + 0.5)},split=2[bt][bb];"
                 f"{last}[bt]overlay=x=0:y='-h+h*min(1,t/0.7)'[f1];[f1][bb]overlay=x=0:y='H-h*min(1,t/0.7)'[f2]")
        last = "[f2]"
        b = plan.story_offset()
        if b > 0:
            li = png(gfx.light_leak())
            g.append(f"{li}format=yuva420p,fade=t=in:st={_n(max(0, b - 0.3))}:d=0.2:alpha=1,"
                     f"fade=t=out:st={_n(b)}:d=0.5:alpha=1[lk];{last}[lk]overlay=0:0:"
                     f"enable='between(t,{_n(max(0, b - 0.3))},{_n(b + 0.6)})'[f3]")
            last = "[f3]"
    if gfx and plan.style == "viral":
        for i, f in enumerate(e for e in plan.effects if e.kind == "flash"):
            bi = png(gfx.burst())
            cy = (lay.game_box[1] + lay.game_box[3] // 2)
            g.append(f"{bi}format=yuva420p,fade=t=in:st={_n(f.t)}:d=0.05:alpha=1,fade=t=out:st={_n(f.t + 0.1)}:d=0.3:alpha=1[bu{i}];"
                     f"{last}[bu{i}]overlay=x=(W-w)/2:y={cy}-h/2:enable='between(t,{_n(f.t)},{_n(f.t + 0.45)})'[fb{i}]")
            last = f"[fb{i}]"
    if gfx and plan.style == "clean":
        title = brand.get("creator_name") or brand.get("name") or "VicePulse"
        sub = brand.get("handle") or ""
        lt = png(gfx.lower_third(title, sub))
        ly = round(H * 0.70)
        g.append(f"{last}{lt}overlay=x='if(lt(t,0.4),-w,if(lt(t,0.75),-w+(t-0.4)*(w+{round(48 * k)})/0.35,"
                 f"if(lt(t,4.4),{round(48 * k)},{round(48 * k)}-(t-4.4)*(w+{round(48 * k)})/0.35)))':y={ly}:"
                 f"enable='lt(t,4.8)'[f4]")
        bd = png(gfx.badge(d=84))
        g.append(f"[f4]{bd}overlay=x={round(48 * k)}:y={round(150 * k)}[f5]")
        last = "[f5]"
    if gfx and total > 6 and (reveal := gfx.logo_reveal()):
        # End card: the brand logo pops in for the last 1.8 s, above the outro line.
        t0 = total - 1.8
        li = png(reveal)
        g.append(f"{li}format=yuva420p,fade=t=in:st={_n(t0)}:d=0.25:alpha=1[lr];"
                 f"{last}[lr]overlay=x=(W-w)/2:y={round(H * 0.47)}-h:enable='gte(t,{_n(t0)})'[fl]")
        last = "[fl]"
    tail = []
    for f in plan.effects:
        if f.kind == "flash":
            tail.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{_n(f.amount)}:t=fill:"
                        f"enable='between(t,{_n(f.t)},{_n(f.t + f.dur)})'")
        if f.kind == "fade_out":
            tail.append(f"fade=t=out:st={_n(f.t)}:d={_n(f.dur)}")
    fonts = f":fontsdir={_esc(gfx_fonts())}" if gfx else ""
    tail += [f"ass={_esc(ass)}{fonts}", "format=yuv420p"]
    g.append(f"{last}" + ",".join(tail) + "[v]")

    # Audio: style polish, generated sound effects, optional voice-over (ducking the game), loudness to -14 LUFS.
    g.append(f"[ca]{AUDIO_POLISH[plan.style]}[cap]")
    sfx = _sfx_sources(plan)
    g += sfx
    mix_in = "[cap]" + "".join(f"[sfx{i}]" for i in range(len(sfx)))
    if sfx:
        g.append(f"{mix_in}amix=inputs={1 + len(sfx)}:duration=first:normalize=0[gmix]")
    else:
        g.append("[cap]anull[gmix]")
    fade = next((f for f in plan.effects if f.kind == "fade_out"), None)
    atail = f",afade=t=out:st={_n(fade.t)}:d={_n(fade.dur)}" if fade else ""
    if voice:
        g.append(f"[{n}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 "loudnorm=I=-14:TP=-1.5:LRA=11,asplit=2[vo][sc];[gmix]volume=0.6[gq];"
                 "[gq][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
                 f"[duck][vo]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9{atail}[a]")
    else:
        g.append(f"[gmix]loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.9{atail}[a]")
    return ";".join(g)


def gfx_fonts() -> str:
    from .graphics import FONT_DIR

    return str(FONT_DIR)


def plan_cues_hook(plan: EditPlan) -> list[str]:
    return [plan.hook] if getattr(plan, "hook", None) else []


def title_margin(facecam: dict | None, info: dict) -> int:
    """Title position for the classic layouts (kept for callers outside the styles)."""
    if not facecam:
        return 300
    if facecam.get("mode") == "split":
        return round(OUT_H * SPLIT_CAM) + 40
    cam_aspect = (facecam["h"] * info["height"]) / max(1e-6, facecam["w"] * info["width"])
    return round(OUT_H * (PIP_TOP + PIP_WIDTH * cam_aspect * 9 / 16) + 50)


def segment_inputs(plan: EditPlan, src_path: str, in_s: float) -> list[str]:
    """One input per story segment, seeked straight to it (a little extra at the end so the trim is exact)."""
    out: list[str] = []
    for seg in plan.segments:
        out += ["-ss", f"{in_s + seg.start:.3f}", "-t", f"{seg.end - seg.start + 0.1:.3f}", "-i", src_path]
    return out


def render_styled(src_path: str, info: dict, *, in_s: float, out_s: float, cues: list[dict], style: str,
                  platform: str, out_path: Path, size: tuple[int, int] = (OUT_W, OUT_H), encoder: str = "libx264",
                  preset: str = "medium", voice: Path | None = None, brand: dict | None = None,
                  facecam: dict | None = None, layout: str = "auto", analysis: dict | None = None,
                  plan: EditPlan | None = None, graphics: bool = True) -> dict:
    """Render one clip in one style. Pass ``analysis`` to reuse it across styles and platforms."""
    t0 = time.monotonic()
    analysis = analysis or analyse(src_path, info, in_s, out_s, cues, facecam=facecam)
    plan = plan or plan_edit(style, analysis, cues, voice=voice is not None, layout=layout)
    plan.hook = next((c["text"] for c in cues if c.get("kind") in ("hook", "hook_top")), None)
    facecam = analysis.get("facecam") if facecam in (None, "auto") else facecam
    active = analysis.get("active")
    sw, sh = (active["w"], active["h"]) if active else (info["width"], info["height"])
    lay = plan_layout(plan, size, sw, sh, facecam)
    plan.notes.extend(lay.notes)
    gfx = None
    if graphics:
        from .graphics import Assets

        gfx = Assets(out_path.parent, size, brand)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass = out_path.with_suffix(".ass")
    ass.write_text(build_styled_ass(retime_cues(cues, plan), plan, platform, brand, top_margin=lay.title_y,
                                    caption_margin=lay.caption_margin, graphics=gfx is not None), encoding="utf-8")
    extra: list[str] = []
    graph = build_graph(plan, info, ass, size, analysis["motion"], facecam=facecam, voice=voice is not None,
                        active=active, gfx=gfx, inputs=extra, brand=brand or {}, layout=lay)
    args = ["ffmpeg", "-v", "error", "-y", *segment_inputs(plan, src_path, in_s)]
    if voice:
        args += ["-i", str(voice)]
    args += extra
    script = out_path.with_suffix(".graph.txt")
    script.write_text(graph, encoding="utf-8")
    tail = ["-t", f"{plan.duration:.3f}", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", str(out_path)]
    base = args + ["-/filter_complex", str(script), "-map", "[v]", "-map", "[a]"]
    used = encoder
    try:
        media.run(base + _video_codec_args(encoder, preset) + tail)
    except Exception:
        if encoder != "nvenc":
            raise
        used = "libx264"
        media.run(base + _video_codec_args("libx264", preset) + tail)
    return {"path": str(out_path), "style": plan.style, "encoder": used, "plan": plan.as_dict(),
            "facecam": lay.cam, "layout": lay.mode, "render_s": round(time.monotonic() - t0, 2)}


# --------------------------------------------------------------------------- quality check


def quality_check(path: str | Path, *, size: tuple[int, int] | None = None, duration: float | None = None) -> dict:
    """Checks a finished render: resolution, length, audio present and near -14 LUFS, no long black screen."""
    info = media.probe(path)
    proc = media.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", "blackdetect=d=0.5:pix_th=0.08",
                      "-af", "ebur128", "-f", "null", "-"])
    issues: list[str] = []
    m = _SUMMARY_I.search(proc.stderr)
    lufs = float(m.group(1)) if m and m.group(1) != "-inf" else None
    black = sum(float(x) for x in _BLACK.findall(proc.stderr))
    if size and (info["width"], info["height"]) != tuple(size):
        issues.append(f"size {info['width']}x{info['height']}, expected {size[0]}x{size[1]}")
    if duration is not None and abs(info["duration"] - duration) > 0.6:
        issues.append(f"length {info['duration']:.1f}s, expected {duration:.1f}s")
    if not info["has_audio"]:
        issues.append("no audio track")
    elif lufs is None or not (-17.5 <= lufs <= -11.0):
        issues.append(f"loudness {lufs} LUFS, expected about -14")
    if info["duration"] and black / info["duration"] > 0.2:
        issues.append(f"{black:.1f}s of black screen")
    return {"ok": not issues, "issues": issues, "width": info["width"], "height": info["height"],
            "duration": round(info["duration"], 2), "lufs": lufs, "black_s": round(black, 2),
            "size_mb": round(Path(path).stat().st_size / 1e6, 2)}
