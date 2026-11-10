"""FFmpeg render: cut, reframe to 9:16, burn captions, optional voice mix, 1080x1920 H.264.

One template, driven by the clip's edit data. Each platform gets its own file so caption
safe areas can differ.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageChops

from . import db, media, rights
from .config import PLATFORMS, Settings
from .voice import VoiceProvider, make_voice

OUT_W, OUT_H, FPS = 1080, 1920, 30

# Bottom margin (px) that keeps captions clear of each app's own buttons and text.
CAPTION_MARGIN_V = {"youtube": 420, "instagram": 480, "tiktok": 520}
FONT = os.environ.get("VICEPULSE_CAPTION_FONT", "DejaVu Sans")


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(cues: list[dict], platform: str) -> str:
    margin = CAPTION_MARGIN_V[platform]
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {OUT_W}\nPlayResY: {OUT_H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Cap,{FONT},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,{margin},1\n"
        f"Style: Hook,{FONT},96,&H0000F6FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,80,{margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for c in cues:
        text = c["text"].replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
        style = "Hook" if c.get("kind") == "hook" else "Cap"
        lines.append(f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},{style},,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


def action_center(src: str | Path, in_s: float, out_s: float, work: Path, samples: int = 6) -> tuple[float, float]:
    """Where the motion is, horizontally: (centre 0..1, spread 0..1) from frame differences.

    Used to place a static 9:16 crop for the clip. A wide spread means a crop would cut
    off action, so the caller falls back to the blurred-fill layout.
    """
    work.mkdir(parents=True, exist_ok=True)
    frames = []
    for k in range(samples):
        t = in_s + (out_s - in_s) * (k + 0.5) / samples
        f = media.extract_frame(src, t, work / f"motion_{k}.jpg", width=96)
        frames.append(Image.open(f).convert("L"))
    width, height = frames[0].size
    cols = [0.0] * width
    for a, b in zip(frames, frames[1:]):
        px = list(ImageChops.difference(a, b).get_flattened_data())
        for x in range(width):
            cols[x] += sum(px[x::width])
    total = sum(cols)
    if total <= 0:
        return 0.5, 1.0
    centre = sum(x * v for x, v in enumerate(cols)) / total
    var = sum(((x - centre) ** 2) * v for x, v in enumerate(cols)) / total
    return centre / (width - 1), min(1.0, (var ** 0.5) / (width / 2))


def _esc(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def video_filter(src_w: int, src_h: int, ass: Path, *, layout: str, centre: float = 0.5) -> str:
    if layout == "crop":
        scaled_w = src_w * OUT_H / src_h
        x = max(0, min(int(scaled_w * centre - OUT_W / 2), int(scaled_w - OUT_W)))
        base = f"[0:v]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H}:{x}:0[base]"
    else:  # blurred fill behind the full frame
        base = (f"[0:v]split=2[bgsrc][fgsrc];"
                f"[bgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},boxblur=20:5[bg];"
                f"[fgsrc]scale={OUT_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    return f"{base};[base]fps={FPS},setsar=1,ass={_esc(ass)},format=yuv420p[v]"


def render_clip_platform(src_path: str, src_info: dict, *, in_s: float, out_s: float, cues: list[dict],
                         platform: str, out_path: Path, voice: Path | None, layout: str, centre: float,
                         preset: str = "medium") -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass = out_path.with_suffix(".ass")
    ass.write_text(build_ass(cues, platform), encoding="utf-8")
    dur = out_s - in_s
    args = ["ffmpeg", "-v", "error", "-y", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", src_path]
    graph = [video_filter(src_info["width"], src_info["height"], ass, layout=layout, centre=centre)]
    if voice:
        args += ["-i", str(voice)]
    if not src_info["has_audio"]:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    game = "[0:a]" if src_info["has_audio"] else f"[{2 if voice else 1}:a]"
    if voice:
        # Voice on top, game audio ducked underneath whenever the voice is speaking.
        graph.append(f"[1:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11,asplit=2[vo][sc];"
                     f"{game}aresample=48000,volume=0.6[g];"
                     f"[g][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
                     f"[duck][vo]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9[a]")
    elif src_info["has_audio"]:
        graph.append(f"{game}aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    else:
        graph.append(f"{game}anull[a]")
    args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", preset, "-crf", "20", "-profile:v", "high", "-pix_fmt", "yuv420p",
             "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-t", f"{dur:.3f}",
             "-movflags", "+faststart", str(out_path)]
    media.run(args)
    return out_path


def render_pending(conn, settings: Settings, voice: VoiceProvider | None = None, *,
                   clip_ids: list[int] | None = None, layout: str = "auto") -> dict:
    voice = voice or make_voice(settings)
    preset = os.environ.get("VICEPULSE_X264_PRESET", "medium")
    rows = conn.execute(
        """SELECT cl.*, ch.key AS channel_key, c.source_id, s.file_path FROM clips cl
           JOIN channels ch ON ch.id = cl.channel_id
           JOIN candidates c ON c.id = cl.candidate_id JOIN sources s ON s.id = c.source_id
           WHERE cl.status = 'scripted' AND (%s::int[] IS NULL OR cl.id = ANY(%s::int[])) ORDER BY cl.id""",
        (clip_ids, clip_ids)).fetchall()
    out = {"rendered": [], "files": []}
    for clip in rows:
        rights.require_cleared_source(conn, clip["source_id"])  # never render uncleared footage
        info = media.probe(clip["file_path"])
        work = settings.dir("work", f"clip{clip['id']}")
        use_layout, centre = layout, 0.5
        if layout in ("auto", "crop"):
            centre, spread = action_center(clip["file_path"], clip["in_s"], clip["out_s"], work)
            use_layout = "crop" if (layout == "crop" or spread < 0.55) else "blur"
        voice_path = voice.voice_for(dict(clip))
        for platform in PLATFORMS:
            path = settings.dir("renders", clip["channel_key"], str(clip["for_date"])) / f"clip{clip['id']}_{platform}.mp4"
            render_clip_platform(clip["file_path"], info, in_s=clip["in_s"], out_s=clip["out_s"],
                                 cues=clip["script"]["captions"], platform=platform, out_path=path,
                                 voice=voice_path, layout=use_layout, centre=centre, preset=preset)
            meta = media.probe(path)
            conn.execute(
                """INSERT INTO renders (clip_id, platform, file_path, file_sha256, width, height, duration_s,
                                        has_voice, has_captions)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                   ON CONFLICT (clip_id, platform) DO UPDATE SET file_path = EXCLUDED.file_path,
                       file_sha256 = EXCLUDED.file_sha256, width = EXCLUDED.width, height = EXCLUDED.height,
                       duration_s = EXCLUDED.duration_s, has_voice = EXCLUDED.has_voice, created_at = now()""",
                (clip["id"], platform, str(path), media.sha256_file(path), meta["width"], meta["height"],
                 meta["duration"], voice_path is not None))
            out["files"].append(str(path))
        conn.execute("UPDATE clips SET status = 'rendered', voice_path = %s WHERE id = %s",
                     (str(voice_path) if voice_path else None, clip["id"]))
        db.log_event(conn, "render", "clip", clip["id"], "rendered",
                     {"layout": use_layout, "voice": voice_path is not None})
        out["rendered"].append(clip["id"])
        conn.commit()
    return out
