"""FFmpeg render: cut, reframe to 9:16, burn captions, optional voice mix, 1080x1920 H.264.

One template, driven by the clip's edit data. Each platform gets its own file so caption
safe areas can differ.

Faster mode (used for live clips): a smaller size such as 720x1280, the GPU encoder (NVENC) when the
container can reach it, and ``reuse=True``, which encodes once with the strictest caption safe area and
reuses that file for every platform instead of encoding three times.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageChops

from datetime import datetime, timezone

from . import db, media, rights
from .config import PLATFORMS, Settings
from .voice import VoiceProvider, make_voice

OUT_W, OUT_H, FPS = 1080, 1920, 30

# Bottom margin (px) that keeps captions clear of each app's own buttons and text.
CAPTION_MARGIN_V = {"youtube": 420, "instagram": 480, "tiktok": 520, "facebook": 480}
FONT = os.environ.get("VICEPULSE_CAPTION_FONT", "DejaVu Sans")


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(cues: list[dict], platform: str) -> str:
    # "shared": one file for every platform, so it keeps clear of the tallest app overlay.
    margin = max(CAPTION_MARGIN_V.values()) if platform == "shared" else CAPTION_MARGIN_V[platform]
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {OUT_W}\nPlayResY: {OUT_H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Cap,{FONT},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,{margin},1\n"
        f"Style: Hook,{FONT},96,&H0000F6FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,80,{margin},1\n"
        f"Style: HookTop,{FONT},88,&H0000F6FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,8,80,80,260,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for c in cues:
        text = c["text"].replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
        style = {"hook": "Hook", "hook_top": "HookTop"}.get(c.get("kind"), "Cap")
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


def video_filter(src_w: int, src_h: int, ass: Path, *, layout: str, centre: float = 0.5,
                 size: tuple[int, int] = (OUT_W, OUT_H)) -> str:
    # Captions are laid out on a 1080x1920 canvas (PlayRes); libass scales them to the real output size.
    w, h = size
    blur = max(6, round(20 * h / OUT_H))
    if layout == "crop":
        scaled_w = src_w * h / src_h
        x = max(0, min(int(scaled_w * centre - w / 2), int(scaled_w - w)))
        base = f"[0:v]scale=-2:{h},crop={w}:{h}:{x}:0[base]"
    else:  # blurred fill behind the full frame (blurred at quarter size: same look, far less CPU)
        base = (f"[0:v]split=2[bgsrc][fgsrc];"
                f"[bgsrc]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
                f"boxblur={max(2, blur // 4)}:3,scale={w}:{h}[bg];"
                f"[fgsrc]scale={w}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    return f"{base};[base]fps={FPS},setsar=1,ass={_esc(ass)},format=yuv420p[v]"


def _same_file(src: Path, dest: Path) -> None:
    """dest becomes the same video as src: a hard link (no extra disk) or, if that is not possible, a copy."""
    dest.unlink(missing_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)


def render_size(value: str | None = None) -> tuple[int, int]:
    """'720x1280' -> (720, 1280). Default from VICEPULSE_RENDER_SIZE, else 1080x1920."""
    value = value or os.environ.get("VICEPULSE_RENDER_SIZE") or f"{OUT_W}x{OUT_H}"
    w, h = (int(x) for x in value.lower().split("x"))
    if w % 2 or h % 2 or not (360 <= w <= 2160 and w * 16 == h * 9):
        raise ValueError(f"render size must be an even 9:16 size such as 720x1280, got {value}")
    return w, h


_NVENC_OK: bool | None = None


def nvenc_available() -> bool:
    """True when this container can use the NVIDIA hardware encoder (checked once with a 1 s test encode)."""
    global _NVENC_OK
    if _NVENC_OK is None:
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=256x256:rate=30", "-t", "1",
                            "-c:v", "h264_nvenc", "-f", "null", "-"], check=True, capture_output=True, timeout=30)
            _NVENC_OK = True
        except (subprocess.SubprocessError, OSError):
            _NVENC_OK = False
    return _NVENC_OK


def pick_encoder(choice: str | None = None) -> str:
    """libx264 | nvenc | auto (nvenc when available, else libx264). Default VICEPULSE_VIDEO_ENCODER or libx264."""
    choice = (choice or os.environ.get("VICEPULSE_VIDEO_ENCODER") or "libx264").lower()
    if choice == "auto":
        return "nvenc" if nvenc_available() else "libx264"
    if choice not in ("libx264", "nvenc"):
        raise ValueError(f"unknown video encoder {choice!r} (libx264, nvenc or auto)")
    return choice


def _video_codec_args(encoder: str, preset: str) -> list[str]:
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "23", "-b:v", "0",
                "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", "20", "-profile:v", "high"]


def render_clip_platform(src_path: str, src_info: dict, *, in_s: float, out_s: float, cues: list[dict],
                         platform: str, out_path: Path, voice: Path | None, layout: str, centre: float,
                         preset: str = "medium", size: tuple[int, int] = (OUT_W, OUT_H),
                         encoder: str = "libx264") -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass = out_path.with_suffix(".ass")
    ass.write_text(build_ass(cues, platform), encoding="utf-8")
    dur = out_s - in_s
    args = ["ffmpeg", "-v", "error", "-y", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", src_path]
    graph = [video_filter(src_info["width"], src_info["height"], ass, layout=layout, centre=centre, size=size)]
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
    tail = ["-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-t", f"{dur:.3f}",
            "-movflags", "+faststart", str(out_path)]
    base = args + ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]"]
    try:
        media.run(base + _video_codec_args(encoder, preset) + tail)
    except Exception:
        if encoder != "nvenc":
            raise
        media.run(base + _video_codec_args("libx264", preset) + tail)   # GPU busy or gone: CPU instead
    return out_path


def default_facecam() -> str | None:
    """Facecam handling for pipeline renders: 'auto' (detect it; needs OpenCV, otherwise no facecam) or 'none'."""
    return None if os.environ.get("VICEPULSE_FACECAM", "auto") == "none" else "auto"


def channel_brand(settings: Settings, channel_key: str) -> dict:
    """Branding for a channel's styled edits (channels.json "brand"), e.g. handle, accent colour, outro line."""
    try:
        ch = settings.channel(channel_key)
    except (KeyError, OSError):
        return {}
    return {"name": ch.get("name", ""), **(ch.get("brand") or {})}


