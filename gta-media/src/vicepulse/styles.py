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

STYLES = ("cinematic", "viral", "clean")
ALIASES = {"funny": "viral", "funny_viral": "viral", "professional": "clean", "clean_professional": "clean"}
LABELS = {"cinematic": "Cinematic", "viral": "Funny/Viral", "clean": "Clean Professional"}

_EBU = re.compile(r"t:\s*([0-9.]+)\s+TARGET:.*?M:\s*(-?[0-9.]+|-inf)")
_PTS = re.compile(r"pts_time:([0-9.]+)")
_SUMMARY_I = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?[0-9.]+|-inf) LUFS")
_BLACK = re.compile(r"black_duration:\s*([0-9.]+)")


def style_name(value: str | None) -> str:
    v = (value or "").strip().lower().replace("-", "_").replace("/", "_")
    v = ALIASES.get(v, v)
    if v not in STYLES:
        raise ValueError(f"unknown edit style {value!r}: use cinematic, viral (funny) or clean")
    return v


# --------------------------------------------------------------------------- analysis


@dataclass
class Event:
    t: float            # seconds from the clip start
    kind: str           # loud | cut | reaction
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


def motion_track(path, in_s: float, dur: float, src_w: int, src_h: int, *, fps: int = 4, width: int = 96) -> dict:
    """Where the action is, horizontally, over time: smoothed keyframes [(t, centre 0..1)] and overall spread."""
    h = max(2, int(round(width * src_h / max(src_w, 1) / 2)) * 2)
    raw = media.run_bytes(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                           "-an", "-vf", f"fps={fps},scale={width}:{h},format=gray", "-f", "rawvideo", "-"])
    n = width * h
    frames = [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]
    cols_all = [0.0] * width
    bins: dict[int, list[float]] = {}
    for k in range(1, len(frames)):
        a, b = frames[k - 1], frames[k]
        cols = [0.0] * width
        for y in range(0, h, 2):                      # every other row: same answer, half the work
            row = y * width
            for x in range(width):
                cols[x] += abs(a[row + x] - b[row + x])
        cols = [c if c > 3 * (h // 2) else 0.0 for c in cols]     # ignore sensor/compression noise
        energy = sum(cols)
        sec = int(k / fps)
        acc = bins.setdefault(sec, [0.0, 0.0])
        acc[0] += sum(x * c for x, c in enumerate(cols))
        acc[1] += energy
        for x in range(width):
            cols_all[x] += cols[x]
    total = sum(cols_all)
    if total <= 0:
        return {"keys": [(0.0, 0.5)], "spread": 1.0, "centre": 0.5}
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
    return {"keys": out, "spread": round(spread, 3), "centre": round(centre / (width - 1), 3)}


def analyse(path, info: dict, in_s: float, out_s: float, cues: list[dict] | None = None) -> dict:
    dur = out_s - in_s
    t0 = time.monotonic()
    loud, median = loudness_events(path, in_s, dur) if info.get("has_audio") else ([], -120.0)
    cuts = cut_events(path, in_s, dur)
    react = reaction_events(cues or [])
    motion = motion_track(path, in_s, dur, info["width"], info["height"])
    return {"duration": round(dur, 3), "events": sorted(loud + cuts + react, key=lambda e: e.t),
            "median_lufs": median, "motion": motion, "src_fps": info.get("fps") or 30.0,
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
    kind: str             # zoom | push | shake | flash | whoosh | hit | fade_out
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
        if e.kind != "loud":
            continue
        bonus = 0.25 if any(r.kind == "reaction" and 0 <= r.t - e.t <= 2.5 for r in events) else 0.0
        if e.strength + bonus > best_score:
            best, best_score = e, e.strength + bonus
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
    if layout == "auto":
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
    # Natural end: let the payoff land, and keep anything that still happens after it (a second, smaller
    # bang, a scene change, a spoken reaction), plus a short beat after the last of those.
    after = [e.t for e in events if e.t > p + 0.5]
    end = max(p + 4.0, (max(after) + 2.5) if after else 0.0)
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
        notes.append(f"ends {dur - end:.1f}s early: no loud moment, reaction or scene change after {end - 2.5:.1f}s")

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
            if e.kind not in ("loud", "reaction") or e.strength < 0.35:
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
                     top_margin: int = 300) -> str:
    """top_margin: where titles sit (1920-high canvas); pushed down when a facecam occupies the top."""
    brand = {**DEFAULT_BRAND, **(brand or {})}
    margin = max(CAPTION_MARGIN_V.values()) if platform == "shared" else CAPTION_MARGIN_V[platform]
    accent = _ass_colour(brand["colour"])
    font = os.environ.get("VICEPULSE_CAPTION_FONT", "DejaVu Sans")
    serif = os.environ.get("VICEPULSE_TITLE_FONT_SERIF", "DejaVu Serif")
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
           "MarginR, MarginV, Encoding")
    S = plan.style
    if S == "cinematic":
        styles = [f"Style: Cap,{font},66,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,-1,0,0,0,100,100,1,0,1,3,4,2,90,90,{margin},1",
                  f"Style: Hook,{serif},82,&H00F0F0F0,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,2,0,1,3,5,8,90,90,{top_margin},1"]
    elif S == "viral":
        styles = [f"Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,3,2,70,70,{margin + 60},1",
                  f"Style: Hook,{font},104,{accent},&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,3,8,70,70,{top_margin},1"]
    else:
        box = _ass_colour("#101418", 0x30)
        styles = [f"Style: Cap,{font},60,&H00FFFFFF,&H00FFFFFF,{box},&H00000000,-1,0,0,0,100,100,0,0,3,14,0,2,100,100,{margin},1",
                  f"Style: Hook,{font},70,&H00FFFFFF,&H00FFFFFF,{box},&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,100,100,{top_margin},1"]
    styles += [f"Style: Tag,{font},40,&H50FFFFFF,&H00FFFFFF,&H80000000,&H00000000,-1,0,0,0,100,100,1,0,1,2,0,7,70,70,230,1",
               f"Style: Outro,{font},70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,5,90,90,0,1"]
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {OUT_W}\nPlayResY: {OUT_H}\nWrapStyle: 0\n"
            f"ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n{fmt}\n" + "\n".join(styles) +
            "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines: list[str] = []

    def add(start, end, style, text, layer=0):
        if end > start:
            lines.append(f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{style},,0,0,0,,{text}")

    reaction_words = {w for c in cues if c.get("kind") == "speech" for w in c["text"].lower().split()
                      if any(p.search(w) for p in signals._PHRASES)}
    for c in cues:
        text = _clean_text(c["text"])
        is_hook = c.get("kind") in ("hook", "hook_top")
        st = "Hook" if is_hook else "Cap"
        if S == "cinematic":
            add(c["start"], c["end"], st, r"{\fad(220,200)}" + text)
        elif S == "clean":
            add(c["start"], c["end"], st, r"{\fad(120,120)}" + text)
        elif is_hook:
            add(c["start"], c["end"], st, r"{\fscx130\fscy130\t(0,160,\fscx100\fscy100)}" + text.upper())
        else:
            # Word-by-word highlight. The cue's words are spread evenly over the cue (the cue itself is in sync).
            words = text.upper().split()
            step = (c["end"] - c["start"]) / max(1, len(words))
            for i in range(len(words)):
                parts = []
                for j, w in enumerate(words):
                    if j == i:
                        col = "&H004040FF&" if w.lower().strip(".,!?") in reaction_words else accent + "&"
                        parts.append(r"{\c" + col + r"\fscx112\fscy112}" + w + r"{\r}")
                    else:
                        parts.append(w)
                pop = r"{\fscx85\fscy85\t(0,90,\fscx100\fscy100)}" if i == 0 else ""
                add(c["start"] + i * step, c["start"] + (i + 1) * step, st, pop + " ".join(parts))
    total = plan.duration
    tag = brand.get("credit") or brand.get("handle")
    if tag and S == "clean":
        add(0.0, total, "Tag", _clean_text(tag), layer=1)
    if brand.get("intro_text") and not any(c.get("kind") in ("hook", "hook_top") for c in cues):
        add(0.0, min(1.4, total), "Hook", r"{\fad(150,200)}" + _clean_text(brand["intro_text"]), layer=1)
    if brand.get("outro_text") and total > 6:
        add(total - 1.8, total, "Outro", r"{\fad(250,0)}" + _clean_text(brand["outro_text"]), layer=2)
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
}
AUDIO_POLISH = {
    "cinematic": "equalizer=f=90:t=q:w=1:g=3,acompressor=threshold=0.125:ratio=2.5:attack=15:release=250",
    "viral": "acompressor=threshold=0.1:ratio=4:attack=5:release=120",
    "clean": "highpass=f=60,acompressor=threshold=0.18:ratio=2:attack=20:release=300",
}


def build_graph(plan: EditPlan, info: dict, ass: Path, size: tuple[int, int], motion: dict, *,
                facecam: dict | None = None, voice: bool = False) -> str:
    """Input i is story segment i, already cut to that segment (see ``segment_inputs``); input n is the voice.

    Each segment is read from the file on its own. Splitting one input instead makes FFmpeg hold every
    frame between the teaser and the rest of the story in memory (over 1 GB at 720p60) - too much for this PC.
    """
    W, H = size
    segs = plan.segments
    n = len(segs)
    g: list[str] = []
    has_audio = bool(info.get("has_audio"))
    fade_col = "white" if plan.transition == "white" else "black"
    for i, s in enumerate(segs):
        seg_len = s.end - s.start
        v = f"[{i}:v]trim=duration={_n(seg_len)},setpts=(PTS-STARTPTS)/{_n(s.speed)}"
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

    # Framing on the edited timeline.
    sw, sh = info["width"], info["height"]
    keys = output_keys(plan, [tuple(k) for k in motion["keys"]])
    cam = facecam or None
    mode = (cam or {}).get("mode", "pip")
    gh = H - (round(H * 0.36 / 2) * 2 if cam and mode == "split" else 0)        # gameplay area height
    g.append(f"[cv]fps={FPS},setsar=1" + ("[cvx];[cvx]split=2[cvr][cvc]" if cam else "[cvg]"))
    if cam:
        # The facecam is shown on its own, so paint over its original corner in the gameplay picture.
        dx, dy = max(1, round(cam["x"] * sw)), max(1, round(cam["y"] * sh))
        dw = max(4, min(round(cam["w"] * sw), sw - dx - 1))
        dh = max(4, min(round(cam["h"] * sh), sh - dy - 1))
        g.append(f"[cvr]delogo=x={dx}:y={dy}:w={dw}:h={dh}[cvg]")
    if plan.layout == "crop":
        scaled_w = sw * gh / sh
        span = max(0.0, scaled_w - W)
        xkeys = [(t, min(span, max(0.0, scaled_w * c - W / 2))) for t, c in keys]
        g.append(f"[cvg]scale=-2:{gh},crop={W}:{gh}:x='{_lerp_expr(xkeys)}':y=0[gm]")
    else:
        bw, bh = max(2, W // 4 // 2 * 2), max(2, gh // 4 // 2 * 2)
        g.append(f"[cvg]split=2[bgs][fgs];[bgs]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                 f"boxblur={max(2, round(5 * H / OUT_H))}:3,scale={W}:{gh}[bg];[fgs]scale={W}:-2[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2[gm]")
    z = _zoom_terms(plan)
    sx, sy = _shake_terms(plan, W)
    if z or sx != "0":
        zexpr = f"min(1.4,1+{z})" if z else "1.06"          # shake alone needs a little room to move in
        g.append(f"[gm]scale=w='2*trunc({W}*({zexpr})/2)':h='2*trunc({gh}*({zexpr})/2)':eval=frame,"
                 f"crop={W}:{gh}:x='clip((iw-{W})/2+{sx},0,iw-{W})':y='clip((ih-{gh})/2+{sy},0,ih-{gh})'[gz]")
    else:
        g.append("[gm]null[gz]")
    if cam:
        cx, cy = round(cam["x"] * sw), round(cam["y"] * sh)
        cw, ch = max(2, round(cam["w"] * sw) // 2 * 2), max(2, round(cam["h"] * sh) // 2 * 2)
        if mode == "split":
            g.append(f"[cvc]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H - gh}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H - gh}[cam];[cam][gz]vstack[comp]")
        else:
            pw = round(W * PIP_WIDTH / 2) * 2
            border = max(2, round(6 * W / OUT_W))
            g.append(f"[cvc]crop={cw}:{ch}:{cx}:{cy},scale={pw}:-2,pad=iw+{2 * border}:ih+{2 * border}:{border}:{border}:white[cam];"
                     f"[gz][cam]overlay=x=W-w-{round(W * 0.05)}:y={round(H * PIP_TOP)}[comp]")
    else:
        g.append("[gz]null[comp]")
    post = ["format=yuv420p", GRADES[plan.style]]
    for f in plan.effects:
        if f.kind == "flash":
            post.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{_n(f.amount)}:t=fill:"
                        f"enable='between(t,{_n(f.t)},{_n(f.t + f.dur)})'")
        if f.kind == "fade_out":
            post.append(f"fade=t=out:st={_n(f.t)}:d={_n(f.dur)}")
    post.append(f"ass={_esc(ass)}")
    post.append("format=yuv420p")         # ass may hand back another pixel format
    g.append("[comp]" + ",".join(post) + "[v]")

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
    tail = f",afade=t=out:st={_n(fade.t)}:d={_n(fade.dur)}" if fade else ""
    if voice:
        g.append(f"[{n}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 "loudnorm=I=-14:TP=-1.5:LRA=11,asplit=2[vo][sc];[gmix]volume=0.6[gq];"
                 "[gq][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
                 f"[duck][vo]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9{tail}[a]")
    else:
        g.append(f"[gmix]loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.9{tail}[a]")
    return ";".join(g)


PIP_TOP = 0.11          # facecam picture-in-picture: top right, below the apps' top bar
PIP_WIDTH = 0.36


def title_margin(facecam: dict | None, info: dict) -> int:
    """Title position (on the 1920-high caption canvas) that stays clear of a repositioned facecam."""
    if not facecam:
        return 300
    if facecam.get("mode") == "split":
        return round(OUT_H * 0.36) + 40
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
                  plan: EditPlan | None = None) -> dict:
    """Render one clip in one style. Pass ``analysis`` to reuse it across styles and platforms."""
    t0 = time.monotonic()
    analysis = analysis or analyse(src_path, info, in_s, out_s, cues)
    plan = plan or plan_edit(style, analysis, cues, voice=voice is not None, layout=layout)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass = out_path.with_suffix(".ass")
    ass.write_text(build_styled_ass(retime_cues(cues, plan), plan, platform, brand,
                                    top_margin=title_margin(facecam, info)), encoding="utf-8")
    args = ["ffmpeg", "-v", "error", "-y", *segment_inputs(plan, src_path, in_s)]
    graph = build_graph(plan, info, ass, size, analysis["motion"], facecam=facecam, voice=voice is not None)
    if voice:
        args += ["-i", str(voice)]
    script = out_path.with_suffix(".graph.txt")
    script.write_text(graph, encoding="utf-8")
    tail = ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out_path)]
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
            "render_s": round(time.monotonic() - t0, 2)}


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
