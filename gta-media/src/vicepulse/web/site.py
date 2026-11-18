"""VicePulse website: public pages (EN + PT-BR), creator sign-up/sign-in, the creator dashboard, permission
records, clip delivery and preview pages, plans (no live payments) and a small staff page.

Standard library only, like the review page. `App.handle(Request) -> Response` holds all the logic so tests can
call it without a socket; `serve()` wraps it in an HTTP server bound to 127.0.0.1 by default.
Nothing here is reachable from the internet unless the owner deploys it.
"""

from __future__ import annotations

import email.parser
import email.policy
import html
import json
import mimetypes
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, urlparse

from .. import clients, db, rights
from ..config import PACKAGE_ROOT, Settings
from . import assets
from .i18n import LANG_CODE, LANGS, t

COOKIE = "vp_session"
FONT_DIR = PACKAGE_ROOT / "assets" / "fonts"
LOGO = PACKAGE_ROOT / "assets" / "brand" / "vicepulse_en_logo.jpg"
LEGAL_DIR = PACKAGE_ROOT / "legal"
MAX_BODY = 6 * 1024 * 1024

# Plans shown when there is no database (static export). Mirrors migration 006.
DEFAULT_PLANS = [
    {"key": "free", "name": "Trial", "monthly_price_cents": 0, "currency": "USD", "clips_per_month": 5, "live_feeds": 0,
     "styles": ["clean"], "features": {"watermark": True, "resolution": "720p", "support": "email"}},
    {"key": "starter", "name": "Starter", "monthly_price_cents": 4900, "currency": "USD", "clips_per_month": 30,
     "live_feeds": 0, "styles": ["clean", "cinematic", "viral"],
     "features": {"watermark": False, "resolution": "1080p", "captions": True, "support": "email"}},
    {"key": "pro", "name": "Pro", "monthly_price_cents": 14900, "currency": "USD", "clips_per_month": 120,
     "live_feeds": 1, "styles": ["clean", "cinematic", "viral", "pro"],
     "features": {"watermark": False, "resolution": "1080p", "captions": True, "branding": True,
                  "live_clipping": True, "support": "priority"}},
    {"key": "studio", "name": "Studio", "monthly_price_cents": 39900, "currency": "USD", "clips_per_month": 400,
     "live_feeds": 3, "styles": ["clean", "cinematic", "viral", "pro"],
     "features": {"watermark": False, "resolution": "1080p", "captions": True, "branding": True,
                  "live_clipping": True, "publishing": True, "analytics": True, "support": "dedicated"}},
]


def e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


