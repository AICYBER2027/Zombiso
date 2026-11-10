"""Duplicate prevention.

Layers (PLAN.md section 3.6):
1. source time-range overlap        -> the same moment cut twice
2. perceptual hash of keyframes     -> the same visuals from a different file or creator
3. text similarity of scripts/titles -> repeating the same joke or angle
4. UNIQUE (clip_id, platform) in publish_jobs -> the same clip posting twice after a retry (enforced by Postgres)

Audio fingerprinting (also listed in the plan) is not built yet; see README "Not built yet".
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import media

OVERLAP_THRESHOLD = 0.3     # share of the shorter range
HASH_DISTANCE = 10          # max Hamming distance (of 64 bits) for "same frame"
HASH_MATCH_SHARE = 0.5      # share of keyframes that must match
TEXT_SIMILARITY = 0.8
LOOKBACK_DAYS = 60


@dataclass
class DupDecision:
    duplicate: bool
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- pure helpers

def overlap_share(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = min(a[1] - a[0], b[1] - b[0])
    return inter / shorter if shorter > 0 else 0.0


def dhash(path: str | Path, size: int = 8) -> str:
    img = Image.open(path).convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(img.get_flattened_data())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hashes_match(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return False
    matched = sum(1 for h in a if any(hamming(h, o) <= HASH_DISTANCE for o in b))
    return matched / len(a) >= HASH_MATCH_SHARE


def _norm(text: str) -> str:
    return re.sub(r"[^\w ]+", "", (text or "").lower()).strip()


def text_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def keyframe_hashes(src_path: str, start: float, end: float, out_dir: Path, tag: str, n: int = 4) -> tuple[list[str], list[Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames, hashes = [], []
    for k in range(n):
        t = start + (end - start) * (k + 0.5) / n
        f = media.extract_frame(src_path, t, out_dir / f"{tag}_{k}.jpg")
        frames.append(f)
        hashes.append(dhash(f))
    return hashes, frames


# ---------------------------------------------------------------- database checks

_LIVE = "('duplicate', 'rejected', 'blocked', 'taken_down')"


def check_footage(conn, *, channel_id: int, candidate_id: int, in_s: float, out_s: float) -> DupDecision:
    """Layers 1 and 2, run before a clip is created for a channel."""
    cand = conn.execute(
        "SELECT c.*, s.id AS sid FROM candidates c JOIN sources s ON s.id = c.source_id WHERE c.id = %s",
        (candidate_id,)).fetchone()
    reasons = []
    rows = conn.execute(
        f"""SELECT cl.id, cl.in_s, cl.out_s, c.source_id, c.keyframe_hashes
            FROM clips cl JOIN candidates c ON c.id = cl.candidate_id
            WHERE cl.channel_id = %s AND cl.status NOT IN {_LIVE} AND cl.candidate_id <> %s""",
        (channel_id, candidate_id)).fetchall()
    for r in rows:
        if r["source_id"] == cand["source_id"] and overlap_share((in_s, out_s), (r["in_s"], r["out_s"])) >= OVERLAP_THRESHOLD:
            reasons.append(f"overlaps clip {r['id']} from the same source")
        elif hashes_match(cand["keyframe_hashes"] or [], r["keyframe_hashes"] or []):
            reasons.append(f"same visuals as clip {r['id']} (perceptual hash)")
    return DupDecision(bool(reasons), reasons)


def check_script(conn, *, channel_id: int, clip_id: int, script_text: str, angle: str) -> DupDecision:
    """Layer 3, run after commentary is written."""
    rows = conn.execute(
        f"""SELECT id, script_text, script->>'angle' AS angle FROM clips
            WHERE channel_id = %s AND id <> %s AND script_text IS NOT NULL AND status NOT IN {_LIVE}
              AND created_at > now() - interval '{LOOKBACK_DAYS} days'""",
        (channel_id, clip_id)).fetchall()
    reasons = []
    for r in rows:
        sim = text_similarity(script_text, r["script_text"])
        if sim >= TEXT_SIMILARITY:
            reasons.append(f"script {sim:.0%} similar to clip {r['id']}")
        elif angle and r["angle"] and _norm(angle) == _norm(r["angle"]):
            reasons.append(f"same angle as clip {r['id']}: {angle}")
    return DupDecision(bool(reasons), reasons)
