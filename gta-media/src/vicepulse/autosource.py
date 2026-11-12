"""Hands-free clip production from footage that may legitimately be processed.

Sources:
  1. Wikimedia Commons: video files whose licence (read from the official Commons API) allows edits:
     public domain, CC0, CC BY. NonCommercial, NoDerivatives and ShareAlike licences are skipped.
     Wikimedia requires contact details in automated requests, so WIKIMEDIA_CONTACT must be set.
  2. The owner's own recordings folder (<data>/incoming/own): processed automatically once the owner has
     declared, one time, that everything in it is theirs (VICEPULSE_OWN_FOLDER_DECLARED=1).

There is no YouTube or Twitch downloader: their terms do not allow downloading other people's videos.

Rights are still recorded for every file. Licences accepted automatically are marked ``verified_by =
'auto:...'``; they are enough for private clip production, but the pre-publish gate refuses them until a
person confirms the licence (``vicepulse rights-verify``). Human approval and the live-publish switch also
still apply.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import date
from pathlib import Path

import requests

from . import commentary, db, ingest, media, policy, render, selection, signals
from .config import Settings
from .llm import LLM

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
AUTO_PREFIX = "auto:"
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm")


# ---------------------------------------------------------------- licences

def classify_licence(short_name: str | None, *, allow_share_alike: bool = False) -> dict | None:
    """Map a Commons licence name to rights-ledger fields, or None if it does not allow our use."""
    name = (short_name or "").strip()
    low = name.lower()
    if not low:
        return None
    if low in ("public domain", "pd") or low.startswith("pd-") or "public domain" in low:
        return {"licence_type": "creative_commons", "cc_licence": "PD", "label": "public domain"}
    if low.startswith("cc0") or low == "cc-zero":
        return {"licence_type": "creative_commons", "cc_licence": "CC0-1.0", "label": "CC0"}
    m = re.fullmatch(r"cc[ -]by((?:[ -](?:sa|nc|nd))*)[ -]?(\d(?:\.\d)?)?", low)
    if not m:
        return None
    parts = {p for p in re.split(r"[ -]", m.group(1)) if p}
    if "nc" in parts or "nd" in parts:
        return None                                    # no commercial use / no edits
    if "sa" in parts and not allow_share_alike:
        return None                                    # clips would have to be released under the same licence
    code = "CC-BY" + ("-SA" if "sa" in parts else "") + (f"-{m.group(2)}" if m.group(2) else "")
    return {"licence_type": "creative_commons", "cc_licence": code, "label": name}


def _strip(html: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


# ---------------------------------------------------------------- Wikimedia Commons

class Commons:
    def __init__(self, contact: str, session: requests.Session | None = None):
        if not contact:
            raise ValueError("WIKIMEDIA_CONTACT is not set")
        self.http = session or requests.Session()
        # Wikimedia's User-Agent policy: identify the tool and give a way to contact its operator.
        self.headers = {"User-Agent": f"VicePulse/0.1 (contact: {contact}) python-requests"}

    def search_videos(self, query: str, limit: int = 20) -> list[dict]:
        r = self.http.get(COMMONS_API, headers=self.headers, timeout=30, params={
            "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
            "gsrsearch": f"{query} filetype:video", "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|size|mime|sha1|extmetadata"})
        r.raise_for_status()
        out = []
        for page in (r.json().get("query", {}).get("pages") or {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            if not info.get("url") or not (info.get("mime") or "").startswith("video/"):
                continue
            out.append({"title": page.get("title"), "url": info["url"], "page_url": info.get("descriptionurl"),
                        "mime": info.get("mime"), "size": info.get("size"), "sha1": info.get("sha1"),
                        "duration": info.get("duration"),
                        "licence": _strip((meta.get("LicenseShortName") or {}).get("value")),
                        "artist": _strip((meta.get("Artist") or {}).get("value")) or "unknown author",
                        "attribution_required": (meta.get("AttributionRequired") or {}).get("value") != "false"})
        return out

    def download(self, item: dict, dest: Path, max_bytes: int) -> Path:
        if item.get("size") and item["size"] > max_bytes:
            raise ValueError(f"file is {item['size']} bytes, over the {max_bytes}-byte limit")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        sha1, total = hashlib.sha1(), 0
        with self.http.get(item["url"], headers=self.headers, timeout=120, stream=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("download exceeded the size limit")
                    sha1.update(chunk)
                    f.write(chunk)
        if item.get("sha1") and sha1.hexdigest() != item["sha1"]:
            tmp.unlink(missing_ok=True)
            raise ValueError("checksum does not match the Commons record")
        tmp.replace(dest)
        return dest


def _safe_name(title: str) -> str:
    base = re.sub(r"^File:", "", title or "file")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120]


# ---------------------------------------------------------------- rights (automatic)

def auto_rights_for_commons(conn, item: dict, lic: dict) -> int:
    row = conn.execute("SELECT id FROM rights_ledger WHERE document_url = %s AND licence_type = 'creative_commons'",
                       (item["page_url"],)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        """INSERT INTO rights_ledger (owner_name, licence_type, cc_licence, platforms, languages, starts_on,
               monetisation_allowed, derivative_edits_allowed, credit_text, document_url, verified_by, verified_at, notes)
           VALUES (%s, 'creative_commons', %s, %s, ARRAY['*'], CURRENT_DATE, TRUE, TRUE, %s, %s, %s, now(), %s)
           RETURNING id""",
        (item["artist"], lic["cc_licence"], ["youtube", "instagram", "tiktok"],
         f"Video: {item['artist']} ({item['licence']}), via Wikimedia Commons", item["page_url"],
         AUTO_PREFIX + "wikimedia-commons-licence-api",
         f"Licence read automatically from the Commons API ({item['licence']}). Confirm with "
         f"'vicepulse rights-verify' before publishing.")).fetchone()["id"]


def own_folder_rights(conn) -> int:
    row = conn.execute("""SELECT id FROM rights_ledger WHERE licence_type = 'own'
                          AND verified_by = 'owner declaration (VICEPULSE_OWN_FOLDER_DECLARED)'""").fetchone()
    if row:
        return row["id"]
    return conn.execute(
        """INSERT INTO rights_ledger (owner_name, licence_type, platforms, starts_on, monetisation_allowed,
               derivative_edits_allowed, verified_by, verified_at, notes)
           VALUES ('VicePulse (own recordings folder)', 'own', %s, CURRENT_DATE, TRUE, TRUE,
                   'owner declaration (VICEPULSE_OWN_FOLDER_DECLARED)', now(),
                   'The owner declared that every file placed in the own recordings folder is their own recording.')
           RETURNING id""", (["youtube", "instagram", "tiktok"],)).fetchone()["id"]


def rights_for_file(conn, kind: str, *, licence: str = "", author: str = "", url: str = "", creator: str = "") -> int:
    """Rights for one file the owner points at, from their single answer about where it came from.

    own: the owner's own recording. cc: a Creative Commons licence that allows commercial use and edits
    (licence name, author and licence page URL). permission: written permission from the creator to clip,
    edit and monetise (creator name and a link to the evidence). The owner's answer is the confirmation.
    """
    platforms = ["youtube", "instagram", "tiktok"]
    by = "owner (highlight command)"
    if kind == "own":
        return conn.execute(
            """INSERT INTO rights_ledger (owner_name, licence_type, platforms, starts_on, monetisation_allowed,
                   derivative_edits_allowed, verified_by, verified_at, notes)
               VALUES ('VicePulse (own recording)', 'own', %s, CURRENT_DATE, TRUE, TRUE, %s, now(),
                       'The owner stated this file is their own recording.') RETURNING id""",
            (platforms, by)).fetchone()["id"]
    if kind == "cc":
        lic = classify_licence(licence)
        if not lic:
            raise ValueError(f"licence {licence!r} cannot be used: it must allow commercial use and edits "
                             "(CC BY, CC0 or public domain; not NC, ND or SA)")
        if not (author and url):
            raise ValueError("a Creative Commons file needs --author and --url (the licence or file page)")
        return conn.execute(
            """INSERT INTO rights_ledger (owner_name, licence_type, cc_licence, platforms, languages, starts_on,
                   monetisation_allowed, derivative_edits_allowed, credit_text, document_url, verified_by, verified_at, notes)
               VALUES (%s, 'creative_commons', %s, %s, ARRAY['*'], CURRENT_DATE, TRUE, TRUE, %s, %s, %s, now(),
                       'Licence stated by the owner.') RETURNING id""",
            (author, lic["cc_licence"], platforms, f"Video: {author} ({licence})", url, by)).fetchone()["id"]
    if kind == "permission":
        if not (creator and url):
            raise ValueError("creator permission needs --creator and --url (a link to the written permission)")
        return conn.execute(
            """INSERT INTO rights_ledger (owner_name, licence_type, platforms, languages, starts_on,
                   monetisation_allowed, derivative_edits_allowed, credit_text, document_url, verified_by, verified_at, notes)
               VALUES (%s, 'creator_permission', %s, ARRAY['*'], CURRENT_DATE, TRUE, TRUE, %s, %s, %s, now(),
                       'The owner stated the creator gave written permission to clip, edit and monetise.') RETURNING id""",
            (creator, platforms, f"Video: {creator}", url, by)).fetchone()["id"]
    raise ValueError("rights must be own, cc or permission")


def highlight(conn, settings: Settings, llm: LLM, path: str | Path, *, rights: str, channel: str = "en",
              clip_seconds: float = 45, licence: str = "", author: str = "", url: str = "", creator: str = "") -> dict:
    """One highlight from one file: rights, ingest, best window, subtitles, vertical render, saved MP4."""
    path = Path(path)
    digest = media.sha256_file(path)
    known = conn.execute("SELECT id FROM sources WHERE file_sha256 = %s", (digest,)).fetchone()
    if known:
        done = conn.execute("""SELECT cl.id FROM clips cl JOIN candidates c ON c.id = cl.candidate_id
                               JOIN channels ch ON ch.id = cl.channel_id JOIN renders r ON r.clip_id = cl.id
                               WHERE c.source_id = %s AND ch.key = %s AND r.platform = 'youtube'
                               ORDER BY cl.id DESC LIMIT 1""", (known["id"], channel)).fetchone()
        if done:
            return {"source_id": known["id"], "clips": [done["id"]], "already_done": True,
                    "finished_files": _export(conn, settings, [done["id"]], _safe_name(path.stem))}
    rid = rights_for_file(conn, rights, licence=licence, author=author, url=url, creator=creator)
    conn.commit()
    res = ingest.ingest_file(conn, settings, path, origin="own_recording" if rights == "own" else "creator_upload",
                             rights_id=rid)
    if res["status"] != "cleared":
        return {"source_id": res["source_id"], "clips": [], "finished_files": [], "refused": res.get("reasons")}
    ids = _clip_and_render(conn, settings, llm, res["source_id"], channels=[channel], clip_seconds=clip_seconds,
                           max_clips=1)
    clips = conn.execute("SELECT id, status, status_reason, in_s, out_s, script FROM clips WHERE id = ANY(%s)",
                         (ids,)).fetchall()
    return {"source_id": res["source_id"], "clips": ids, "already_done": False,
            "details": [{"clip": c["id"], "status": c["status"], "reason": c["status_reason"],
                         "from_s": c["in_s"], "to_s": c["out_s"],
                         "subtitles": (c["script"] or {}).get("caption_source"),
                         "subtitle_lines": len((c["script"] or {}).get("captions", []))} for c in clips],
            "finished_files": _export(conn, settings, ids, _safe_name(path.stem))}


# ---------------------------------------------------------------- the automatic run

def _clip_and_render(conn, settings: Settings, llm: LLM, source_id: int, *, channels: list[str], clip_seconds: float,
                     max_clips: int) -> list[int]:
    duration = conn.execute("SELECT duration_s FROM sources WHERE id = %s", (source_id,)).fetchone()["duration_s"] or 0
    signals.extract(conn, settings, source_id, window_s=min(clip_seconds, max(duration, 1)))
    selection.triage_new(conn, settings, llm)
    clip_ids = []
    for ch in channels:
        clip_ids += selection.select_for_channel(conn, settings, llm, ch, date.today(), max_clips=max_clips,
                                                 source_id=source_id)["selected"]
    if clip_ids:
        commentary.write_scripts(conn, settings, llm, clip_ids=clip_ids)
        render.render_pending(conn, settings, clip_ids=clip_ids)
        policy.final_check(conn, settings, llm, clip_ids=clip_ids)
    return clip_ids


def _export(conn, settings: Settings, clip_ids: list[int], label: str) -> list[str]:
    """Copy each clip's YouTube render to <data>/finished/<date>/ with a readable name."""
    out = []
    folder = settings.dir("finished", date.today().isoformat())
    for cid in clip_ids:
        r = conn.execute("""SELECT r.file_path, ch.key FROM renders r JOIN clips cl ON cl.id = r.clip_id
                            JOIN channels ch ON ch.id = cl.channel_id
                            WHERE r.clip_id = %s AND r.platform = 'youtube'""", (cid,)).fetchone()
        if r:
            dest = folder / f"{label}_clip{cid}_{r['key']}.mp4"
            shutil.copyfile(r["file_path"], dest)
            out.append(str(dest))
    return out


