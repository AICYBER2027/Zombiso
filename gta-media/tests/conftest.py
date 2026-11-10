"""Test fixtures: a throwaway local Postgres, synthetic FFmpeg footage, offline settings.

Nothing here touches the network. Footage is generated with FFmpeg test sources.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest

from vicepulse import db
from vicepulse.config import PACKAGE_ROOT, Settings

# Tests must never reach a real service, whatever is in the developer's shell.
for _var in ("ANTHROPIC_API_KEY", "VICEPULSE_ALLOW_LIVE_PUBLISH", "LLM_MODE", "DATABASE_URL"):
    os.environ.pop(_var, None)
os.environ["VICEPULSE_X264_PRESET"] = "ultrafast"


def _pg_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    hits = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"))
    return hits[-1] if hits else None


@pytest.fixture(scope="session")
def database_url():
    """TEST_DATABASE_URL if given (an empty database you don't mind wiping), else a temporary cluster."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    initdb, pg_ctl = _pg_bin("initdb"), _pg_bin("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("PostgreSQL is not installed and TEST_DATABASE_URL is not set")
    root = Path(tempfile.mkdtemp(prefix="vicepulse_pg_"))
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    prefix = ["runuser", "-u", "postgres", "--"] if as_root else []
    if as_root:
        os.chmod(root, 0o755)
        shutil.chown(root, "postgres", "postgres")
    data = root / "data"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(prefix + [initdb, "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8"],
                   check=True, capture_output=True, cwd=root)
    subprocess.run(prefix + [pg_ctl, "-D", str(data), "-w", "-l", str(root / "log"), "-o",
                             f"-p {port} -k {root} -c listen_addresses=''", "start"],
                   check=True, capture_output=True, cwd=root)
    try:
        yield f"postgresql://postgres@/postgres?host={root}&port={port}"
    finally:
        subprocess.run(prefix + [pg_ctl, "-D", str(data), "-m", "immediate", "stop"], capture_output=True, cwd=root)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def settings(database_url, tmp_path):
    channels = json.loads((PACKAGE_ROOT / "config" / "channels.json").read_text())
    for ch in channels["channels"]:
        ch["clips_per_day"] = 2            # keep renders quick; production value is 5
    cfile = tmp_path / "channels.json"
    cfile.write_text(json.dumps(channels))
    return Settings(database_url=database_url, data_dir=tmp_path / "data", game="gta5", channels_file=cfile,
                    llm_mode="mock", allow_live_publish=False, require_approval=True, transcriber="none",
                    voice_mode="own", voice_dir=tmp_path / "voice", worker_token="test-token",
                    public_media_base_url="https://media.example.invalid/renders")


@pytest.fixture()
def conn(settings):
    c = db.connect(settings)
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.migrate(c)
    db.seed_channels(c, settings)
    yield c
    c.rollback()
    c.close()


PATTERNS = ["testsrc2", "smptebars", "mandelbrot", "rgbtestsrc", "pal75bars", "testsrc"]


def make_footage(path: Path, *, seg_s: int = 8, patterns: list[str] | None = None, size: str = "640x360",
                 crf: int = 23) -> Path:
    """Synthetic 16:9 'gameplay': a new test pattern every seg_s seconds, with a loud burst mid-segment."""
    patterns = patterns or PATTERNS
    total = seg_s * len(patterns)
    args = ["ffmpeg", "-v", "error", "-y"]
    for p in patterns:
        args += ["-f", "lavfi", "-t", str(seg_s), "-i", f"{p}=size={size}:rate=30"]
    args += ["-f", "lavfi", "-t", str(total), "-i", "sine=frequency=440:sample_rate=48000"]
    bursts = "+".join(f"between(t,{i * seg_s + seg_s / 2 - 1},{i * seg_s + seg_s / 2 + 1})" for i in range(len(patterns)))
    vids = "".join(f"[{i}:v]" for i in range(len(patterns)))
    graph = (f"{vids}concat=n={len(patterns)}:v=1:a=0,format=yuv420p[v];"
             f"[{len(patterns)}:a]volume='if({bursts},1,0.02)':eval=frame[a]")
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
             "-crf", str(crf), "-c:a", "aac", str(path)]
    subprocess.run(args, check=True, capture_output=True)
    return path


@pytest.fixture(scope="session")
def footage(tmp_path_factory):
    return make_footage(tmp_path_factory.mktemp("footage") / "own_session.mp4")


@pytest.fixture(scope="session")
def other_footage(tmp_path_factory):
    """Different content (different file hash and visuals), used as the unlicensed upload."""
    return make_footage(tmp_path_factory.mktemp("footage") / "someone_elses.mp4",
                        patterns=["yuvtestsrc", "smptehdbars", "testsrc"])


OWN_RIGHTS = dict(owner_name="VicePulse (own recording)", licence_type="own",
                  platforms=["youtube", "instagram", "tiktok"], starts_on="2026-01-01",
                  monetisation_allowed=True, derivative_edits_allowed=True)


def verified_rights(conn, **overrides) -> int:
    from vicepulse import ingest

    rid = ingest.add_rights(conn, **{**OWN_RIGHTS, **overrides})
    conn.execute("UPDATE rights_ledger SET verified_by = 'test reviewer', verified_at = now() WHERE id = %s", (rid,))
    conn.commit()
    return rid


__all__ = ["make_footage", "verified_rights", "OWN_RIGHTS", "psycopg"]