@dataclass
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)          # lower-case names
    body: bytes = b""

    @property
    def cookies(self) -> dict:
        c = SimpleCookie()
        try:
            c.load(self.headers.get("cookie", ""))
        except Exception:
            return {}
        return {k: m.value for k, m in c.items()}

    def form(self) -> dict:
        ctype = self.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + self.body)
            out: dict = {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                if part.get_filename() is not None:
                    out[name] = part.get_payload(decode=True) or b""
                else:
                    out[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            return out
        parsed = parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)
        return {k: (v if len(v) > 1 else v[0]) for k, v in parsed.items()}


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict = field(default_factory=dict)

    @staticmethod
    def html(text: str, status: int = 200, **headers) -> "Response":
        return Response(status, text.encode(), {"Content-Type": "text/html; charset=utf-8", **headers})

    @staticmethod
    def redirect(where: str, **headers) -> "Response":
        return Response(303, b"", {"Location": where, **headers})


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' "
                               "'unsafe-inline'; script-src 'none'; form-action 'self'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class App:
    def __init__(self, settings: Settings, connect: Callable | None = None, *, secure_cookies: bool | None = None,
                 app_url: str = ""):
        self.settings = settings
        self.connect = connect or (lambda: db.connect(settings))
        self.secure = (os.environ.get("VICEPULSE_WEB_SECURE", "0") == "1") if secure_cookies is None else secure_cookies
        self.app_url = app_url.rstrip("/")         # where sign-up lives (static export points here)
        self.static_mode = False

    # ------------------------------------------------------------------ layout
    def layout(self, lang: str, title: str, body: str, *, user: dict | None = None, path: str = "") -> str:
        other = "pt" if lang == "en" else "en"
        swap = re.sub(r"^/(en|pt)(?=/|$)", f"/{other}", path or f"/{lang}") or f"/{other}"
        a = self.app_url
        if user:
            right = (f'<a href="/{lang}/dashboard" class="btn small ghost">{t(lang, "nav.dashboard")}</a>'
                     f'<form method=post action="/{lang}/logout" style="display:inline">{self.csrf_field(user)}'
                     f'<button class="btn small ghost">{t(lang, "nav.logout")}</button></form>')
        else:
            right = (f'<a href="{a}/{lang}/login">{t(lang, "nav.login")}</a>'
                     f'<a href="{a}/{lang}/signup" class="btn small primary">{t(lang, "nav.signup")}</a>')
        ext = ".html" if self.static_mode else ""
        home = f"/{lang}/" if not self.static_mode else f"/{lang}/index.html"
        return (
            f'<!doctype html><html lang="{LANG_CODE[lang]}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} | VicePulse</title>'
            f'<meta name="description" content="{e(t(lang, "hero.lead"))}">'
            f'<link rel="alternate" hreflang="en" href="{e(re.sub(r"^/pt", "/en", path or "/en"))}">'
            f'<link rel="alternate" hreflang="pt-BR" href="{e(re.sub(r"^/en", "/pt", path or "/pt"))}">'
            f'<link rel="icon" href="/static/logo.jpg"><link rel="stylesheet" href="/static/site.css"></head><body>'
            f'<header class="top"><div class="wrap"><a class="brand" href="{home}"><img src="/static/logo.jpg" alt="">'
            f'VICEPULSE</a><nav class="main"><a href="{home}#styles">{t(lang, "nav.styles")}</a>'
            f'<a href="{home}#how">{t(lang, "nav.how")}</a><a href="/{lang}/pricing{ext}">{t(lang, "nav.pricing")}</a>'
            f'{right}</nav></div></header><main>{body}</main>'
            f'<footer><div class="wrap"><span>© VicePulse</span><a href="/{lang}/privacy{ext}">{t(lang, "footer.privacy")}</a>'
            f'<a href="/{lang}/terms{ext}">{t(lang, "footer.terms")}</a><a href="{e(swap)}{ext if self.static_mode and swap.count("/") > 1 else ""}">'
            f'{t(lang, "footer.lang")}</a><span style="flex-basis:100%">{t(lang, "footer.rights")}</span></div></footer>'
            f'</body></html>')

    @staticmethod
    def csrf_field(user: dict | None) -> str:
        return f'<input type="hidden" name="csrf" value="{e(user["csrf"])}">' if user else ""

    # ------------------------------------------------------------------ public pages
    def home(self, lang: str) -> str:
        cards = "".join(
            f'<div class="card">{assets.preview(s)}<h3>{t(lang, "style." + s)}</h3><p>{t(lang, "style." + s + ".d")}</p></div>'
            for s in ("cinematic", "viral", "clean", "pro"))
        steps = "".join(f'<div class="step"><h3>{t(lang, f"how.{i}")}</h3><p>{t(lang, f"how.{i}.d")}</p></div>'
                        for i in range(1, 5))
        a = self.app_url
        return (
            f'<div class="wrap"><div class="hero"><div><div class="kicker">{t(lang, "hero.kicker")}</div>'
            f'<h1>{t(lang, "hero.title").replace(",", ",<br>", 1) if lang == "en" else t(lang, "hero.title")}</h1>'
            f'<p class="lead">{t(lang, "hero.lead")}</p><div class="cta"><a class="btn primary" href="{a}/{lang}/signup">'
            f'{t(lang, "hero.cta")}</a><a class="btn ghost" href="#styles">{t(lang, "hero.cta2")}</a></div>'
            f'<div class="trust"><span>{t(lang, "trust.rights")}</span><span>{t(lang, "trust.real")}</span>'
            f'<span>{t(lang, "trust.private")}</span></div></div>'
            f'<div style="max-width:300px;margin:auto;width:100%">{assets.preview("pro")}</div></div></div>'
            f'<section id="styles"><div class="wrap"><h2>{t(lang, "styles.title")}</h2><p class="lead">'
            f'{t(lang, "styles.lead")}</p><div class="cards">{cards}</div></div></section>'
            f'<section id="how"><div class="wrap"><h2>{t(lang, "how.title")}</h2><div class="steps">{steps}</div></div></section>'
            f'<section><div class="wrap"><h2>{t(lang, "platforms.title")}</h2><p class="lead">{t(lang, "platforms.lead")}'
            f'</p></div></section>')

    def plans_list(self) -> list[dict]:
        if self.static_mode:
            return DEFAULT_PLANS
        try:
            with self.connect() as conn:
                return clients.plans(conn) or DEFAULT_PLANS
        except Exception:
            return DEFAULT_PLANS

    def plan_cards(self, lang: str, plans: list[dict], *, user: dict | None = None, current: str | None = None) -> str:
        out = []
        for p in plans:
            price = (t(lang, "pricing.free") if p["monthly_price_cents"] == 0 else
                     f'${p["monthly_price_cents"] // 100}<small> {t(lang, "pricing.month")}</small>')
            f = p.get("features") or {}
            feats = [f'{p["clips_per_month"]} {t(lang, "pricing.clips")}',
                     f'{t(lang, "pricing.styles")}: ' + ", ".join(t(lang, "style." + s) for s in p["styles"])]
            if p.get("live_feeds"):
                feats.append(f'{p["live_feeds"]} {t(lang, "pricing.feeds")}')
            feats.append(f'{t(lang, "feat.resolution")}: {f.get("resolution", "720p")}')
            for k in ("captions", "branding", "live_clipping", "publishing", "analytics"):
                if f.get(k):
                    feats.append(t(lang, "feat." + k))
            if f.get("watermark"):
                feats.append(t(lang, "feat.watermark"))
            if user:
                if p["key"] == current:
                    action = f'<span class="pill active">{t(lang, "plan.current")}</span>'
                else:
                    action = (f'<form method=post action="/{lang}/dashboard/plan">{self.csrf_field(user)}'
                              f'<input type=hidden name=plan value="{e(p["key"])}"><button class="btn small primary">'
                              f'{t(lang, "plan.request")}</button></form>')
            else:
                action = f'<a class="btn small primary" href="{self.app_url}/{lang}/signup?plan={e(p["key"])}">{t(lang, "pricing.choose")}</a>'
            out.append(f'<div class="plan{" hl" if p["key"] == "pro" else ""}"><h3 style="margin:0;font-size:26px">'
                       f'{e(p["name"])}</h3><div class="price">{price}</div><ul>'
                       + "".join(f"<li>{e(x)}</li>" for x in feats) + f"</ul><div style='margin-top:auto'>{action}</div></div>")
        return '<div class="plans">' + "".join(out) + "</div>"

    def pricing(self, lang: str) -> str:
        return (f'<section><div class="wrap"><h1 style="font-size:48px;margin:0">{t(lang, "pricing.title")}</h1>'
                f'<p class="lead">{t(lang, "pricing.lead")}</p>{self.plan_cards(lang, self.plans_list())}'
                f'<p class="hint" style="margin-top:18px">{t(lang, "plan.nopay")}</p></div></section>')

    def legal(self, lang: str, name: str) -> str:
        path = LEGAL_DIR / ("privacy-policy.md" if name == "privacy" else "terms-of-service.md")
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        fills = {"{{BRAND}}": "VicePulse", "{{WEBSITE}}": os.environ.get("VICEPULSE_WEBSITE", "this website")}
        for k, v in fills.items():
            text = text.replace(k, v)
        return (f'<section><div class="wrap legal"><div class="note">{t(lang, "legal.draft")}</div>'
                f'{markdown(text)}</div></section>')

    # ------------------------------------------------------------------ auth pages
    def signup_page(self, lang: str, msg: str = "", plan: str = "") -> str:
        err = f'<div class="msg err">{e(msg)}</div>' if msg else ""
        return (f'<div class="auth"><h1>{t(lang, "auth.signup")}</h1>{err}<form method=post class=stack '
                f'action="/{lang}/signup"><input type=hidden name=plan value="{e(plan)}">'
                f'<label>{t(lang, "auth.name")}<input name=name required maxlength=60 autocomplete=nickname></label>'
                f'<label>{t(lang, "auth.email")}<input name=email type=email required autocomplete=email></label>'
                f'<label>{t(lang, "auth.password")}<input name=password type=password required minlength=10 '
                f'autocomplete=new-password><span class=hint>{t(lang, "auth.pw_hint")}</span></label>'
                f'<label>{t(lang, "auth.language")}<select name=language><option value=en{" selected" if lang == "en" else ""}>'
                f'English</option><option value=pt-BR{" selected" if lang == "pt" else ""}>Português (Brasil)</option>'
                f'</select></label><label class=inline><input type=checkbox name=accept value=1 required>'
                f'<span>{t(lang, "auth.accept")} (<a href="/{lang}/terms">{t(lang, "footer.terms")}</a>, '
                f'<a href="/{lang}/privacy">{t(lang, "footer.privacy")}</a>)</span></label>'
                f'<button class="btn primary">{t(lang, "auth.signup")}</button></form>'
                f'<p class=hint>{t(lang, "auth.have")} <a href="/{lang}/login">{t(lang, "auth.login")}</a></p></div>')

    def login_page(self, lang: str, msg: str = "") -> str:
        err = f'<div class="msg err">{e(msg)}</div>' if msg else ""
        return (f'<div class="auth"><h1>{t(lang, "auth.login")}</h1>{err}<form method=post class=stack '
                f'action="/{lang}/login"><label>{t(lang, "auth.email")}<input name=email type=email required '
                f'autocomplete=email></label><label>{t(lang, "auth.password")}<input name=password type=password '
                f'required autocomplete=current-password></label><button class="btn primary">{t(lang, "auth.login")}'
                f'</button></form><p class=hint>{t(lang, "auth.new")} <a href="/{lang}/signup">{t(lang, "auth.signup")}'
                f'</a></p></div>')

    # ------------------------------------------------------------------ dashboard
    TABS = (("", "dash.overview"), ("clips", "dash.clips"), ("branding", "dash.branding"),
            ("editing", "dash.prefs"), ("permissions", "dash.perms"), ("analytics", "dash.analytics"),
            ("plan", "dash.plan"), ("account", "dash.account"))

    def dash(self, lang: str, user: dict, tab: str, inner: str, msg: str = "", err: bool = False) -> str:
        tabs = list(self.TABS) + ([("staff", "dash.staff")] if user.get("is_staff") else [])
        side = "".join(f'<a href="/{lang}/dashboard{"/" + k if k else ""}" class="{"on" if k == tab else ""}">'
                       f'{t(lang, label)}</a>' for k, label in tabs)
        note = f'<div class="msg{" err" if err else ""}">{e(msg)}</div>' if msg else ""
        return f'<div class="wrap dash"><nav class="side">{side}</nav><div>{note}{inner}</div></div>'

    def overview(self, conn, lang: str, user: dict) -> str:
        c = clients.get_client(conn, user, user["client_id"])
        u = clients.usage(conn, c["id"])
        perms = clients.permissions(conn, user, c["id"])
        recent = clients.deliveries(conn, user, c["id"], limit=4)
        b, p = c["branding"] or {}, c["preferences"] or {}
        pct = 0 if not u["clips_included"] else min(100, round(100 * u["clips_used"] / u["clips_included"]))
        checklist = [(bool(perms), "check.perm"), (bool(b.get("logo") or b.get("theme")), "check.brand"),
                     (bool(p.get("style")) and p.get("style") != "clean" or bool(p.get("style_chosen")), "check.style"),
                     (any(x["status"] == "verified" for x in perms), "check.verified")]
        status = t(lang, f"dash.status.{c['status']}") if c["status"] in ("pending", "active", "suspended") else ""
        return (f'<div class="panel"><h2>{t(lang, "dash.welcome")}, {e(c["display_name"])}</h2><p>{status}</p>'
                f'<div class="kpis"><div class="kpi"><span>{t(lang, "dash.usage")}</span><b>{u["clips_used"]}/'
                f'{u["clips_included"]}</b><div class="bar"><i style="width:{pct}%"></i></div></div>'
                f'<div class="kpi"><span>{t(lang, "plan.current")}</span><b>{e(u["plan_name"])}</b></div>'
                f'<div class="kpi"><span>{t(lang, "prefs.style")}</span><b>{e(t(lang, "style." + p.get("style", "clean")))}</b>'
                f'</div></div></div><div class="panel"><h2>{t(lang, "dash.checklist")}</h2><ul class="check">'
                + "".join(f'<li class="{"done" if ok else ""}">{t(lang, k)}</li>' for ok, k in checklist)
                + f'</ul></div><div class="panel"><h2>{t(lang, "dash.recent")}</h2>{self.clip_grid(lang, recent)}</div>')

    def clip_grid(self, lang: str, rows: list[dict]) -> str:
        if not rows:
            return f'<p class="hint">{t(lang, "dash.none")}</p>'
        return '<div class="clipgrid">' + "".join(
            f'<a class="clip" href="/{lang}/dashboard/clips/{r["id"]}"><video preload="metadata" muted '
            f'src="/media/delivery/{r["id"]}#t=0.5"></video><div style="margin-top:8px;font-weight:700">{e(r["title"])}</div>'
            f'<span class="pill {e(r["status"])}">{t(lang, "dstatus." + r["status"])}</span></a>' for r in rows) + "</div>"

    def clip_page(self, conn, lang: str, user: dict, did: int) -> str:
        d = clients.delivery(conn, user, did)
        actions = ""
        if clients.can(user, "review_delivery", d["client_id"]) and d["status"] in ("ready", "changes_requested", "approved"):
            actions = (f'<form method=post action="/{lang}/dashboard/clips/{did}" class="stack">{self.csrf_field(user)}'
                       f'<label>{t(lang, "clips.note")}<textarea name=note rows=3 maxlength=1000>{e(d["client_note"])}'
                       f'</textarea></label><div class=row><button class="btn ok" name=decision value=approve>'
                       f'{t(lang, "clips.approve")}</button><button class="btn ghost" name=decision value=changes>'
                       f'{t(lang, "clips.changes")}</button></div></form>')
        meta = " · ".join(x for x in (t(lang, "style." + d["style"]) if d["style"] else "",
                                      f'{d["duration_s"]:.1f} s' if d["duration_s"] else "",
                                      d["created_at"].strftime("%Y-%m-%d")) if x)
        return (f'<div class="panel"><a href="/{lang}/dashboard/clips">← {t(lang, "back")}</a><h2>{e(d["title"])}</h2>'
                f'<p class="hint">{e(meta)} <span class="pill {e(d["status"])}">{t(lang, "dstatus." + d["status"])}</span></p>'
                f'<div class="row" style="align-items:flex-start;gap:26px"><div class="player"><video controls playsinline '
                f'preload="metadata" src="/media/delivery/{did}"></video><p><a class="btn small ghost" '
                f'href="/media/delivery/{did}?download=1">{t(lang, "clips.download")}</a></p></div>'
                f'<div style="flex:1;min-width:260px">{actions}</div></div></div>')

    def branding_page(self, lang: str, user: dict, c: dict) -> str:
        b = c["branding"] or {}
        th = {"primary": "#FF2D95", "secondary": "#8B2CF5", "accent": "#FF8A3D", "dark": "#0D0714", **(b.get("theme") or {})}
        fr = {"style": "gradient", "radius": 28, "thickness": 8, **(b.get("frame") or {})}
        logo = (f'<img class="logo-prev" src="/media/logo/{c["id"]}" alt="">' if b.get("logo") else "")
        sw = "".join(f'<label>{t(lang, "brand." + k)}<input type=color name={k} value="{e(th[k])}"></label>'
                     for k in ("primary", "secondary", "accent", "dark"))
        frames = "".join(f'<option value={k}{" selected" if fr["style"] == k else ""}>{t(lang, "frame." + k)}</option>'
                         for k in clients.FRAME_STYLES)
        mono = b.get("monogram") or "".join(w[0] for w in (b.get("creator_name") or c["display_name"]).split()[:2]).upper()
        return (f'<div class="panel"><h2>{t(lang, "dash.branding")}</h2><div class="row" style="align-items:flex-start;gap:30px">'
                f'<form method=post class=stack action="/{lang}/dashboard/branding">{self.csrf_field(user)}'
                f'<label>{t(lang, "brand.name")}<input name=creator_name maxlength=40 value="{e(b.get("creator_name"))}"></label>'
                f'<label>{t(lang, "brand.handle")}<input name=handle maxlength=31 value="{e(b.get("handle"))}"></label>'
                f'<label>{t(lang, "brand.monogram")}<input name=monogram maxlength=3 value="{e(b.get("monogram"))}"></label>'
                f'<div><b>{t(lang, "brand.colours")}</b><div class="swatches">{sw}</div></div>'
                f'<label>{t(lang, "brand.frame")}<select name=frame_style>{frames}</select></label>'
                f'<div class=row><label>{t(lang, "brand.radius")}<input type=number name=frame_radius min=0 max=80 '
                f'value="{e(fr["radius"])}"></label><label>{t(lang, "brand.thickness")}<input type=number '
                f'name=frame_thickness min=2 max=20 value="{e(fr["thickness"])}"></label></div>'
                f'<label>{t(lang, "brand.outro")}<input name=outro_text maxlength=40 value="{e(b.get("outro_text"))}"></label>'
                f'<button class="btn primary">{t(lang, "save")}</button></form>'
                f'<div style="width:240px">{assets.preview("pro", monogram=e(mono[:3] or "VP"))}</div></div></div>'
                f'<div class="panel"><h2>{t(lang, "brand.logo")}</h2>{logo}<form method=post enctype="multipart/form-data" '
                f'class=stack action="/{lang}/dashboard/logo">{self.csrf_field(user)}<input type=file name=logo '
                f'accept="image/png,image/jpeg,image/webp" required><span class=hint>{t(lang, "brand.logo_hint")}</span>'
                f'<button class="btn primary">{t(lang, "brand.upload")}</button></form></div>')

    def editing_page(self, conn, lang: str, user: dict, c: dict) -> str:
        p = c["preferences"] or {}
        plan = clients.plan_of(conn, c["id"])
        styles = "".join(
            f'<option value={s}{" selected" if p.get("style") == s else ""}{"" if s in plan["styles"] else " disabled"}>'
            f'{t(lang, "style." + s)}{"" if s in plan["styles"] else " (" + t(lang, "prefs.locked") + ")"}</option>'
            for s in clients.STYLES)
        layouts = "".join(f'<option value={k}{" selected" if p.get("layout") == k else ""}>{t(lang, "layout." + k)}</option>'
                          for k in clients.LAYOUTS)
        caplang = "".join(f'<option value={k}{" selected" if p.get("caption_language", "auto") == k else ""}>{k}</option>'
                          for k in ("auto", "en", "pt-BR"))
        plats = "".join(f'<label class=inline><input type=checkbox name=platforms value={k}'
                        f'{" checked" if k in (p.get("platforms") or []) else ""}>{k.title()}</label>'
                        for k in ("youtube", "instagram", "tiktok", "facebook"))
        previews = "".join(f'<div style="width:120px">{assets.preview(s)}<div class=hint>{t(lang, "style." + s)}</div></div>'
                           for s in plan["styles"])
        return (f'<div class="panel"><h2>{t(lang, "dash.prefs")}</h2><div class=row style="margin-bottom:18px">{previews}</div>'
                f'<form method=post class=stack action="/{lang}/dashboard/editing">{self.csrf_field(user)}'
                f'<input type=hidden name=platforms_sent value=1>'
                f'<label>{t(lang, "prefs.style")}<select name=style>{styles}</select></label>'
                f'<label>{t(lang, "prefs.layout")}<select name=layout>{layouts}</select></label>'
                f'<label class=inline><input type=checkbox name=captions value=1{" checked" if p.get("captions", True) else ""}>'
                f'{t(lang, "prefs.captions")}</label><label>{t(lang, "prefs.caption_language")}<select name=caption_language>'
                f'{caplang}</select></label><div><b>{t(lang, "prefs.platforms")}</b><div class=row>{plats}</div></div>'
                f'<label>{t(lang, "prefs.facecam")}<input name=facecam value="{e(p.get("facecam", "auto"))}">'
                f'<span class=hint>{t(lang, "prefs.facecam_hint")}</span></label>'
                f'<button class="btn primary">{t(lang, "save")}</button></form></div>')

    def permissions_page(self, conn, lang: str, user: dict, c: dict) -> str:
        rows = clients.permissions(conn, user, c["id"])
        can_manage = clients.can(user, "manage_permissions", c["id"])
        trs = "".join(
            f'<tr><td>{t(lang, "scope." + r["scope"])}</td><td>{e(", ".join(r["platforms"]) or "-")}</td>'
            f'<td><span class="pill {e(r["status"])}">{t(lang, "pstatus." + r["status"])}</span></td><td>'
            + (f'<form method=post action="/{lang}/dashboard/permissions/revoke">{self.csrf_field(user)}'
               f'<input type=hidden name=id value={r["id"]}><button class="btn small danger">{t(lang, "perm.revoke")}'
               f'</button></form>' if can_manage and r["status"] in ("requested", "verified") else "")
            + "</td></tr>" for r in rows)
        scopes = "".join(f'<option value={s}>{t(lang, "scope." + s)}</option>' for s in rights.SCOPES)
        plats = "".join(f'<label class=inline><input type=checkbox name=platforms value={k}>{k.title()}</label>'
                        for k in clients.PLATFORMS)
        form = (f'<form method=post class=stack action="/{lang}/dashboard/permissions">{self.csrf_field(user)}'
                f'<label>{t(lang, "perm.scope")}<select name=scope>{scopes}</select></label>'
                f'<div><b>{t(lang, "perm.platforms")}</b><div class=row>{plats}</div></div>'
                f'<label>{t(lang, "perm.evidence")}<input name=evidence_url type=url placeholder="https://"></label>'
                f'<label>{t(lang, "perm.note")}<textarea name=note rows=3 maxlength=500></textarea></label>'
                f'<button class="btn primary">{t(lang, "perm.add")}</button></form>') if can_manage else ""
        table = (f'<table><tr><th>{t(lang, "perm.scope")}</th><th>{t(lang, "perm.platforms")}</th>'
                 f'<th>{t(lang, "perm.status")}</th><th></th></tr>{trs}</table>') if rows else ""
        return (f'<div class="panel"><h2>{t(lang, "perm.title")}</h2><p class=hint>{t(lang, "perm.lead")}</p>{table}</div>'
                f'<div class="panel">{form}</div>')

    def analytics_page(self, conn, lang: str, user: dict, c: dict) -> str:
        a = clients.analytics(conn, user, c["id"])
        top = max(a["per_day"].values(), default=1)
        days = "".join(f'<div title="{d}: {n}" style="flex:1;background:var(--grad);height:{max(4, round(80 * n / top))}px;'
                       f'border-radius:4px 4px 0 0"></div>' for d, n in a["per_day"].items())
        styles = "".join(f'<tr><td>{t(lang, "style." + s) if s != "classic" else "Classic"}</td><td>{n}</td></tr>'
                         for s, n in sorted(a["by_style"].items(), key=lambda x: -x[1]))
        rate = f'{round(100 * a["approval_rate"])}%' if a["approval_rate"] is not None else "–"
        return (f'<div class="panel"><h2>{t(lang, "an.title")}</h2><div class="kpis"><div class="kpi"><span>'
                f'{t(lang, "an.clips")}</span><b>{a["clips"]}</b></div><div class="kpi"><span>{t(lang, "an.approval")}'
                f'</span><b>{rate}</b></div><div class="kpi"><span>{t(lang, "an.views")}</span><b>'
                f'{a["views"] if a["views"] is not None else "–"}</b></div></div></div>'
                f'<div class="panel"><h3>{t(lang, "an.per_day")}</h3><div style="display:flex;gap:4px;align-items:flex-end;'
                f'height:90px">{days or "<span class=hint>–</span>"}</div></div>'
                f'<div class="panel"><h3>{t(lang, "an.by_style")}</h3><table>{styles}</table></div>')

    def staff_page(self, conn, lang: str, user: dict) -> str:
        reqs = clients.open_permission_requests(conn, user)
        rows = "".join(
            f'<tr><td>{e(r["display_name"])}</td><td>{t(lang, "scope." + r["scope"])}<br><span class=hint>'
            f'{e(", ".join(r["platforms"]))}</span></td><td>{("<a href=" + chr(34) + e(r["evidence_url"]) + chr(34) + " rel=noopener>link</a>") if r["evidence_url"] else "-"}'
            f'<br><span class=hint>{e(r["note"])}</span></td><td><form method=post action="/{lang}/dashboard/staff" class=stack>'
            f'{self.csrf_field(user)}<input type=hidden name=id value={r["id"]}><input name=document_url type=url '
            f'placeholder="{t(lang, "staff.doc")}"><div class=row><button class="btn small ok" name=decision value=verify>'
            f'{t(lang, "staff.verify")}</button><button class="btn small danger" name=decision value=reject>'
            f'{t(lang, "staff.reject")}</button></div></form></td></tr>' for r in reqs)
        return (f'<div class="panel"><h2>{t(lang, "staff.requests")}</h2>'
                + (f"<table>{rows}</table>" if rows else '<p class=hint>–</p>') + "</div>")

    # ------------------------------------------------------------------ request handling
    def handle(self, req: Request) -> Response:
        try:
            resp = self._route(req)
        except clients.AccessDenied:
            resp = Response(403, b"forbidden", {"Content-Type": "text/plain"})
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if resp.headers.get("Content-Type", "").startswith("text/html"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def _cookie(self, value: str, max_age: int) -> str:
        return (f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
                + ("; Secure" if self.secure else ""))

    def _lang_of(self, req: Request) -> str:
        al = req.headers.get("accept-language", "").lower()
        return "pt" if al.startswith("pt") else "en"

    def _route(self, req: Request) -> Response:
        path = req.path.rstrip("/") or "/"
        if path == "/":
            return Response.redirect(f"/{self._lang_of(req)}/")
        if path == "/healthz":
            return Response(200, b"ok", {"Content-Type": "text/plain"})
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):])
        m = re.match(r"^/media/(delivery|logo)/(\d+)$", path)
        if m:
            return self.media(req, m.group(1), int(m.group(2)))
        m = re.match(r"^/(en|pt)(/.*)?$", path)
        if not m:
            return self.not_found("en")
        lang, rest = m.group(1), (m.group(2) or "/")
        if req.method == "POST":
            return self.post(req, lang, rest)
        if req.method not in ("GET", "HEAD"):
            return Response(405, b"method not allowed", {"Content-Type": "text/plain"})
        user = self.current_user(req)
        q = {k: v[0] if isinstance(v, list) else v for k, v in req.query.items()}
        if rest in ("/", ""):
            return Response.html(self.layout(lang, "VicePulse", self.home(lang), user=user, path=req.path))
        if rest == "/pricing":
            return Response.html(self.layout(lang, t(lang, "pricing.title"), self.pricing(lang), user=user, path=req.path))
        if rest in ("/privacy", "/terms"):
            title = t(lang, "footer.privacy" if rest == "/privacy" else "footer.terms")
            return Response.html(self.layout(lang, title, self.legal(lang, rest[1:]), user=user, path=req.path))
        if rest == "/signup":
            if user:
                return Response.redirect(f"/{lang}/dashboard")
            return Response.html(self.layout(lang, t(lang, "auth.signup"), self.signup_page(lang, plan=q.get("plan", "")),
                                             path=req.path))
        if rest == "/login":
            if user:
                return Response.redirect(f"/{lang}/dashboard")
            return Response.html(self.layout(lang, t(lang, "auth.login"), self.login_page(lang), path=req.path))
        if rest.startswith("/dashboard"):
            if not user:
                return Response.redirect(f"/{lang}/login")
            return self.dashboard_get(req, lang, rest, user, q)
        return self.not_found(lang)

    def not_found(self, lang: str) -> Response:
        return Response.html(self.layout(lang, t(lang, "notfound"), f'<div class="wrap"><h1>{t(lang, "notfound")}</h1></div>'),
                             404)

    def current_user(self, req: Request) -> dict | None:
        token = req.cookies.get(COOKIE)
        if not token:
            return None
        with self.connect() as conn:
            return clients.session_user(conn, token)

    def dashboard_get(self, req: Request, lang: str, rest: str, user: dict, q: dict) -> Response:
        tab = rest[len("/dashboard"):].strip("/")
        msg, err = q.get("msg", ""), q.get("err") == "1"
        with self.connect() as conn:
            c = clients.get_client(conn, user, user["client_id"])
            m = re.match(r"^clips/(\d+)$", tab)
            if m:
                inner, tab = self.clip_page(conn, lang, user, int(m.group(1))), "clips"
            elif tab == "":
                inner = self.overview(conn, lang, user)
            elif tab == "clips":
                inner = (f'<div class="panel"><h2>{t(lang, "clips.title")}</h2>'
                         f'{self.clip_grid(lang, clients.deliveries(conn, user, c["id"]))}</div>')
            elif tab == "branding":
                inner = self.branding_page(lang, user, c)
            elif tab == "editing":
                inner = self.editing_page(conn, lang, user, c)
            elif tab == "permissions":
                inner = self.permissions_page(conn, lang, user, c)
            elif tab == "analytics":
                inner = self.analytics_page(conn, lang, user, c)
            elif tab == "plan":
                inner = (f'<div class="panel"><h2>{t(lang, "dash.plan")}</h2>'
                         f'{self.plan_cards(lang, clients.plans(conn), user=user, current=c["plan_key"])}'
                         f'<p class=hint style="margin-top:14px">{t(lang, "plan.nopay")}</p></div>')
            elif tab == "account":
                inner = (f'<div class="panel"><h2>{t(lang, "acct.change_pw")}</h2><p class=hint>{e(user["email"])}</p>'
                         f'<form method=post class=stack action="/{lang}/dashboard/account">{self.csrf_field(user)}'
                         f'<label>{t(lang, "acct.old")}<input type=password name=old required autocomplete=current-password>'
                         f'</label><label>{t(lang, "acct.new")}<input type=password name=new required minlength=10 '
                         f'autocomplete=new-password></label><button class="btn primary">{t(lang, "save")}</button></form></div>')
            elif tab == "staff" and user.get("is_staff"):
                inner = self.staff_page(conn, lang, user)
            else:
                return self.not_found(lang)
        return Response.html(self.layout(lang, t(lang, "dash.title"), self.dash(lang, user, tab, inner, msg, err),
                                         user=user, path=req.path))

    def post(self, req: Request, lang: str, rest: str) -> Response:
        # Same-origin forms only: browsers send Origin on POST; a different host is refused outright.
        origin = req.headers.get("origin")
        if origin and urlparse(origin).netloc != req.headers.get("host", ""):
            return Response(403, b"cross-site request refused", {"Content-Type": "text/plain"})
        if len(req.body) > MAX_BODY:
            return Response(413, b"too large", {"Content-Type": "text/plain"})
        form = req.form()
        if rest == "/signup":
            return self.do_signup(lang, form)
        if rest == "/login":
            return self.do_login(lang, form)
        user = self.current_user(req)
        if not user:
            return Response.redirect(f"/{lang}/login")
        if not isinstance(form.get("csrf"), str) or not clients.hmac.compare_digest(form.get("csrf", ""), user["csrf"]):
            return Response(403, b"form expired, reload the page", {"Content-Type": "text/plain"})
        if rest == "/logout":
            with self.connect() as conn:
                clients.end_session(conn, req.cookies.get(COOKIE))
            return Response.redirect(f"/{lang}/", **{"Set-Cookie": self._cookie("", 0)})
        back = f"/{lang}{rest}"
        try:
            with self.connect() as conn:
                done = self.dashboard_post(conn, lang, rest, user, form)
        except clients.AccessDenied:
            raise
        except (ValueError, clients.PaymentsDisabled) as ex:
            target = back if not rest.endswith("/revoke") and rest != "/dashboard/logo" else back.rsplit("/", 1)[0]
            if rest == "/dashboard/logo":
                target = f"/{lang}/dashboard/branding"
            return Response.redirect(f"{target}?err=1&msg={quote(str(ex))}")
        where, msg = done
        return Response.redirect(f"{where}?msg={quote(msg)}")

    def do_signup(self, lang: str, form: dict) -> Response:
        try:
            with self.connect() as conn:
                out = clients.register(conn, email=form.get("email", ""), password=form.get("password", ""),
                                       display_name=form.get("name", ""), language=form.get("language", "en"),
                                       accept_terms=form.get("accept") == "1")
                token, _ = clients.create_session(conn, out["user_id"])
        except ValueError as ex:
            return Response.html(self.layout(lang, t(lang, "auth.signup"), self.signup_page(lang, str(ex),
                                                                                            form.get("plan", ""))), 400)
        dash_lang = "pt" if form.get("language") == "pt-BR" else "en"
        return Response.redirect(f"/{dash_lang}/dashboard", **{"Set-Cookie": self._cookie(token, clients.SESSION_DAYS * 86400)})

    def do_login(self, lang: str, form: dict) -> Response:
        with self.connect() as conn:
            u = clients.authenticate(conn, form.get("email", ""), form.get("password", ""))
            if u is None:
                return Response.html(self.layout(lang, t(lang, "auth.login"), self.login_page(lang, t(lang, "auth.bad"))), 401)
            token, _ = clients.create_session(conn, u["id"])
        return Response.redirect(f"/{lang}/dashboard", **{"Set-Cookie": self._cookie(token, clients.SESSION_DAYS * 86400)})

    def dashboard_post(self, conn, lang: str, rest: str, user: dict, form: dict) -> tuple[str, str]:
        cid = user["client_id"]
        base = f"/{lang}/dashboard"
        saved = t(lang, "saved")
        if rest == "/dashboard/branding":
            clients.update_branding(conn, user, cid, {k: v for k, v in form.items() if isinstance(v, str)})
            return f"{base}/branding", saved
        if rest == "/dashboard/logo":
            data = form.get("logo")
            if not isinstance(data, bytes) or not data:
                raise ValueError(t(lang, "brand.logo_hint"))
            clients.save_logo(conn, user, cid, data, self.settings.data_dir)
            return f"{base}/branding", saved
        if rest == "/dashboard/editing":
            data = {k: v for k, v in form.items() if k in ("style", "layout", "caption_language", "facecam")}
            data["captions"] = form.get("captions") == "1"
            plats = form.get("platforms", [])
            data["platforms"] = plats if isinstance(plats, list) else [plats] if plats else []
            clients.update_preferences(conn, user, cid, data)
            return f"{base}/editing", saved
        if rest == "/dashboard/permissions":
            plats = form.get("platforms", [])
            clients.request_permission(conn, user, cid, scope=form.get("scope", ""),
                                       platforms=plats if isinstance(plats, list) else [plats] if plats else [],
                                       evidence_url=form.get("evidence_url", "").strip(), note=form.get("note", ""))
            return f"{base}/permissions", saved
        if rest == "/dashboard/permissions/revoke":
            clients.revoke_permission(conn, user, int(form.get("id", "0") or 0))
            return f"{base}/permissions", saved
        m = re.match(r"^/dashboard/clips/(\d+)$", rest)
        if m:
            clients.review_delivery(conn, user, int(m.group(1)), approve=form.get("decision") == "approve",
                                    note=form.get("note", ""))
            return f"{base}/clips/{m.group(1)}", saved
        if rest == "/dashboard/plan":
            clients.request_plan(conn, user, form.get("plan", ""))
            return f"{base}/plan", t(lang, "plan.requested")
        if rest == "/dashboard/account":
            clients.change_password(conn, user, form.get("old", ""), form.get("new", ""))
            return f"/{lang}/login", saved
        if rest == "/dashboard/staff":
            if not user.get("is_staff"):
                raise clients.AccessDenied("staff only")
            clients.staff_verify_permission(conn, user, int(form.get("id", "0") or 0),
                                            document_url=form.get("document_url", "").strip(),
                                            approve=form.get("decision") == "verify")
            return f"{base}/staff", saved
        raise ValueError(t(lang, "notfound"))

    # ------------------------------------------------------------------ files
    def static(self, name: str) -> Response:
        if name == "site.css":
            return Response(200, assets.CSS.encode(), {"Content-Type": "text/css; charset=utf-8",
                                                       "Cache-Control": "public, max-age=3600"})
        if name == "logo.jpg" and LOGO.is_file():
            return Response(200, LOGO.read_bytes(), {"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=86400"})
        m = re.match(r"^fonts/([A-Za-z0-9-]+\.ttf)$", name)
        if m and (FONT_DIR / m.group(1)).is_file():
            return Response(200, (FONT_DIR / m.group(1)).read_bytes(), {"Content-Type": "font/ttf",
                                                                       "Cache-Control": "public, max-age=604800"})
        return Response(404, b"not found", {"Content-Type": "text/plain"})

    def media(self, req: Request, kind: str, ident: int) -> Response:
        user = self.current_user(req)
        if not user:
            return Response(401, b"sign in first", {"Content-Type": "text/plain"})
        with self.connect() as conn:
            if kind == "delivery":
                d = clients.delivery(conn, user, ident)          # raises AccessDenied for other clients
                path = Path(d["file_path"])
                name = re.sub(r"[^A-Za-z0-9_-]+", "_", d["title"])[:60] + ".mp4"
            else:
                c = clients.get_client(conn, user, ident)
                path = Path((c["branding"] or {}).get("logo") or "")
                name = "logo.png"
        if not path.is_file():
            return Response(404, b"file not found", {"Content-Type": "text/plain"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        headers = {"Content-Type": ctype, "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"}
        if (req.query.get("download") or [""])[0] == "1" if isinstance(req.query.get("download"), list) else req.query.get("download") == "1":
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        rng = re.match(r"^bytes=(\d*)-(\d*)$", req.headers.get("range", ""))
        with open(path, "rb") as f:
            if rng and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = min(size - 1, int(rng.group(2))) if rng.group(2) else size - 1
                else:
                    start, end = max(0, size - int(rng.group(2))), size - 1
                if start >= size or start > end:
                    return Response(416, b"", {"Content-Range": f"bytes */{size}"})
                f.seek(start)
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                return Response(206, f.read(end - start + 1), headers)
            return Response(200, f.read(), headers)


# ---------------------------------------------------------------- tiny Markdown (legal drafts only)

def markdown(text: str) -> str:
    """Headings, paragraphs, lists, bold/italic, links and block quotes. Input is escaped first."""
    out, para, items = [], [], []

    def inline(s: str) -> str:
        s = e(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*(?!\s)(.+?)\*", r"<i>\1</i>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)", r'<a href="\2">\1</a>', s)
        return s

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        s = line.strip()
        h = re.match(r"^(#{1,4})\s+(.*)$", s)
        if h:
            flush()
            n = min(3, len(h.group(1)))
            out.append(f"<h{n}>{inline(h.group(2))}</h{n}>")
        elif re.match(r"^([-*]|\d+\.)\s+", s):
            if para:
                flush()
            items.append(re.sub(r"^([-*]|\d+\.)\s+", "", s))
        elif s.startswith(">"):
            flush()
            out.append(f'<div class="note">{inline(s.lstrip("> "))}</div>')
        elif not s:
            flush()
        elif s.startswith("|") or set(s) <= set("-|: "):
            flush()
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not set(s) <= set("-|: "):
                out.append("<table><tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr></table>")
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "\n".join(out)


# ---------------------------------------------------------------- static export and server

def build_static(settings: Settings, out_dir: Path, *, app_url: str = "") -> list[str]:
    """Write the public pages (home, pricing, privacy, terms in EN and PT) as plain files for a free static host.
    Sign-up and dashboard links point at app_url. Nothing is uploaded anywhere."""
    app = App(settings, connect=lambda: (_ for _ in ()).throw(RuntimeError("no database in static export")),
              app_url=app_url)
    app.static_mode = True
    out_dir = Path(out_dir)
    written = []
    for lang in LANGS:
        pages = {"index.html": ("VicePulse", app.home(lang), f"/{lang}/"),
                 "pricing.html": (t(lang, "pricing.title"), app.pricing(lang), f"/{lang}/pricing"),
                 "privacy.html": (t(lang, "footer.privacy"), app.legal(lang, "privacy"), f"/{lang}/privacy"),
                 "terms.html": (t(lang, "footer.terms"), app.legal(lang, "terms"), f"/{lang}/terms")}
        for name, (title, body, path) in pages.items():
            p = out_dir / lang / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(app.layout(lang, title, body, path=path), encoding="utf-8")
            written.append(str(p.relative_to(out_dir)))
    st = out_dir / "static"
    (st / "fonts").mkdir(parents=True, exist_ok=True)
    (st / "site.css").write_text(assets.CSS, encoding="utf-8")
    if LOGO.is_file():
        (st / "logo.jpg").write_bytes(LOGO.read_bytes())
    for f in FONT_DIR.glob("*.ttf"):
        (st / "fonts" / f.name).write_bytes(f.read_bytes())
    (out_dir / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=/en/index.html">'
        '<link rel="canonical" href="/en/index.html"><a href="/en/index.html">VicePulse</a>', encoding="utf-8")
    written += ["index.html", "static/site.css"]
    return written


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VicePulseWeb/1"

        def log_message(self, *args):
            pass

        def _run(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length) if length else b""
            req = Request(self.command, url.path, parse_qs(url.query), {k.lower(): v for k, v in self.headers.items()},
                          body)
            resp = app.handle(req)
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)

        do_GET = do_POST = do_HEAD = _run

    return Handler


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8091) -> None:
    app = App(settings)
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    print(json.dumps({"web": f"http://{host}:{port}", "started": datetime.now(timezone.utc).isoformat()}))
    srv.serve_forever()


__all__ = ["App", "Request", "Response", "serve", "build_static", "markdown", "DEFAULT_PLANS"]