def edit_style(value: str | None = None) -> str | None:
    """None = the classic template; else cinematic | viral | clean (default VICEPULSE_EDIT_STYLE)."""
    from . import styles

    value = value or os.environ.get("VICEPULSE_EDIT_STYLE", "")
    return None if value.strip().lower() in ("", "classic", "none") else styles.style_name(value)


def compare_styles(src: str, *, in_s: float, out_s: float | None, cues: list[dict], styles_: list[str], out_dir: str,
                   size: str = "720x1280", encoder: str = "auto", platform: str = "shared", brand: dict | None = None,
                   facecam: str | dict | None = None, layout: str = "auto") -> dict:
    """The same highlight in several styles: one analysis, one render per style, quality checks, and a
    side-by-side preview video for comparing them."""
    from . import styles

    info = media.probe(src)
    out_s = out_s if out_s is not None else info["duration"]
    out_size = render_size(size)
    enc = pick_encoder(encoder)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    analysis = styles.analyse(src, info, in_s, out_s, cues, facecam=facecam)
    results = []
    for name in [styles.style_name(x) for x in styles_]:
        res = styles.render_styled(src, info, in_s=in_s, out_s=out_s, cues=cues, style=name, platform=platform,
                                   out_path=folder / f"{name}.mp4", size=out_size, encoder=enc, preset="veryfast",
                                   brand=brand, facecam=analysis["facecam"], analysis=analysis, layout=layout)
        res["qc"] = styles.quality_check(res["path"], size=out_size, duration=res["plan"]["duration"])
        results.append(res)
    preview = folder / "side_by_side.mp4"
    n = len(results)
    if n > 1:
        ins = sum((["-i", r["path"]] for r in results), [])
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        lab = "".join(f"[{i}:v]scale=360:640,drawtext=fontfile={font}:text='{styles.LABELS[r['style']]}':x=(w-tw)/2:y=24:"
                      f"fontsize=26:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=8[p{i}];" for i, r in enumerate(results))
        graph = lab + "".join(f"[p{i}]" for i in range(n)) + f"hstack=inputs={n}[v]"
        media.run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
                   *_video_codec_args(enc, "veryfast"), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   str(preview)])
    analysis_out = {**analysis, "events": [vars(e) for e in analysis["events"]]}
    return {"source": src, "in_s": in_s, "out_s": out_s, "size": f"{out_size[0]}x{out_size[1]}", "encoder": enc,
            "analysis": analysis_out, "styles": results, "side_by_side": str(preview) if n > 1 else None}


