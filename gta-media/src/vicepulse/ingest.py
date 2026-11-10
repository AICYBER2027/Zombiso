"""Ingest: register a new footage file and run rights gate 1.

Only two origins exist: the owner's own recordings and uploads from creators who signed
a licence. There is deliberately no "download from URL" path.
"""

from __future__ import annotations

from pathlib import Path

from . import db, media, rights
from .config import Settings


def add_rights(conn, **fields) -> int:
    cols = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    row = conn.execute(f"INSERT INTO rights_ledger ({cols}) VALUES ({marks}) RETURNING id", tuple(fields.values())).fetchone()
    db.log_event(conn, "rights", "rights_ledger", row["id"], "added", {"licence_type": fields.get("licence_type")})
    conn.commit()
    return row["id"]


def ingest_file(conn, settings: Settings, path: str | Path, *, origin: str, rights_id: int | None,
                game: str | None = None) -> dict:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if origin not in ("own_recording", "creator_upload"):
        raise ValueError("origin must be own_recording or creator_upload")
    digest = media.sha256_file(path)
    existing = conn.execute("SELECT * FROM sources WHERE file_sha256 = %s", (digest,)).fetchone()
    if existing:
        return {"source_id": existing["id"], "status": existing["status"], "duplicate_file": True,
                "reasons": ["identical file already ingested"]}
    info = media.probe(path)
    src = conn.execute(
        """INSERT INTO sources (game, origin, rights_id, file_path, file_sha256, duration_s, width, height)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (game or settings.game, origin, rights_id, str(path), digest, info["duration"], info["width"], info["height"]),
    ).fetchone()
    decision = rights.ingest_gate(conn, src["id"])
    status = "cleared" if decision.ok else "quarantined"
    conn.execute("UPDATE sources SET status = %s, status_reason = %s WHERE id = %s",
                 (status, "; ".join(decision.reasons) or None, src["id"]))
    db.log_event(conn, "ingest", "source", src["id"], status, decision.as_dict())
    conn.commit()
    return {"source_id": src["id"], "status": status, "duplicate_file": False, "reasons": decision.reasons}


def recheck_quarantine(conn) -> list[dict]:
    """Re-run gate 1 on quarantined sources (e.g. after a licence was verified)."""
    out = []
    for src in conn.execute("SELECT id FROM sources WHERE status = 'quarantined'").fetchall():
        decision = rights.ingest_gate(conn, src["id"])
        if decision.ok:
            conn.execute("UPDATE sources SET status = 'cleared', status_reason = NULL WHERE id = %s", (src["id"],))
            db.log_event(conn, "ingest", "source", src["id"], "cleared_on_recheck")
        out.append({"source_id": src["id"], **decision.as_dict()})
    conn.commit()
    return out
