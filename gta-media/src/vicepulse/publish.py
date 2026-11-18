"""Scheduler, publisher, analytics pull, takedown and health check.

Publishing is a dry run unless VICEPULSE_ALLOW_LIVE_PUBLISH=1 *and* the platform account row
is enabled. The UNIQUE (clip_id, platform) key on publish_jobs makes retries safe.

Retries: only errors the platform marks as temporary (rate limit, quota, server error, a connection that never
opened) are tried again, later each time (15 min, 30 min, ...; quota: 6 h). Anything where the post may already
have gone out (a dropped connection mid-upload, an unknown error) is never retried automatically.
Duplicates: the same file is never posted twice to the same account, even from a different clip row.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import psycopg
import requests

from . import db, rights
from .config import PUBLISH_PLATFORMS, RENDER_FOR, Settings
from .platforms import PlatformError, Publisher, PublishRequest, make_publisher
from .policy import TIKTOK_REQUIRED_OPTIONS

MAX_ATTEMPTS = 4
RETRY_BASE_S = 15 * 60
QUOTA_WAIT_S = 6 * 3600


def retry_delay(attempt: int, err: PlatformError) -> float:
    """Seconds to wait before the next try (attempt = tries made so far, 1-based)."""
    if err.retry_after:
        return max(60.0, float(err.retry_after))
    if err.kind == "quota":
        return QUOTA_WAIT_S
    return RETRY_BASE_S * (2 ** max(0, attempt - 1))
PublisherFactory = Callable[..., Publisher]


def slot_times(channel_cfg: dict, for_date: date) -> list[datetime]:
    tz = ZoneInfo(channel_cfg["timezone"])
    return [datetime.combine(for_date, time.fromisoformat(s), tzinfo=tz) for s in channel_cfg["default_slots"]]


def schedule_approved(conn, settings: Settings) -> dict:
    """Give each approved clip the next free slot of its day and create one job per platform."""
    out = {"scheduled_jobs": 0, "clips": [], "skipped": []}
    clips = conn.execute(
        """SELECT cl.*, ch.key AS channel_key FROM clips cl JOIN channels ch ON ch.id = cl.channel_id
           WHERE cl.status = 'approved' ORDER BY cl.for_date, cl.id""").fetchall()
    for clip in clips:
        cfg = settings.channel(clip["channel_key"])
        slots = slot_times(cfg, clip["for_date"])
        used = conn.execute(
            """SELECT count(DISTINCT p.clip_id) AS n FROM publish_jobs p JOIN clips c ON c.id = p.clip_id
               WHERE c.channel_id = %s AND c.for_date = %s AND p.status <> 'cancelled'""",
            (clip["channel_id"], clip["for_date"])).fetchone()["n"]
        if used >= len(slots):
            out["skipped"].append({"clip_id": clip["id"], "reason": "no free slot that day"})
            continue
        when = slots[used]
        renders = {r["platform"]: r for r in conn.execute("SELECT * FROM renders WHERE clip_id = %s",
                                                          (clip["id"],)).fetchall()}
        accounts = conn.execute("SELECT * FROM platform_accounts WHERE channel_id = %s ORDER BY id",
                                (clip["channel_id"],)).fetchall()
        for acct in accounts:
            if acct["platform"] not in PUBLISH_PLATFORMS:
                continue
            # Facebook Reels reuse the Instagram render.
            r = renders.get(acct["platform"]) or renders.get(RENDER_FOR.get(acct["platform"], ""))
            if r is None:
                continue
            options = {}
            if acct["platform"] == "tiktok":
                options = (clip["check_result"] or {}).get("tiktok_options") or {}
            conn.execute(
                """INSERT INTO publish_jobs (clip_id, platform, account_id, render_id, scheduled_for, post_options)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (clip_id, platform) DO NOTHING""",
                (clip["id"], acct["platform"], acct["id"], r["id"], when, db.Jsonb(options)))
            out["scheduled_jobs"] += 1
        conn.execute("UPDATE clips SET status = 'scheduled' WHERE id = %s", (clip["id"],))
        db.log_event(conn, "schedule", "clip", clip["id"], "scheduled", {"at": when.isoformat()})
        out["clips"].append(clip["id"])
    conn.commit()
    return out


def _request_for(settings: Settings, job: dict) -> PublishRequest:
    copy = job["titles"][job["platform"]]
    path = Path(job["file_path"])
    public_url = None
    if settings.public_media_base_url:
        try:
            rel = path.resolve().relative_to((settings.data_dir / "renders").resolve())
            public_url = f"{settings.public_media_base_url.rstrip('/')}/{rel.as_posix()}"
        except ValueError:
            public_url = None
    return PublishRequest(file_path=path, title=copy["title"], description=copy["description"],
                          hashtags=copy["hashtags"], language=job["language"], public_url=public_url,
                          synthetic_media=settings.voice_mode == "synthetic",
                          options={**(job["post_options"] or {}),
                                   **({"duration_s": job["duration_s"]} if job.get("duration_s") else {})})


def run_due(conn, settings: Settings, *, now: datetime | None = None,
            publisher_factory: PublisherFactory = make_publisher) -> dict:
    """Post every job that is due. Safe to run every 15 minutes and safe to re-run."""
    now = now or datetime.now(timezone.utc)
    out = {"published": [], "dry_run": [], "blocked": [], "failed": [], "retry": []}
    jobs = conn.execute(
        """SELECT p.*, cl.titles, cl.language, cl.status AS clip_status, ch.key AS channel_key,
                  r.file_path, r.file_sha256, r.duration_s, a.enabled, a.audited
           FROM publish_jobs p JOIN clips cl ON cl.id = p.clip_id JOIN channels ch ON ch.id = cl.channel_id
           JOIN renders r ON r.id = p.render_id JOIN platform_accounts a ON a.id = p.account_id
           WHERE p.status = 'scheduled' AND p.scheduled_for <= %s
           ORDER BY p.scheduled_for, p.id FOR UPDATE OF p SKIP LOCKED""", (now,)).fetchall()
    for job in jobs:
        jid = job["id"]

        def finish(status: str, error: str | None = None, kind: str | None = None, **cols) -> None:
            sets = "".join(f", {k} = %s" for k in cols)
            entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": status}
            if error:
                entry.update(error=error[:500], kind=kind)
            conn.execute("UPDATE publish_jobs SET status = %s, last_error = %s, error_kind = %s, "
                         "attempt_log = attempt_log || %s::jsonb" + sets + " WHERE id = %s",
                         (status, error, kind, db.Jsonb([entry]), *cols.values(), jid))
            db.log_event(conn, "publish", "publish_job", jid, status,
                         {"error": error, "kind": kind, "platform": job["platform"]})
            conn.commit()

        # Gate 2: rights can lapse between render and post.
        decision = rights.prepublish_gate(conn, job["clip_id"], job["platform"], on=now.date(),
                                          monetised=settings.monetised)
        if not decision.ok:
            finish("blocked", "rights: " + "; ".join(decision.reasons), "permission")
            out["blocked"].append(jid)
            continue
        if job["clip_status"] not in ("scheduled", "published"):
            finish("blocked", f"clip status is {job['clip_status']}", "rejected")
            out["blocked"].append(jid)
            continue
        if job["platform"] == "tiktok":
            missing = [k for k in TIKTOK_REQUIRED_OPTIONS if k not in (job["post_options"] or {})]
            if missing:
                finish("blocked", "TikTok per-post choices missing from approval: " + ", ".join(missing), "config")
                out["blocked"].append(jid)
                continue
        # Never post the same file twice to one account (a re-cut clip row, a manual re-queue, ...).
        if job["file_sha256"]:
            twin = conn.execute(
                """SELECT id, status FROM publish_jobs WHERE account_id = %s AND content_sha256 = %s
                   AND id <> %s AND status IN ('published', 'publishing') LIMIT 1""",
                (job["account_id"], job["file_sha256"], jid)).fetchone()
            if twin:
                finish("blocked", f"duplicate: this file was already posted to this account by job {twin['id']}"
                       f" ({twin['status']})", "duplicate")
                out["blocked"].append(jid)
                continue
        req = _request_for(settings, job)
        if not (settings.allow_live_publish and job["enabled"]):
            why = "live publishing is switched off" if not settings.allow_live_publish else "platform account not enabled"
            finish("dry_run", f"dry run: {why}; nothing was sent")
            out["dry_run"].append(jid)
            continue
        # Claim the job before any network call so a crash cannot lead to a double post.
        conn.execute("UPDATE publish_jobs SET status = 'publishing', attempts = attempts + 1, content_sha256 = %s "
                     "WHERE id = %s", (job["file_sha256"], jid))
        conn.commit()
        attempt = job["attempts"] + 1
        try:
            publisher = publisher_factory(job["platform"], job["channel_key"], audited=job["audited"])
            result = publisher.publish(req)
        except PlatformError as e:
            if e.retryable and attempt < MAX_ATTEMPTS:
                wait = retry_delay(attempt, e)
                finish("scheduled", f"try {attempt} of {MAX_ATTEMPTS}: {e}", e.kind,
                       scheduled_for=now + timedelta(seconds=wait))
                out["retry"].append(jid)
            else:
                finish("failed", str(e), e.kind)
                out["failed"].append(jid)
            continue
        except requests.ConnectTimeout as e:     # never connected: nothing can have been posted
            if attempt < MAX_ATTEMPTS:
                finish("scheduled", f"try {attempt} of {MAX_ATTEMPTS}: could not connect ({type(e).__name__})",
                       "network", scheduled_for=now + timedelta(seconds=RETRY_BASE_S * 2 ** (attempt - 1)))
                out["retry"].append(jid)
            else:
                finish("failed", f"could not connect: {e!r}", "network")
                out["failed"].append(jid)
            continue
        except requests.RequestException as e:   # dropped mid-way: the post may exist, a person must check
            finish("failed", f"connection lost during the upload ({type(e).__name__}); check the platform "
                   "before retrying so it is not posted twice", "network")
            out["failed"].append(jid)
            continue
        except Exception as e:  # unknown state: never auto-retry, a person must look
            finish("failed", f"unexpected: {e!r}", "unknown")
            out["failed"].append(jid)
            continue
        finish("published", None, external_id=result.external_id, external_url=result.url,
               published_at=datetime.now(timezone.utc))
        out["published"].append(jid)
    done = conn.execute(
        """UPDATE clips SET status = 'published' WHERE status = 'scheduled' AND EXISTS
             (SELECT 1 FROM publish_jobs p WHERE p.clip_id = clips.id AND p.status = 'published')
           AND NOT EXISTS (SELECT 1 FROM publish_jobs p WHERE p.clip_id = clips.id AND p.status IN ('scheduled', 'publishing'))
           RETURNING id""").fetchall()
    conn.commit()
    out["clips_published"] = [r["id"] for r in done]
    return out


def pull_analytics(conn, settings: Settings, *, publisher_factory: PublisherFactory = make_publisher,
                   days: int = 30) -> dict:
    if not settings.allow_live_publish:
        return {"skipped": "live publishing is switched off; no platform calls made", "pulled": 0}
    jobs = conn.execute(
        """SELECT p.*, ch.key AS channel_key, a.audited FROM publish_jobs p JOIN clips cl ON cl.id = p.clip_id
           JOIN channels ch ON ch.id = cl.channel_id JOIN platform_accounts a ON a.id = p.account_id
           WHERE p.status = 'published' AND p.external_id IS NOT NULL AND p.published_at > now() - %s::interval""",
        (f"{days} days",)).fetchall()
    out = {"pulled": 0, "errors": []}
    for job in jobs:
        try:
            m = publisher_factory(job["platform"], job["channel_key"], audited=job["audited"]).metrics(job["external_id"])
        except PlatformError as e:
            out["errors"].append({"job": job["id"], "error": str(e)})
            continue
        conn.execute(
            """INSERT INTO metrics (publish_job_id, captured_on, views, watch_time_s, avg_view_pct, likes, comments,
                                    shares, followers_delta, raw)
               VALUES (%s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (publish_job_id, captured_on) DO UPDATE SET views = EXCLUDED.views,
                   watch_time_s = EXCLUDED.watch_time_s, avg_view_pct = EXCLUDED.avg_view_pct, likes = EXCLUDED.likes,
                   comments = EXCLUDED.comments, shares = EXCLUDED.shares,
                   followers_delta = EXCLUDED.followers_delta, raw = EXCLUDED.raw""",
            (job["id"], m.get("views"), m.get("watch_time_s"), m.get("avg_view_pct"), m.get("likes"),
             m.get("comments"), m.get("shares"), m.get("followers_delta"), db.Jsonb(m.get("raw") or {})))
        out["pulled"] += 1
    conn.commit()
    return out


def take_down(conn, settings: Settings, *, clip_id: int | None = None, source_id: int | None = None, reason: str,
              publisher_factory: PublisherFactory = make_publisher) -> dict:
    """Remove a clip (or every clip cut from a source) everywhere and block it from further use."""
    if (clip_id is None) == (source_id is None):
        raise ValueError("give exactly one of clip_id or source_id")
    if source_id is not None:
        conn.execute("UPDATE sources SET status = 'blocked', status_reason = %s WHERE id = %s", (reason, source_id))
        conn.execute("UPDATE candidates SET status = 'blocked', status_reason = %s WHERE source_id = %s", (reason, source_id))
        clip_ids = [r["id"] for r in conn.execute(
            "SELECT cl.id FROM clips cl JOIN candidates c ON c.id = cl.candidate_id WHERE c.source_id = %s",
            (source_id,)).fetchall()]
        db.log_event(conn, "takedown", "source", source_id, "blocked", {"reason": reason})
    else:
        clip_ids = [clip_id]
    out = {"clips": clip_ids, "removed": [], "cancelled": [], "manual_removal_needed": [], "errors": []}
    for cid in clip_ids:
        jobs = conn.execute(
            """SELECT p.*, ch.key AS channel_key, a.audited FROM publish_jobs p JOIN clips cl ON cl.id = p.clip_id
               JOIN channels ch ON ch.id = cl.channel_id JOIN platform_accounts a ON a.id = p.account_id
               WHERE p.clip_id = %s""", (cid,)).fetchall()
        for job in jobs:
            if job["status"] in ("scheduled", "dry_run", "failed", "blocked"):
                conn.execute("UPDATE publish_jobs SET status = 'cancelled', last_error = %s WHERE id = %s", (reason, job["id"]))
                out["cancelled"].append(job["id"])
            elif job["status"] in ("published", "publishing"):
                entry = {"job": job["id"], "platform": job["platform"], "channel": job["channel_key"],
                         "external_id": job["external_id"], "url": job["external_url"]}
                try:
                    removed = publisher_factory(job["platform"], job["channel_key"],
                                                audited=job["audited"]).take_down(job["external_id"])
                except PlatformError as e:
                    removed = False
                    out["errors"].append({**entry, "error": str(e)})
                if removed:
                    conn.execute("UPDATE publish_jobs SET status = 'taken_down', last_error = %s WHERE id = %s",
                                 (reason, job["id"]))
                    out["removed"].append(entry)
                else:
                    out["manual_removal_needed"].append(entry)
        conn.execute("UPDATE clips SET status = 'taken_down', status_reason = %s WHERE id = %s", (reason, cid))
        db.log_event(conn, "takedown", "clip", cid, "taken_down", {"reason": reason})
    conn.commit()
    return out


def requeue(conn, job_id: int, *, reason: str, when: datetime | None = None) -> dict:
    """Put a failed or blocked job back in the queue after a person has checked it (e.g. the post is not on the
    platform). Never re-queues a job that is published, publishing or taken down."""
    job = conn.execute("SELECT * FROM publish_jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
    if job is None:
        raise ValueError(f"no publish job {job_id}")
    if job["status"] not in ("failed", "blocked", "dry_run"):
        raise ValueError(f"job {job_id} is {job['status']}; only failed, blocked or dry-run jobs can be re-queued")
    when = when or datetime.now(timezone.utc)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "requeued", "note": reason}
    conn.execute("UPDATE publish_jobs SET status = 'scheduled', scheduled_for = %s, attempts = 0, last_error = NULL, "
                 "error_kind = NULL, attempt_log = attempt_log || %s::jsonb WHERE id = %s",
                 (when, db.Jsonb([entry]), job_id))
    db.log_event(conn, "publish", "publish_job", job_id, "requeued", {"reason": reason})
    conn.commit()
    return {"job": job_id, "scheduled_for": when.isoformat()}


def queue_report(conn) -> dict:
    """Publishing queue at a glance: counts per platform and status, and the latest errors with their kind."""
    counts = conn.execute("SELECT platform, status, count(*) AS n FROM publish_jobs GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    errors = conn.execute(
        """SELECT p.id, p.platform, ch.key AS channel, p.status, p.error_kind, p.last_error, p.attempts, p.scheduled_for
           FROM publish_jobs p JOIN clips cl ON cl.id = p.clip_id JOIN channels ch ON ch.id = cl.channel_id
           WHERE p.last_error IS NOT NULL AND p.status IN ('failed', 'blocked', 'scheduled')
           ORDER BY p.id DESC LIMIT 50""").fetchall()
    by: dict = {}
    for r in counts:
        by.setdefault(r["platform"], {})[r["status"]] = r["n"]
    return {"counts": by, "errors": [{**e, "scheduled_for": e["scheduled_for"].isoformat()} for e in errors]}


def health(conn, settings: Settings, *, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    alerts: list[str] = []

    def q(sql: str, *args) -> list[dict]:
        return conn.execute(sql, args).fetchall()

    for r in q("SELECT id, platform, last_error FROM publish_jobs WHERE status = 'failed'"):
        alerts.append(f"publish job {r['id']} ({r['platform']}) failed: {r['last_error']}")
    for r in q("SELECT id, platform, last_error FROM publish_jobs WHERE status = 'blocked'"):
        alerts.append(f"publish job {r['id']} ({r['platform']}) blocked: {r['last_error']}")
    for r in q("SELECT id FROM publish_jobs WHERE status = 'publishing'"):
        alerts.append(f"publish job {r['id']} is stuck in 'publishing'; check the platform by hand before retrying")
    for r in q("""SELECT id, owner_name, expires_on FROM rights_ledger WHERE revoked_at IS NULL
                  AND expires_on IS NOT NULL AND expires_on <= %s""", today + timedelta(days=14)):
        alerts.append(f"licence {r['id']} ({r['owner_name']}) expires {r['expires_on']}")
    for r in q("SELECT id, status_reason FROM sources WHERE status = 'quarantined'"):
        alerts.append(f"source {r['id']} quarantined: {r['status_reason']}")
    for r in q("""SELECT ch.key, ch.clips_per_day, count(cl.id) FILTER (WHERE cl.status IN
                     ('pending_approval', 'approved', 'scheduled', 'published')) AS ready
                  FROM channels ch LEFT JOIN clips cl ON cl.channel_id = ch.id AND cl.for_date = %s
                  GROUP BY ch.id""", today):
        if r["ready"] < r["clips_per_day"]:
            alerts.append(f"channel {r['key']}: {r['ready']} of {r['clips_per_day']} clips ready for {today}")
    pending = q("SELECT count(*) AS n FROM clips WHERE status = 'pending_approval'")[0]["n"]
    if pending:
        alerts.append(f"{pending} clips waiting for approval")
    return {"healthy": not alerts, "alerts": alerts, "live_publish": settings.allow_live_publish,
            "llm_mode": settings.llm_mode, "game": settings.game}


__all__ = ["schedule_approved", "run_due", "requeue", "queue_report", "retry_delay", "pull_analytics", "take_down", "health", "slot_times", "psycopg"]
