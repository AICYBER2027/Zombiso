"""Creator clients: accounts, sign-in sessions, roles, branding, editing preferences, permission records,
deliveries, usage limits per plan, analytics, and the billing architecture (no live payments).

Security notes
- Passwords: PBKDF2-HMAC-SHA256 (600k iterations by default), per-user salt, constant-time compare.
- Sessions: a random token in an HttpOnly cookie; only its SHA-256 is stored. Each session has its own CSRF token.
- Sign-in lockout after 5 failed attempts (15 minutes).
- Every read and write is scoped to the signed-in user's client unless the user is VicePulse staff.
- Permission records from the website are requests. They never make footage usable on their own: VicePulse staff
  check the evidence and only then write the verified rights-ledger row the pipeline's gates look at.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db, rights

ROLES = ("owner", "editor", "viewer")
STYLES = ("cinematic", "viral", "clean", "pro")
LAYOUTS = ("auto", "crop", "blur")
FRAME_STYLES = ("gradient", "solid", "neon", "minimal")
PLATFORMS = ("youtube", "instagram", "tiktok", "facebook", "twitch", "kick")
SESSION_DAYS = 14
MAX_FAILED = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")

# what each role may do inside its own client
PERMS = {
    "owner": {"view", "edit_branding", "edit_preferences", "review_delivery", "manage_permissions", "manage_plan"},
    "editor": {"view", "edit_branding", "edit_preferences", "review_delivery"},
    "viewer": {"view"},
}


class AccessDenied(PermissionError):
    pass


# ---------------------------------------------------------------- passwords and sessions

def _iterations() -> int:
    return int(os.environ.get("VICEPULSE_PBKDF2_ITERATIONS", "600000"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n = _iterations()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, n)
    return f"pbkdf2_sha256${n}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(n))
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_password_rules(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
    if password.lower() in (email.lower(), email.split("@")[0].lower()):
        raise ValueError("the password cannot be your email address")
    if len(set(password)) < 5:
        raise ValueError("the password is too simple")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return s if len(s) >= 3 else (s + "-creator")[:40]


def register(conn, *, email: str, password: str, display_name: str, language: str = "en",
             accept_terms: bool = False) -> dict:
    """New creator account (status pending, Trial plan, no payment). Returns {client_id, user_id}."""
    email = (email or "").strip().lower()
    display_name = (display_name or "").strip()
    if not accept_terms:
        raise ValueError("please accept the terms and the privacy policy")
    if not EMAIL.match(email):
        raise ValueError("please enter a valid email address")
    if not (2 <= len(display_name) <= 60):
        raise ValueError("the creator name needs 2 to 60 characters")
    if language not in ("en", "pt-BR"):
        raise ValueError("language must be en or pt-BR")
    check_password_rules(password, email)
    if conn.execute("SELECT 1 FROM client_users WHERE email = %s", (email,)).fetchone():
        raise ValueError("an account with this email already exists")
    base = slugify(display_name)
    slug, i = base, 1
    while conn.execute("SELECT 1 FROM clients WHERE slug = %s", (slug,)).fetchone():
        i += 1
        slug = f"{base[:36]}-{i}"
    client = conn.execute(
        """INSERT INTO clients (slug, display_name, language, branding, preferences) VALUES (%s, %s, %s, %s, %s)
           RETURNING id""",
        (slug, display_name, language, db.Jsonb({"creator_name": display_name}),
         db.Jsonb({"style": "clean", "layout": "auto", "captions": True, "facecam": "auto"}))).fetchone()["id"]
    user = conn.execute("INSERT INTO client_users (client_id, email, password_hash, role) VALUES (%s, %s, %s, 'owner') "
                        "RETURNING id", (client, email, hash_password(password))).fetchone()["id"]
    conn.execute("INSERT INTO subscriptions (client_id, plan_key, status, provider) VALUES (%s, 'free', 'trialing', 'none')",
                 (client,))
    db.log_event(conn, "client", "client", client, "registered", {"language": language})
    conn.commit()
    return {"client_id": client, "user_id": user, "slug": slug}


def add_user(conn, actor: dict, *, email: str, password: str, role: str) -> int:
    require(actor, "manage_permissions", actor["client_id"])
    if role not in ROLES:
        raise ValueError("unknown role")
    email = email.strip().lower()
    if not EMAIL.match(email):
        raise ValueError("please enter a valid email address")
    check_password_rules(password, email)
    uid = conn.execute("INSERT INTO client_users (client_id, email, password_hash, role) VALUES (%s, %s, %s, %s) "
                       "RETURNING id", (actor["client_id"], email, hash_password(password), role)).fetchone()["id"]
    conn.commit()
    return uid


def create_staff(conn, *, email: str, password: str) -> int:
    """A VicePulse staff login (CLI only). Staff belong to a 'vicepulse-staff' client and can see every client."""
    check_password_rules(password, email)
    row = conn.execute("SELECT id FROM clients WHERE slug = 'vicepulse-staff'").fetchone()
    cid = row["id"] if row else conn.execute(
        "INSERT INTO clients (slug, display_name, status) VALUES ('vicepulse-staff', 'VicePulse staff', 'active') "
        "RETURNING id").fetchone()["id"]
    uid = conn.execute("""INSERT INTO client_users (client_id, email, password_hash, role, is_staff)
                          VALUES (%s, %s, %s, 'owner', true) RETURNING id""",
                       (cid, email.strip().lower(), hash_password(password))).fetchone()["id"]
    conn.commit()
    return uid


def authenticate(conn, email: str, password: str, *, now: datetime | None = None) -> dict | None:
    """The user row on success; None on a wrong email/password or while locked (same answer, no hints)."""
    now = now or datetime.now(timezone.utc)
    u = conn.execute("SELECT * FROM client_users WHERE email = %s", ((email or "").strip().lower(),)).fetchone()
    if u is None:
        verify_password(password, hash_password("x" * 12))        # same time either way
        return None
    if u["locked_until"] and u["locked_until"] > now:
        return None
    if not verify_password(password, u["password_hash"]):
        fails = u["failed_logins"] + 1
        lock = now + timedelta(minutes=LOCK_MINUTES) if fails >= MAX_FAILED else None
        conn.execute("UPDATE client_users SET failed_logins = %s, locked_until = %s WHERE id = %s",
                     (0 if lock else fails, lock, u["id"]))
        conn.commit()
        return None
    conn.execute("UPDATE client_users SET failed_logins = 0, locked_until = NULL, last_login_at = %s WHERE id = %s",
                 (now, u["id"]))
    conn.commit()
    return u


def create_session(conn, user_id: int, *, now: datetime | None = None) -> tuple[str, str]:
    """(cookie token, csrf token)."""
    now = now or datetime.now(timezone.utc)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    conn.execute("DELETE FROM web_sessions WHERE user_id = %s AND expires_at < %s", (user_id, now))
    conn.execute("INSERT INTO web_sessions (token_sha256, user_id, csrf, expires_at) VALUES (%s, %s, %s, %s)",
                 (_sha(token), user_id, csrf, now + timedelta(days=SESSION_DAYS)))
    conn.commit()
    return token, csrf


def session_user(conn, token: str | None, *, now: datetime | None = None) -> dict | None:
    if not token:
        return None
    now = now or datetime.now(timezone.utc)
    return conn.execute(
        """SELECT u.id, u.client_id, u.email, u.role, u.is_staff, s.csrf, c.slug, c.display_name, c.language,
                  c.status AS client_status, c.plan_key
           FROM web_sessions s JOIN client_users u ON u.id = s.user_id JOIN clients c ON c.id = u.client_id
           WHERE s.token_sha256 = %s AND s.expires_at > %s""", (_sha(token), now)).fetchone()


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM web_sessions WHERE token_sha256 = %s", (_sha(token),))
        conn.commit()


def change_password(conn, user: dict, old: str, new: str) -> None:
    row = conn.execute("SELECT password_hash, email FROM client_users WHERE id = %s", (user["id"],)).fetchone()
    if not verify_password(old, row["password_hash"]):
        raise ValueError("the current password is wrong")
    check_password_rules(new, row["email"])
    conn.execute("UPDATE client_users SET password_hash = %s WHERE id = %s", (hash_password(new), user["id"]))
    conn.execute("DELETE FROM web_sessions WHERE user_id = %s", (user["id"],))      # sign out everywhere
    conn.commit()


# ---------------------------------------------------------------- access control

def can(user: dict | None, action: str, client_id: int) -> bool:
    if not user:
        return False
    if user.get("is_staff"):
        return True
    return user["client_id"] == client_id and action in PERMS.get(user["role"], set())


def require(user: dict | None, action: str, client_id: int) -> None:
    if not can(user, action, client_id):
        raise AccessDenied(f"not allowed: {action}")


def get_client(conn, user: dict, client_id: int) -> dict:
    require(user, "view", client_id)
    c = conn.execute("SELECT * FROM clients WHERE id = %s", (client_id,)).fetchone()
    if c is None:
        raise AccessDenied("no such client")
    return c


# ---------------------------------------------------------------- plans and billing architecture

def plans(conn) -> list[dict]:
    return conn.execute("SELECT * FROM plans WHERE active ORDER BY sort").fetchall()


def plan_of(conn, client_id: int) -> dict:
    return conn.execute("SELECT p.* FROM plans p JOIN clients c ON c.plan_key = p.key WHERE c.id = %s",
                        (client_id,)).fetchone()


class PaymentsDisabled(RuntimeError):
    pass


class NoPayments:
    """The only payment provider wired in. A real provider (Stripe / Mercado Pago) would implement the same two
    methods once the owner approves it; until then nothing can be charged."""
    name = "none"

    def checkout_url(self, client: dict, plan: dict) -> str:
        raise PaymentsDisabled("online payments are not switched on yet; VicePulse will contact you")

    def cancel(self, subscription: dict) -> None:
        return None


def payment_provider():
    if os.environ.get("VICEPULSE_PAYMENTS", "off") != "off":
        raise PaymentsDisabled("VICEPULSE_PAYMENTS is set but no payment provider is implemented or approved")
    return NoPayments()


def request_plan(conn, user: dict, plan_key: str) -> dict:
    """The client asks for a plan. Free trial: switched at once. Paid plans: recorded as a request (status 'none',
    provider 'none') for VicePulse to confirm by hand. Never charges anything."""
    require(user, "manage_plan", user["client_id"])
    plan = conn.execute("SELECT * FROM plans WHERE key = %s AND active", (plan_key,)).fetchone()
    if plan is None:
        raise ValueError("unknown plan")
    cid = user["client_id"]
    if plan["monthly_price_cents"] == 0:
        conn.execute("UPDATE clients SET plan_key = %s WHERE id = %s", (plan_key, cid))
        status = "trialing"
    else:
        status = "none"
    sub = conn.execute("INSERT INTO subscriptions (client_id, plan_key, status, provider) VALUES (%s, %s, %s, 'none') "
                       "RETURNING id", (cid, plan_key, status)).fetchone()["id"]
    db.log_event(conn, "billing", "client", cid, "plan_requested", {"plan": plan_key, "charged": False})
    conn.commit()
    return {"subscription_id": sub, "plan": plan_key, "status": status, "charged": False}


def staff_set_plan(conn, staff: dict, client_id: int, plan_key: str, *, until: date | None = None,
                   note: str = "manual") -> None:
    """Staff confirm a plan after an offline agreement (provider 'manual'); no payment is taken here."""
    if not staff.get("is_staff"):
        raise AccessDenied("staff only")
    conn.execute("UPDATE clients SET plan_key = %s, status = 'active' WHERE id = %s", (plan_key, client_id))
    conn.execute("""INSERT INTO subscriptions (client_id, plan_key, status, provider, provider_ref, current_period_start,
                    current_period_end) VALUES (%s, %s, 'active', 'manual', %s, CURRENT_DATE, %s)""",
                 (client_id, plan_key, note[:100], until))
    db.log_event(conn, "billing", "client", client_id, "plan_set", {"plan": plan_key, "by": staff["email"]})
    conn.commit()


def usage(conn, client_id: int, *, today: date | None = None) -> dict:
    today = today or date.today()
    start = today.replace(day=1)
    used = conn.execute("""SELECT count(*) AS n FROM client_deliveries WHERE client_id = %s AND created_at >= %s
                           AND status <> 'withdrawn'""", (client_id, start)).fetchone()["n"]
    plan = plan_of(conn, client_id)
    return {"plan": plan["key"], "plan_name": plan["name"], "clips_used": used, "clips_included": plan["clips_per_month"],
            "clips_left": max(0, plan["clips_per_month"] - used), "period_start": start.isoformat()}


# ---------------------------------------------------------------- branding and preferences

def update_branding(conn, user: dict, client_id: int, data: dict) -> dict:
    require(user, "edit_branding", client_id)
    c = get_client(conn, user, client_id)
    b = dict(c["branding"] or {})
    name = (data.get("creator_name") or "").strip()
    if name:
        if len(name) > 40:
            raise ValueError("the creator name can have at most 40 characters")
        b["creator_name"] = name
    handle = (data.get("handle") or "").strip()
    if handle:
        if not re.match(r"^@?[A-Za-z0-9_.-]{2,30}$", handle):
            raise ValueError("the handle can only use letters, numbers, . _ and -")
        b["handle"] = handle if handle.startswith("@") else "@" + handle
    mono = (data.get("monogram") or "").strip().upper()
    if mono:
        if not re.match(r"^[A-Z0-9]{1,3}$", mono):
            raise ValueError("the monogram is 1 to 3 letters or numbers")
        b["monogram"] = mono
    theme = dict(b.get("theme") or {})
    for k in ("primary", "secondary", "accent", "dark"):
        v = (data.get(k) or "").strip()
        if v:
            if not HEX.match(v):
                raise ValueError(f"{k} colour must look like #FF2E88")
            theme[k] = v.upper()
    if theme:
        b["theme"] = theme
    frame = dict(b.get("frame") or {})
    if data.get("frame_style"):
        if data["frame_style"] not in FRAME_STYLES:
            raise ValueError("unknown frame style")
        frame["style"] = data["frame_style"]
    for k, lo, hi in (("frame_radius", 0, 80), ("frame_thickness", 2, 20)):
        if data.get(k) not in (None, ""):
            v = int(data[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k.replace('_', ' ')} must be between {lo} and {hi}")
            frame[k.split("_")[1]] = v
    if frame:
        b["frame"] = frame
    for k in ("outro_text", "intro_text"):
        if k in data:
            v = (data.get(k) or "").strip()
            if len(v) > 40:
                raise ValueError("intro and outro lines can have at most 40 characters")
            b[k] = v
    conn.execute("UPDATE clients SET branding = %s WHERE id = %s", (db.Jsonb(b), client_id))
    conn.commit()
    return b


def save_logo(conn, user: dict, client_id: int, data: bytes, data_dir: Path) -> str:
    """Validate an uploaded logo (PNG/JPEG/WebP, max 5 MB), store a cleaned 1024 px PNG copy. Returns its path."""
    require(user, "edit_branding", client_id)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("the logo can be at most 5 MB")
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG", "WEBP"):
            raise ValueError
        img.load()
    except Exception:
        raise ValueError("the logo must be a PNG, JPEG or WebP image") from None
    img = img.convert("RGBA")
    img.thumbnail((1024, 1024))
    folder = Path(data_dir) / "clients" / str(client_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "logo.png"
    img.save(path, "PNG")                         # re-encoded: drops metadata and anything hidden in the file
    c = get_client(conn, user, client_id)
    b = dict(c["branding"] or {})
    b["logo"] = str(path)
    conn.execute("UPDATE clients SET branding = %s WHERE id = %s", (db.Jsonb(b), client_id))
    conn.commit()
    return str(path)


def update_preferences(conn, user: dict, client_id: int, data: dict) -> dict:
    require(user, "edit_preferences", client_id)
    c = get_client(conn, user, client_id)
    plan = plan_of(conn, client_id)
    p = dict(c["preferences"] or {})
    if data.get("style"):
        if data["style"] not in STYLES:
            raise ValueError("unknown style")
        if data["style"] not in plan["styles"]:
            raise ValueError(f"the {data['style']} style is not in your plan ({plan['name']})")
        p["style"] = data["style"]
    if data.get("layout"):
        if data["layout"] not in LAYOUTS:
            raise ValueError("unknown layout")
        p["layout"] = data["layout"]
    if "captions" in data:
        p["captions"] = data["captions"] in (True, "1", "on", "true", "yes")
    if data.get("caption_language"):
        if data["caption_language"] not in ("auto", "en", "pt-BR"):
            raise ValueError("unknown caption language")
        p["caption_language"] = data["caption_language"]
    if "platforms" in data:
        plats = [x for x in (data["platforms"] if isinstance(data["platforms"], list) else
                             str(data["platforms"]).split(",")) if x]
        bad = [x for x in plats if x not in PLATFORMS]
        if bad:
            raise ValueError("unknown platform: " + ", ".join(bad))
        p["platforms"] = plats
    if data.get("facecam"):
        fc = data["facecam"].strip()
        if fc not in ("auto", "none") and not re.match(r"^0?\.\d+,0?\.\d+,0?\.\d+,0?\.\d+(:pip|:split)?$", fc):
            raise ValueError("facecam is auto, none, or x,y,w,h as fractions (e.g. 0.02,0.63,0.24,0.33)")
        p["facecam"] = fc
    conn.execute("UPDATE clients SET preferences = %s WHERE id = %s", (db.Jsonb(p), client_id))
    conn.commit()
    return p


def brand_for_render(client: dict) -> dict:
    """The brand dict the edit styles understand, from a client's branding."""
    b = dict(client.get("branding") or {})
    out = {"creator_name": b.get("creator_name") or client.get("display_name"), "handle": b.get("handle"),
           "monogram": b.get("monogram"), "logo": b.get("logo"), "theme": b.get("theme"), "frame": b.get("frame"),
           "outro_text": b.get("outro_text") or "", "intro_text": b.get("intro_text") or "", "credit": ""}
    return {k: v for k, v in out.items() if v not in (None, "")}