def run(conn, settings: Settings, llm: LLM, cfg: dict, *, commons: Commons | None = None, sources: str = "all",
        channels: list[str] | None = None, clip_seconds: float = 30, max_clips: int = 1) -> dict:
    a = cfg.get("autosource", {})
    channels = channels or a.get("channels", ["en"])
    out = {"processed": [], "skipped": [], "finished_files": [], "notes": []}
    done = 0
    limit = a.get("max_files_per_run", 2)

    if sources in ("all", "own"):
        folder = settings.dir("incoming", "own")
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS)
        if files and os.environ.get("VICEPULSE_OWN_FOLDER_DECLARED") != "1":
            out["notes"].append(f"{len(files)} file(s) in {folder} not processed: set VICEPULSE_OWN_FOLDER_DECLARED=1 "
                                "to declare once that everything in that folder is your own recording")
        elif files:
            rid = own_folder_rights(conn)
            conn.commit()
            for p in files:
                if done >= limit:
                    break
                res = ingest.ingest_file(conn, settings, p, origin="own_recording", rights_id=rid)
                if res["duplicate_file"] or res["status"] != "cleared":
                    continue                               # already processed (or refused by the rights gate)
                ids = _clip_and_render(conn, settings, llm, res["source_id"], channels=channels,
                                       clip_seconds=clip_seconds, max_clips=max_clips)
                out["processed"].append({"source": "own folder", "file": p.name, "clips": ids})
                out["finished_files"] += _export(conn, settings, ids, _safe_name(p.stem))
                done += 1

    if sources in ("all", "commons"):
        if commons is None:
            contact = os.environ.get("WIKIMEDIA_CONTACT", "")
            if not contact:
                out["notes"].append("Wikimedia Commons skipped: set WIKIMEDIA_CONTACT (an email address or URL where "
                                    "you can be reached). Wikimedia requires it in automated requests.")
            else:
                commons = Commons(contact)
        if commons:
            max_bytes = int(a.get("max_file_mb", 200)) * 1024 * 1024
            max_dur = a.get("max_duration_s", 900)
            seen = set()
            for q in a.get("commons_queries", ["video game gameplay"]):
                if done >= limit:
                    break
                try:
                    items = commons.search_videos(q)
                except requests.RequestException as e:
                    out["notes"].append(f"Commons search '{q}' failed: {e}")
                    continue
                for item in items:
                    if done >= limit or item["title"] in seen:
                        continue
                    seen.add(item["title"])
                    if conn.execute("SELECT 1 FROM rights_ledger WHERE document_url = %s", (item["page_url"],)).fetchone():
                        continue                           # processed on an earlier run
                    lic = classify_licence(item["licence"], allow_share_alike=a.get("allow_share_alike", False))
                    if lic is None:
                        out["skipped"].append({"title": item["title"], "reason": f"licence not usable: {item['licence']}"})
                        continue
                    if item.get("duration") and item["duration"] > max_dur:
                        out["skipped"].append({"title": item["title"], "reason": f"longer than {max_dur} s"})
                        continue
                    dest = settings.dir("incoming", "commons") / _safe_name(item["title"])
                    try:
                        commons.download(item, dest, max_bytes)
                    except (requests.RequestException, ValueError) as e:
                        out["skipped"].append({"title": item["title"], "reason": f"download failed: {e}"})
                        continue
                    rid = auto_rights_for_commons(conn, item, lic)
                    db.log_event(conn, "autosource", "rights_ledger", rid, "auto_licence",
                                 {"title": item["title"], "licence": item["licence"], "artist": item["artist"]})
                    conn.commit()
                    res = ingest.ingest_file(conn, settings, dest, origin="creator_upload", rights_id=rid)
                    if res["status"] != "cleared":
                        out["skipped"].append({"title": item["title"], "reason": f"rights gate: {res['reasons']}"})
                        continue
                    ids = _clip_and_render(conn, settings, llm, res["source_id"], channels=channels,
                                           clip_seconds=clip_seconds, max_clips=max_clips)
                    out["processed"].append({"source": "Wikimedia Commons", "file": item["title"], "licence": item["licence"],
                                             "author": item["artist"], "clips": ids})
                    out["finished_files"] += _export(conn, settings, ids, _safe_name(Path(item["title"]).stem))
                    done += 1
    return out