def client_rights(conn, client_id: int) -> dict:
    """The client's verified, unrevoked rights entry that allows clip creation, or PermissionError."""
    row = conn.execute(
        """SELECT r.* FROM rights_ledger r WHERE r.id IN (SELECT rights_id FROM client_permissions
               WHERE client_id = %s AND status = 'verified' AND rights_id IS NOT NULL)
           ORDER BY r.id DESC LIMIT 1""", (client_id,)).fetchone()
    decision = rights.check_grant(row, scope="clip_creation")
    if not decision.ok:
        raise PermissionError("this creator has no verified permission for clip creation: " + "; ".join(decision.reasons))
    return row


def render_for_client(conn, settings: Settings, client_slug: str, src: str, *, in_s: float, out_s: float | None,
                      cues: list[dict] | None = None, title: str, size: str | None = None,
                      encoder: str = "auto") -> dict:
    """Edit one moment for a creator client in their own style and branding and put it in their dashboard.

    Needs the client's verified clip_creation permission (checked before the footage is read). Nothing is added
    to the VicePulse channels' sources, clips or publishing queue."""
    from . import clients, styles

    c = conn.execute("SELECT * FROM clients WHERE slug = %s", (client_slug,)).fetchone()
    if c is None:
        raise ValueError(f"no client {client_slug!r}")
    if c["status"] not in ("active", "pending"):
        raise PermissionError(f"client {client_slug} is {c['status']}")
    client_rights(conn, c["id"])
    opts = clients.render_options(conn, c)
    used = clients.usage(conn, c["id"])
    if used["clips_left"] <= 0:
        raise PermissionError(f"the {used['plan_name']} plan's {used['clips_included']} clips for this month are used up")
    info = media.probe(src)
    out_s = out_s if out_s is not None else info["duration"]
    out_size = render_size(size or opts["size"])
    cues = cues if opts["captions"] else []
    cues = cues or []
    fc = opts["facecam"]
    facecam = fc if fc in (None, "auto") else _parse_facecam(fc)
    folder = settings.dir("clients", str(c["id"]), "deliveries")
    out = folder / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{opts['style']}.mp4"
    res = styles.render_styled(src, info, in_s=in_s, out_s=out_s, cues=cues, style=opts["style"], platform="shared",
                               out_path=out, size=out_size, encoder=pick_encoder(encoder), preset="veryfast",
                               brand=opts["brand"], facecam=facecam, layout=opts["layout"])
    qc = styles.quality_check(out, size=out_size, duration=res["plan"]["duration"])
    if not qc.get("ok"):
        raise RuntimeError(f"quality check failed, not delivered: {qc}")
    did = clients.add_delivery(conn, client_id=c["id"], file_path=str(out), title=title, style=opts["style"],
                               duration_s=res["plan"]["duration"], file_sha256=media.sha256_file(out))
    db.log_event(conn, "render", "client_delivery", did, "rendered",
                 {"client": client_slug, "style": opts["style"], "plan": res["plan"], "qc": qc})
    conn.commit()
    return {"delivery": did, "file": str(out), "style": opts["style"], "qc": qc, "facecam": res.get("facecam"),
            "render_s": res.get("render_s")}