def render_options(conn, client: dict) -> dict:
    """Style, layout, facecam and size for this client's clips, limited by the plan."""
    plan = plan_of(conn, client["id"])
    p = client.get("preferences") or {}
    style = p.get("style") if p.get("style") in plan["styles"] else plan["styles"][0]
    fc = p.get("facecam", "auto")
    return {"style": style, "layout": p.get("layout", "auto"), "facecam": None if fc == "none" else fc,
            "size": "1080x1920" if (plan["features"] or {}).get("resolution") == "1080p" else "720x1280",
            "captions": p.get("captions", True), "watermark": bool((plan["features"] or {}).get("watermark")),
            "brand": brand_for_render(client)}


# ---------------------------------------------------------------- permission records

def request_permission(conn, user: dict, client_id: int, *, scope: str, platforms: list[str] | None = None,
                       evidence_url: str = "", note: str = "") -> int:
    require(user, "manage_permissions", client_id)
    if scope not in rights.SCOPES:
        raise ValueError("unknown permission")
    plats = [p for p in (platforms or []) if p]
    if any(p not in PLATFORMS for p in plats):
        raise ValueError("unknown platform")
    if evidence_url and not re.match(r"^https://\S+$", evidence_url):
        raise ValueError("the evidence link must start with https://")
    pid = conn.execute(
        """INSERT INTO client_permissions (client_id, scope, platforms, evidence_url, note, granted_by_user)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (client_id, scope, plats, evidence_url or None, note[:500] or None, user["id"])).fetchone()["id"]
    db.log_event(conn, "client", "client_permission", pid, "requested", {"scope": scope, "client": client_id})
    conn.commit()
    return pid


def revoke_permission(conn, user: dict, perm_id: int) -> None:
    """The creator withdraws a permission. Takes effect at once: the rights row loses the scope (or is revoked)."""
    perm = conn.execute("SELECT * FROM client_permissions WHERE id = %s", (perm_id,)).fetchone()
    if perm is None:
        raise ValueError("no such permission")
    require(user, "manage_permissions", perm["client_id"])
    conn.execute("UPDATE client_permissions SET status = 'revoked', revoked_at = now() WHERE id = %s", (perm_id,))
    if perm["rights_id"]:
        _sync_rights(conn, perm["client_id"], perm["rights_id"])
    db.log_event(conn, "client", "client_permission", perm_id, "revoked", {"by": user["email"]})
    conn.commit()


def _sync_rights(conn, client_id: int, rights_id: int) -> None:
    scopes = sorted({r["scope"] for r in conn.execute(
        "SELECT scope FROM client_permissions WHERE client_id = %s AND rights_id = %s AND status = 'verified'",
        (client_id, rights_id)).fetchall()})
    plats = sorted({p for r in conn.execute(
        "SELECT platforms FROM client_permissions WHERE client_id = %s AND rights_id = %s AND status = 'verified'",
        (client_id, rights_id)).fetchall() for p in r["platforms"]})
    if scopes:
        conn.execute("UPDATE rights_ledger SET scopes = %s, platforms = %s, monetisation_allowed = %s WHERE id = %s",
                     (scopes, plats, "monetisation" in scopes, rights_id))
    else:
        conn.execute("UPDATE rights_ledger SET revoked_at = now(), revoked_reason = 'creator withdrew every permission' "
                     "WHERE id = %s AND revoked_at IS NULL", (rights_id,))


def staff_verify_permission(conn, staff: dict, perm_id: int, *, document_url: str, approve: bool = True,
                            expires_on: date | None = None) -> int | None:
    """Staff checked the creator's evidence. Approve -> the client's rights-ledger row gets the scope (created on
    first approval, verified by this staff member). Reject -> nothing becomes usable."""
    if not staff.get("is_staff"):
        raise AccessDenied("staff only")
    perm = conn.execute("SELECT p.*, c.display_name FROM client_permissions p JOIN clients c ON c.id = p.client_id "
                        "WHERE p.id = %s", (perm_id,)).fetchone()
    if perm is None or perm["status"] != "requested":
        raise ValueError("no open permission request with that id")
    if not approve:
        conn.execute("UPDATE client_permissions SET status = 'rejected', verified_by = %s, verified_at = now() "
                     "WHERE id = %s", (staff["email"], perm_id))
        conn.commit()
        return None
    if not re.match(r"^https://\S+$", document_url or ""):
        raise ValueError("a link to the signed licence or the creator's written permission is required")
    rid = conn.execute(
        """SELECT DISTINCT rights_id FROM client_permissions WHERE client_id = %s AND rights_id IS NOT NULL
           AND status = 'verified' ORDER BY rights_id DESC LIMIT 1""", (perm["client_id"],)).fetchone()
    rid = rid["rights_id"] if rid else None
    if rid is not None:
        live = conn.execute("SELECT revoked_at FROM rights_ledger WHERE id = %s", (rid,)).fetchone()
        rid = rid if live and live["revoked_at"] is None else None
    if rid is None:
        rid = conn.execute(
            """INSERT INTO rights_ledger (owner_name, licence_type, platforms, languages, starts_on, expires_on,
                   monetisation_allowed, derivative_edits_allowed, document_url, verified_by, verified_at, scopes, notes)
               VALUES (%s, 'creator_permission', %s, ARRAY['*'], CURRENT_DATE, %s, false, true, %s, %s, now(), %s, %s)
               RETURNING id""",
            (perm["display_name"], perm["platforms"] or [], expires_on, document_url, staff["email"], [perm["scope"]],
             f"client {perm['client_id']} (website permission records)")).fetchone()["id"]
    conn.execute("UPDATE client_permissions SET status = 'verified', verified_by = %s, verified_at = now(), rights_id = %s "
                 "WHERE id = %s", (staff["email"], rid, perm_id))
    _sync_rights(conn, perm["client_id"], rid)
    conn.execute("UPDATE clients SET status = 'active' WHERE id = %s AND status = 'pending'", (perm["client_id"],))
    db.log_event(conn, "client", "client_permission", perm_id, "verified", {"rights_id": rid, "by": staff["email"]})
    conn.commit()
    return rid


def permissions(conn, user: dict, client_id: int) -> list[dict]:
    require(user, "view", client_id)
    return conn.execute("SELECT * FROM client_permissions WHERE client_id = %s ORDER BY created_at DESC",
                        (client_id,)).fetchall()


def open_permission_requests(conn, staff: dict) -> list[dict]:
    if not staff.get("is_staff"):
        raise AccessDenied("staff only")
    return conn.execute("""SELECT p.*, c.display_name FROM client_permissions p JOIN clients c ON c.id = p.client_id
                           WHERE p.status = 'requested' ORDER BY p.created_at""").fetchall()


# ---------------------------------------------------------------- deliveries, history, analytics

def add_delivery(conn, *, client_id: int, file_path: str, title: str, style: str | None = None,
                 clip_id: int | None = None, duration_s: float | None = None, file_sha256: str | None = None) -> int:
    did = conn.execute(
        """INSERT INTO client_deliveries (client_id, clip_id, title, style, file_path, file_sha256, duration_s)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (client_id, clip_id, title[:120], style, file_path, file_sha256, duration_s)).fetchone()["id"]
    db.log_event(conn, "client", "client_delivery", did, "delivered", {"client": client_id})
    conn.commit()
    return did


