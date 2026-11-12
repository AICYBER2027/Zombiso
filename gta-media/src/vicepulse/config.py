"""Settings from environment variables, plus the channel and game config files."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# The project folder (holds db/, config/). From a source checkout it is two levels above this file;
# in the Docker image the package is installed elsewhere, so the image sets VICEPULSE_HOME=/app.
PACKAGE_ROOT = Path(os.environ.get("VICEPULSE_HOME") or Path(__file__).resolve().parents[2])
PLATFORMS = ("youtube", "instagram", "tiktok")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))
    data_dir: Path = field(default_factory=lambda: Path(_env("VICEPULSE_DATA_DIR", str(PACKAGE_ROOT / "data"))))
    game: str = field(default_factory=lambda: _env("VICEPULSE_GAME", "gta5"))
    channels_file: Path = field(
        default_factory=lambda: Path(_env("VICEPULSE_CHANNELS_FILE", str(PACKAGE_ROOT / "config" / "channels.json")))
    )
    games_file: Path = field(
        default_factory=lambda: Path(_env("VICEPULSE_GAMES_FILE", str(PACKAGE_ROOT / "config" / "games.json")))
    )
    llm_mode: str = field(default_factory=lambda: _env("LLM_MODE", "mock"))
    model_main: str = field(default_factory=lambda: _env("VICEPULSE_MODEL_MAIN", "claude-opus-5-5"))
    model_triage: str = field(default_factory=lambda: _env("VICEPULSE_MODEL_TRIAGE", "claude-haiku-5-5"))
    llm_fallbacks: bool = field(default_factory=lambda: _env("VICEPULSE_LLM_FALLBACKS", "1") == "1")
    allow_live_publish: bool = field(default_factory=lambda: _env("VICEPULSE_ALLOW_LIVE_PUBLISH", "0") == "1")
    require_approval: bool = field(default_factory=lambda: _env("VICEPULSE_REQUIRE_APPROVAL", "1") != "0")
    # Treat channels as monetised unless told otherwise, so a licence must allow monetisation.
    monetised: bool = field(default_factory=lambda: _env("VICEPULSE_MONETISED", "1") != "0")
    transcriber: str = field(default_factory=lambda: _env("VICEPULSE_TRANSCRIBER", "none"))
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "base"))
    # auto: subtitles from the clip's real speech when Whisper is on (VICEPULSE_TRANSCRIBER=whisper), else the script
    captions: str = field(default_factory=lambda: _env("VICEPULSE_CAPTIONS", "auto"))
    # transcribe whole videos during signal extraction (slow for long files); captions only need the clip
    transcribe_full: bool = field(default_factory=lambda: _env("VICEPULSE_TRANSCRIBE_FULL", "0") == "1")
    voice_mode: str = field(default_factory=lambda: _env("VICEPULSE_VOICE_MODE", "own"))
    voice_dir: Path = field(default_factory=lambda: Path(_env("VICEPULSE_VOICE_DIR", str(PACKAGE_ROOT / "data" / "voice"))))
    public_media_base_url: str = field(default_factory=lambda: _env("VICEPULSE_PUBLIC_MEDIA_BASE_URL"))
    worker_token: str = field(default_factory=lambda: _env("VICEPULSE_WORKER_TOKEN"))
    worker_port: int = field(default_factory=lambda: int(_env("VICEPULSE_WORKER_PORT", "8088")))

    def channels(self) -> list[dict]:
        return json.loads(self.channels_file.read_text())["channels"]

    def channel(self, key: str) -> dict:
        for ch in self.channels():
            if ch["key"] == key:
                return ch
        raise KeyError(f"unknown channel {key!r}")

    def game_config(self, game: str | None = None) -> dict:
        games = json.loads(self.games_file.read_text())["games"]
        return games[game or self.game]

    def dir(self, *parts: str) -> Path:
        p = self.data_dir.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p


def platform_secret(platform: str, channel_key: str, what: str) -> str:
    """Read a per-channel platform secret from the environment, e.g. TIKTOK_ACCESS_TOKEN_EN."""
    return os.environ.get(f"{platform.upper()}_{what.upper()}_{channel_key.upper()}", "")