def _parse_facecam(text: str) -> dict:
    box, _, mode = text.partition(":")
    x, y, w, h = (float(v) for v in box.split(","))
    return {"x": x, "y": y, "w": w, "h": h, "mode": mode or "pip"}


def render_pending(conn, settings: Settings, voice: VoiceProvider | None = None, *,
                   clip_ids: list[int] | None = None, layout: str = "auto", size: str | None = None,
                   encoder: str | None = None, reuse: bool | None = None, preset: str | None = None,
                   style: str | None = None) -> dict:
    """Render every scripted clip. size/encoder/reuse/preset default to the VICEPULSE_RENDER_* settings.

    style: cinematic | viral | clean uses the event-driven edit styles (styles.py); None keeps the classic template
    (default VICEPULSE_EDIT_STYLE)."""
    style = edit_style(style)
    voice = voice or make_voice(settings)
    preset = preset or os.environ.get("VICEPULSE_X264_PRESET", "medium")
    out_size = render_size(size)
    enc = pick_encoder(encoder)
    if reuse is None:
        reuse = os.environ.get("VICEPULSE_RENDER_REUSE", "0") == "1"
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
        shared: Path | None = None
        styled: dict | None = None
        analysis = None
        if style:
            from . import styles

            cues = clip["script"]["captions"]
            analysis = styles.analyse(clip["file_path"], info, clip["in_s"], clip["out_s"], cues,
                                      facecam=default_facecam())
        for platform in PLATFORMS:
            path = settings.dir("renders", clip["channel_key"], str(clip["for_date"])) / f"clip{clip['id']}_{platform}.mp4"
            if reuse and shared is not None:
                _same_file(shared, path)       # one encode for every platform
            elif style:
                styled = styles.render_styled(
                    clip["file_path"], info, in_s=clip["in_s"], out_s=clip["out_s"], cues=cues, style=style,
                    platform="shared" if reuse else platform, out_path=path, size=out_size, encoder=enc,
                    preset=preset, voice=voice_path, brand=channel_brand(settings, clip["channel_key"]),
                    layout=layout, analysis=analysis)
                styled["qc"] = styles.quality_check(path, size=out_size, duration=styled["plan"]["duration"])
                shared = path
            else:
                render_clip_platform(clip["file_path"], info, in_s=clip["in_s"], out_s=clip["out_s"],
                                     cues=clip["script"]["captions"], platform="shared" if reuse else platform,
                                     out_path=path, voice=voice_path, layout=use_layout, centre=centre, preset=preset,
                                     size=out_size, encoder=enc)
                shared = path
            meta = media.probe(path)
            conn.execute(
                """INSERT INTO renders (clip_id, platform, file_path, file_sha256, width, height, duration_s,
                                        has_voice, has_captions)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (clip_id, platform) DO UPDATE SET file_path = EXCLUDED.file_path,
                       file_sha256 = EXCLUDED.file_sha256, width = EXCLUDED.width, height = EXCLUDED.height,
                       duration_s = EXCLUDED.duration_s, has_voice = EXCLUDED.has_voice,
                       has_captions = EXCLUDED.has_captions, created_at = now()""",
                (clip["id"], platform, str(path), media.sha256_file(path), meta["width"], meta["height"],
                 meta["duration"], voice_path is not None, bool(clip["script"]["captions"])))
            out["files"].append(str(path))
        conn.execute("UPDATE clips SET status = 'rendered', voice_path = %s WHERE id = %s",
                     (str(voice_path) if voice_path else None, clip["id"]))
        detail = {"layout": use_layout, "voice": voice_path is not None, "size": f"{out_size[0]}x{out_size[1]}",
                  "encoder": enc, "encodes": 1 if reuse else len(PLATFORMS), "style": style or "classic"}
        if styled:
            # Why each effect is there (the event that triggered it) and the last quality check.
            detail.update(layout=styled["plan"]["layout"], encoder=styled["encoder"], plan=styled["plan"],
                          qc=styled["qc"])
        db.log_event(conn, "render", "clip", clip["id"], "rendered", detail)
        out["rendered"].append(clip["id"])
        conn.commit()
    return out
