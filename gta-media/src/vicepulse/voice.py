"""Pluggable commentary voice.

- ``own``: the owner records each script; the file is <VOICE_DIR>/<clip_id>.wav (or .m4a/.mp3).
  If the recording is not there yet, the clip renders with captions only.
- ``none``: captions only.
- ``synthetic``: placeholder for a licensed synthetic voice later. Not wired to any vendor;
  using it must also set the platforms' AI-content disclosure (handled in the publishers).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .config import Settings

AUDIO_EXTS = (".wav", ".m4a", ".mp3", ".flac")


class VoiceProvider(Protocol):
    synthetic: bool

    def voice_for(self, clip: dict) -> Path | None: ...


class NoVoice:
    synthetic = False

    def voice_for(self, clip: dict) -> Path | None:
        return None


class OwnRecording:
    synthetic = False

    def __init__(self, voice_dir: Path):
        self.voice_dir = Path(voice_dir)

    def voice_for(self, clip: dict) -> Path | None:
        for ext in AUDIO_EXTS:
            p = self.voice_dir / f"{clip['id']}{ext}"
            if p.is_file():
                return p
        return None


class SyntheticVoice:  # pragma: no cover - deliberately not implemented
    synthetic = True

    def voice_for(self, clip: dict) -> Path | None:
        raise NotImplementedError("No synthetic voice vendor is chosen or paid for. Use VICEPULSE_VOICE_MODE=own or none.")


def make_voice(settings: Settings) -> VoiceProvider:
    if settings.voice_mode == "own":
        return OwnRecording(settings.voice_dir)
    if settings.voice_mode == "none":
        return NoVoice()
    if settings.voice_mode == "synthetic":
        return SyntheticVoice()
    raise ValueError(f"unknown VICEPULSE_VOICE_MODE {settings.voice_mode!r}")
