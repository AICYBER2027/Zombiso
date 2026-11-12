"""Thin FFmpeg / ffprobe helpers."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise MediaError(f"{tool} not found on PATH")


def run(args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"command failed ({proc.returncode}): {' '.join(args[:6])}...\n{proc.stderr[-2000:]}")
    return proc


def run_bytes(args: list[str]) -> bytes:
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise MediaError(f"command failed ({proc.returncode}): {' '.join(args[:6])}...\n{proc.stderr[-2000:].decode(errors='replace')}")
    return proc.stdout


def probe(path: str | Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout
    data = json.loads(out)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "duration": float(data["format"].get("duration", 0.0)),
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "video_codec": video.get("codec_name") if video else None,
        "has_audio": audio is not None,
    }


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_frame(src: str | Path, t: float, out: str | Path, width: int = 640) -> Path:
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale={width}:-2", str(out)])
    return Path(out)
