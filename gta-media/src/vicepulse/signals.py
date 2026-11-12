"""Signal extraction: cheap, AI-free signals that turn a video into candidate windows.

- scene changes (FFmpeg scene score)
- loudness peaks (EBU R128 momentary loudness)
- transcript with timestamps (pluggable; Whisper is optional and runs locally)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import db, media, rights
from .config import Settings

_PTS = re.compile(r"pts_time:([0-9.]+)")
_EBU = re.compile(r"t:\s*([0-9.]+)\s+TARGET:.*?M:\s*(-?[0-9.]+|-inf)")


def scene_changes(path: str | Path, threshold: float = 0.3) -> list[float]:
    proc = media.run(["ffmpeg", "-hide_banner", "-i", str(path), "-an",
                      "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    return sorted({round(float(m), 3) for m in _PTS.findall(proc.stderr)})


def loudness_curve(path: str | Path) -> list[tuple[float, float]]:
    """Momentary loudness (LUFS) roughly every 100 ms. Empty if there is no audio."""
    if not media.probe(path)["has_audio"]:
        return []
    proc = media.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                      "-af", "ebur128", "-f", "null", "-"])
    out = []
    for t, m in _EBU.findall(proc.stderr):
        out.append((float(t), -120.0 if m == "-inf" else float(m)))
    return out


def loudness_peaks(curve: list[tuple[float, float]], *, min_gap_s: float = 4.0, top: int = 20,
                   above_median_db: float = 6.0) -> list[tuple[float, float]]:
    if not curve:
        return []
    values = sorted(v for _, v in curve)
    median = values[len(values) // 2]
    loud = sorted((p for p in curve if p[1] >= median + above_median_db), key=lambda p: -p[1])
    picked: list[tuple[float, float]] = []
    for t, v in loud:
        if all(abs(t - pt) >= min_gap_s for pt, _ in picked):
            picked.append((t, v))
        if len(picked) >= top:
            break
    return sorted(picked)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)       # [{"start": s, "end": s, "text": "word"}]


class Transcriber(Protocol):
    def transcribe(self, path: str | Path, start: float | None = None, duration: float | None = None) -> list[Segment]: ...


class NullTranscriber:
    """No transcription. Selection still works from scenes, loudness and frames."""

    def transcribe(self, path: str | Path, start: float | None = None, duration: float | None = None) -> list[Segment]:
        return []


def decode_audio_16k(path: str | Path, start: float | None = None, duration: float | None = None):
    """Mono 16 kHz float32 samples via FFmpeg (avoids depending on PyAV versions)."""
    import numpy as np

    args = ["ffmpeg", "-nostdin", "-v", "error"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(path)]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    pcm = media.run_bytes(args)
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0


class WhisperTranscriber:
    """Local Whisper via faster-whisper (optional: pip install .[whisper]). Free; runs on the CPU.

    Times returned are relative to ``start`` when a range is given (i.e. relative to the clip).
    """

    def __init__(self, model: str = "base", cache_dir: str | None = None):
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:  # pragma: no cover - optional
            raise RuntimeError("faster-whisper is not installed; rebuild the worker with VICEPULSE_BUILD_WHISPER=1 "
                               "or set VICEPULSE_TRANSCRIBER=none") from e
        self._model = WhisperModel(model, device="cpu", compute_type="int8", download_root=cache_dir)
        self.language: str | None = None

    def transcribe(self, path: str | Path, start: float | None = None, duration: float | None = None) -> list[Segment]:
        audio = decode_audio_16k(path, start, duration)
        if audio.size < 1600:                       # under 0.1 s of audio, or no audio track
            return []
        segments, info = self._model.transcribe(audio, word_timestamps=True, vad_filter=True)
        self.language = info.language
        out = []
        for seg in segments:
            words = [{"start": float(w.start), "end": float(w.end), "text": w.word.strip()}
                     for w in (seg.words or []) if w.word.strip()]
            out.append(Segment(float(seg.start), float(seg.end), seg.text.strip(), words))
        return out


def make_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber == "whisper":
        return WhisperTranscriber(settings.whisper_model, cache_dir=os.environ.get("WHISPER_CACHE_DIR"))
    return NullTranscriber()


def candidate_windows(duration: float, cuts: list[float], peaks: list[tuple[float, float]], *,
                      length_s: float = 20.0, lead_s: float = 3.0, min_len_s: float = 6.0) -> list[dict]:
    """Build candidate windows around loudness peaks and scene cuts, scored by signal density."""
    length_s = min(length_s, duration)
    anchors = [(t, 1.0 + max(0.0, (v + 30.0) / 10.0)) for t, v in peaks] + [(t, 0.5) for t in cuts]
    if not anchors:
        anchors = [(min(lead_s, duration / 2), 0.1)]
    windows: list[dict] = []
    for t, _ in sorted(anchors):
        start = max(0.0, t - lead_s)
        end = min(duration, start + length_s)
        start = max(0.0, end - length_s)
        if end - start < min_len_s:
            continue
        if any(abs(w["start_s"] - start) < 1.0 for w in windows):
            continue
        score = sum(w for at, w in anchors if start <= at <= end)
        windows.append({"start_s": round(start, 3), "end_s": round(end, 3), "signal_score": round(score, 3)})
    return windows


def extract(conn, settings: Settings, source_id: int, transcriber: Transcriber | None = None, *,
            window_s: float = 20.0) -> dict:
    src = rights.require_cleared_source(conn, source_id)
    path = src["file_path"]
    cuts = scene_changes(path)
    curve = loudness_curve(path)
    peaks = loudness_peaks(curve)
    if transcriber is None:
        transcriber = make_transcriber(settings) if settings.transcribe_full else NullTranscriber()
    segments = transcriber.transcribe(path)
    windows = candidate_windows(src["duration_s"], cuts, peaks, length_s=window_s)
    for w in windows:
        text = " ".join(s.text for s in segments if s.end > w["start_s"] and s.start < w["end_s"])
        conn.execute(
            "INSERT INTO candidates (source_id, start_s, end_s, signal_score, transcript) VALUES (%s, %s, %s, %s, %s)",
            (source_id, w["start_s"], w["end_s"], w["signal_score"], text or None),
        )
    signals = {"scene_cuts": cuts, "loudness_peaks": peaks,
               "transcript": [{"start": s.start, "end": s.end, "text": s.text} for s in segments], "windows": len(windows)}
    conn.execute("UPDATE sources SET status = 'analysed', signals = %s WHERE id = %s", (db.Jsonb(signals), source_id))
    db.log_event(conn, "signals", "source", source_id, "analysed", {"windows": len(windows)})
    conn.commit()
    return {"source_id": source_id, "scene_cuts": len(cuts), "peaks": len(peaks), "candidates": len(windows)}