def deliveries(conn, user: dict, client_id: int, limit: int = 100) -> list[dict]:
    require(user, "view", client_id)
    return conn.execute("SELECT * FROM client_deliveries WHERE client_id = %s ORDER BY created_at DESC LIMIT %s",
                        (client_id, limit)).fetchall()


def delivery(conn, user: dict, delivery_id: int) -> dict:
    d = conn.execute("SELECT * FROM client_deliveries WHERE id = %s", (delivery_id,)).fetchone()
    if d is None:
        raise AccessDenied("no such delivery")
    require(user, "view", d["client_id"])
    return d


def review_delivery(conn, user: dict, delivery_id: int, *, approve: bool, note: str = "") -> None:
    d = delivery(conn, user, delivery_id)
    require(user, "review_delivery", d["client_id"])
    if d["status"] not in ("ready", "approved", "changes_requested"):
        raise ValueError("this clip cannot be reviewed")
    conn.execute("UPDATE client_deliveries SET status = %s, client_note = %s WHERE id = %s",
                 ("approved" if approve else "changes_requested", note[:1000] or None, delivery_id))
    db.log_event(conn, "client", "client_delivery", delivery_id, "approved" if approve else "changes_requested", {})
    conn.commit()


def analytics(conn, user: dict, client_id: int, *, days: int = 30) -> dict:
    require(user, "view", client_id)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute("""SELECT status, style, views, created_at FROM client_deliveries
                           WHERE client_id = %s AND created_at >= %s""", (client_id, since)).fetchall()
    by_status: dict = {}
    by_style: dict = {}
    per_day: dict = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_style[r["style"] or "classic"] = by_style.get(r["style"] or "classic", 0) + 1
        d = r["created_at"].date().isoformat()
        per_day[d] = per_day.get(d, 0) + 1
    views = [r["views"] for r in rows if r["views"] is not None]
    approved = by_status.get("approved", 0)
    reviewed = approved + by_status.get("changes_requested", 0)
    return {"days": days, "clips": len(rows), "by_status": by_status, "by_style": by_style,
            "per_day": dict(sorted(per_day.items())), "views": sum(views) if views else None,
            "approval_rate": round(approved / reviewed, 2) if reviewed else None}


__all__ = ["register", "authenticate", "create_session", "session_user", "end_session", "can", "require",
           "AccessDenied", "update_branding", "update_preferences", "save_logo", "request_permission",
           "staff_verify_permission", "revoke_permission", "add_delivery", "deliveries", "review_delivery",
           "analytics", "usage", "plans", "request_plan", "staff_set_plan", "brand_for_render", "render_options",
           "payment_provider", "PaymentsDisabled", "hash_password", "verify_password"]
