"""Postgres access: connection, migrations, channel seeding and the audit event log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import PACKAGE_ROOT, PLATFORMS, Settings

MIGRATIONS_DIR = PACKAGE_ROOT / "db" / "migrations"


def connect(settings: Settings) -> psycopg.Connection:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=False)


def migrate(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    files = sorted(Path(migrations_dir).glob("*.sql"))
    if not files:
        raise RuntimeError(f"no migration files found in {migrations_dir} (set VICEPULSE_HOME to the project folder)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    applied = []
    for path in files:
        if path.name in done:
            continue
        conn.execute(path.read_text())
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    conn.commit()
    return applied


def seed_channels(conn: psycopg.Connection, settings: Settings) -> list[dict]:
    """Create or update the channel rows (VicePulse EN / BR) and their platform account rows."""
    out = []
    for ch in settings.channels():
        row = conn.execute(
            """INSERT INTO channels (key, name, language, clips_per_day, timezone)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, language = EXCLUDED.language,
                   clips_per_day = EXCLUDED.clips_per_day, timezone = EXCLUDED.timezone
               RETURNING *""",
            (ch["key"], ch["name"], ch["language"], ch["clips_per_day"], ch["timezone"]),
        ).fetchone()
        for platform in ch["platforms"]:
            assert platform in PLATFORMS
            conn.execute(
                """INSERT INTO platform_accounts (channel_id, platform) VALUES (%s, %s)
                   ON CONFLICT (channel_id, platform) DO NOTHING""",
                (row["id"], platform),
            )
        out.append(row)
    conn.commit()
    return out


def log_event(conn: psycopg.Connection, stage: str, entity: str, entity_id: int | None, outcome: str,
              detail: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO events (stage, entity, entity_id, outcome, detail) VALUES (%s, %s, %s, %s, %s)",
        (stage, entity, entity_id, outcome, Jsonb(detail or {})),
    )


__all__ = ["connect", "migrate", "seed_channels", "log_event", "Jsonb"]
