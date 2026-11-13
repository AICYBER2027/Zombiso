"""Local review page: watch pending clips, approve or reject them, and record creator permissions.

Runs on the owner's PC only (docker publishes it on 127.0.0.1). Every form carries a per-start secret, and
requests from other sites are refused, so a web page elsewhere cannot approve clips through the browser.
Approving never publishes: publishing stays behind VICEPULSE_ALLOW_LIVE_PUBLISH and the pre-publish gate.
"""

from __future__ import annotations

import html
import mimetypes
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import accounts, db, discovery, policy
from .config import Settings

REVIEWER = "owner (review page)"
PLATFORM_ORDER = ("youtube", "instagram", "tiktok")

CSS = """
:root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--text:#e9ecf2;--muted:#9aa3b2;--ok:#2fbf71;--no:#e5484d;--accent:#f5c518}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:18px;margin:0}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--accent)}
.safe{margin-left:auto;font-size:13px;color:var(--muted)}main{padding:20px;max-width:1300px;margin:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:8px}
video{width:100%;aspect-ratio:9/16;background:#000;border-radius:8px}
.meta{font-size:13px;color:var(--muted)}.title{font-weight:600}.tag{display:inline-block;padding:1px 8px;border-radius:99px;
border:1px solid var(--line);font-size:12px;margin-right:4px;color:var(--text);text-decoration:none}a{color:var(--accent)}details{font-size:13px;color:var(--muted)}
form.row{display:flex;gap:8px;flex-wrap:wrap}input[type=text],input[type=url]{flex:1;min-width:0;padding:8px;border-radius:8px;
border:1px solid var(--line);background:#0c0e12;color:var(--text)}button{padding:8px 14px;border:0;border-radius:8px;font-weight:600;cursor:pointer}
.approve{background:var(--ok);color:#04150b}.reject{background:var(--no);color:#fff}.plain{background:#2a2f3a;color:var(--text)}
.msg{padding:10px 14px;border-radius:8px;background:#1d2a20;border:1px solid #2f5a3d;margin-bottom:16px}
code{background:#0c0e12;border:1px solid var(--line);padding:2px 6px;border-radius:6px;word-break:break-all}
.err{background:#2a1d1d;border-color:#5a2f2f}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top;font-size:14px}th{color:var(--muted);font-weight:500}.empty{color:var(--muted);padding:40px;text-align:center}
"""


def e(v) -> str:
    return html.escape("" if v is None else str(v))


class ReviewApp:
    def __init__(self, settings: Settings, token: str | None = None, public_url: str | None = None):
        self.settings = settings
        self.token = token or secrets.token_urlsafe(24)
        # The address you open in the browser; Google sends you back here after signing in.
        self.public_url = (public_url or os.environ.get("VICEPULSE_REVIEW_URL") or "http://localhost:8090").rstrip("/")
        self.oauth_states: dict[str, str] = {}

    @property
    def youtube_redirect(self) -> str:
        return f"{self.public_url}/oauth/youtube/callback"

    # ------------------------------------------------------------------ pages
    def page(self, title: str, body: str, tab: str, msg: str = "", err: bool = False) -> str:
        s = self.settings
        tabs = "".join(f'<a href="{href}" class="{"on" if key == tab else ""}">{label}</a>'
                       for key, href, label in (("pending", "/", "Waiting for review"), ("done", "/done", "Reviewed"),
                                                ("creators", "/creators", "Creators"), ("accounts", "/accounts", "Accounts")))
        safe = (f'AI: {e(s.llm_mode)} &middot; live publishing: {"ON" if s.allow_live_publish else "off"}'
                f' &middot; approval required: {"yes" if s.require_approval else "no"}')
        note = f'<div class="msg{" err" if err else ""}">{e(msg)}</div>' if msg else ""
        return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
                f"initial-scale=1'><title>VicePulse review</title><style>{CSS}</style></head><body><header>"
                f"<h1>VicePulse</h1>{tabs}<span class=safe>{safe}</span></header><main><h2>{e(title)}</h2>{note}{body}"
                f"</main></body></html>")

    def clip_cards(self, conn, statuses: tuple[str, ...], with_actions: bool) -> str:
        rows = conn.execute(
            """SELECT cl.*, ch.key AS channel_key, ch.name AS channel_name, s.file_path AS source_file,
                      r.credit_text, r.licence_type, r.verified_by
               FROM clips cl JOIN channels ch ON ch.id = cl.channel_id
               JOIN candidates c ON c.id = cl.candidate_id JOIN sources s ON s.id = c.source_id
               LEFT JOIN rights_ledger r ON r.id = s.rights_id
               WHERE cl.status = ANY(%s) ORDER BY cl.id DESC LIMIT 60""", (list(statuses),)).fetchall()
        if not rows:
            return '<div class="empty">Nothing here right now.</div>'
        cards = []
        for c in rows:
            renders = {r["platform"]: r for r in conn.execute(
                "SELECT id, platform, duration_s FROM renders WHERE clip_id = %s", (c["id"],)).fetchall()}
            first = next((p for p in PLATFORM_ORDER if p in renders), None)
            video = (f'<video controls preload=metadata src="/media/{c["id"]}/{first}"></video>' if first
                     else '<div class="empty">no render</div>')
            yt = (c["titles"] or {}).get("youtube", {})
            script = c["script"] or {}
            caps = script.get("captions", [])
            sub_lines = "<br>".join(f'{x["start"]:.1f}s &nbsp;{e(x["text"])}' for x in caps[:80])
            plat = " ".join(f'<a class=tag href="/media/{c["id"]}/{p}" target=_blank>{p}</a>' for p in PLATFORM_ORDER
                            if p in renders)
            rights = e(c["credit_text"] or c["licence_type"] or "unknown")
            if (c["verified_by"] or "").startswith("auto:"):
                rights += " (accepted automatically - confirm before publishing)"
            actions = ""
            if with_actions:
                actions = (f'<form class=row method=post action="/decide"><input type=hidden name=csrf value="{self.token}">'
                           f'<input type=hidden name=clip value="{c["id"]}"><input type=text name=reason placeholder="note (optional)">'
                           f'<button class=approve name=decision value=approve>Approve</button>'
                           f'<button class=reject name=decision value=reject>Reject</button></form>')
            else:
                actions = f'<div class=meta>{e(c["status"])} by {e(c["approved_by"])} {e(c["status_reason"] or "")}</div>'
            cards.append(
                f'<div class=card>{video}<div class=title>{e(yt.get("title") or script.get("hook") or "")}</div>'
                f'<div class=meta><span class=tag>{e(c["channel_key"]).upper()}</span><span class=tag>clip {c["id"]}</span>'
                f'{c["out_s"] - c["in_s"]:.0f}s from {e(Path(c["source_file"]).name)} at {c["in_s"]:.0f}s</div>'
                f'<div class=meta>Subtitles: {e(script.get("caption_source", "script"))} ({len(caps)} lines) &middot; '
                f'Rights: {rights}</div><div class=meta>Files: {plat}</div>'
                f'<details><summary>Subtitles and description</summary><p>{sub_lines}</p>'
                f'<p>{e(yt.get("description", ""))}</p></details>{actions}</div>')
        return f'<div class=grid>{"".join(cards)}</div>'

    def creators_page(self, conn) -> str:
        rows = conn.execute(
            """SELECT cr.id, cr.display_name, cr.primary_language, cr.permission_status, cr.permission_summary,
                      cr.reviewed_by, r.document_url,
                      (SELECT string_agg(cc.platform || ':' || COALESCE(cc.handle, cc.channel_ref), ', ')
                         FROM creator_channels cc WHERE cc.creator_id = cr.id) AS channels,
                      (SELECT max(total) FROM creator_scores cs WHERE cs.creator_id = cr.id) AS score
               FROM creators cr LEFT JOIN rights_ledger r ON r.id = cr.rights_id
               ORDER BY (cr.permission_status = 'authorized') DESC, score DESC NULLS LAST, cr.id LIMIT 200""").fetchall()
        if not rows:
            return '<div class="empty">No creators yet. Run discovery first.</div>'
        out = ['<p class=meta>For creators who told you that you can clip them: paste a link to the proof (a screenshot '
               'in your Google Drive or Dropbox, an email, a message, or their public "clips allowed" post) and press '
               'Authorize. That records a verified permission, so their footage clears the rights check.</p>',
               '<table><tr><th>Creator</th><th>Channels</th><th>Status</th><th>Permission</th></tr>']
        for r in rows:
            if r["permission_status"] == "authorized":
                act = f'authorized by {e(r["reviewed_by"])} &middot; <a href="{e(r["document_url"])}" target=_blank>proof</a>'
            else:
                act = (f'<form class=row method=post action="/authorize"><input type=hidden name=csrf value="{self.token}">'
                       f'<input type=hidden name=creator value="{r["id"]}"><input type=url name=evidence required '
                       f'placeholder="https://link to the proof"><button class=approve>Authorize</button></form>')
            out.append(f'<tr><td><b>{e(r["display_name"])}</b><div class=meta>{e(r["primary_language"] or "")}</div></td>'
                       f'<td class=meta>{e(r["channels"] or "")}</td><td>{e(r["permission_status"])}'
                       f'<div class=meta>{e(r["permission_summary"] or "")}</div></td><td>{act}</td></tr>')
        out.append("</table>")
        return "".join(out)

    def accounts_page(self) -> str:
        st = accounts.status(self.settings)
        csrf = f'<input type=hidden name=csrf value="{self.token}">'
        out = ['<p class=meta>Connecting an account only lets VicePulse upload when publishing is switched on later. '
               'Nothing is posted now. Logins are stored in VicePulse\'s own data folder and never shown here.</p>',
               '<h3>YouTube</h3>']
        if not st["youtube_client"]:
            out.append(
                '<div class=card><b>Step 1 - Google sign-in app</b><p class=meta>In Google Cloud Console, create an '
                '<b>OAuth client ID</b> of type <b>Web application</b> and add this exact address under '
                f'<b>Authorized redirect URIs</b>:</p><p><code>{e(self.youtube_redirect)}</code></p>'
                '<p class=meta>Then paste the client ID and client secret here:</p>'
                f'<form class=row method=post action="/accounts/youtube-client">{csrf}'
                '<input type=text name=client_id placeholder="Client ID (...apps.googleusercontent.com)" required>'
                '<input type=text name=client_secret placeholder="Client secret" required autocomplete=off>'
                '<button class=approve>Save</button></form></div>')
        else:
            src = ".env" if st["youtube_client_from_env"] else "saved here"
            out.append(f'<p class=meta>Google sign-in app: ready ({src}). Redirect address: '
                       f'<code>{e(self.youtube_redirect)}</code></p>')
        rows = ['<table><tr><th>VicePulse channel</th><th>YouTube channel</th><th></th></tr>']
        for key, y in st["youtube"].items():
            who = (f'connected: <b>{e(y["channel_title"])}</b>' if y["connected"] else "not connected")
            btn = ""
            if st["youtube_client"]:
                label = "Reconnect" if y["connected"] else "Connect YouTube"
                btn = (f'<form method=post action="/accounts/youtube-start">{csrf}<input type=hidden name=channel '
                       f'value="{e(key)}"><button class={"plain" if y["connected"] else "approve"}>{label}</button></form>')
            rows.append(f'<tr><td><b>{e(y["name"])}</b> <span class=tag>{e(key).upper()}</span></td><td>{who}</td><td>{btn}</td></tr>')
        rows.append("</table>")
        out.append("".join(rows))
        out.append('<p class=meta>Sign in with the Google account that owns the right channel: the EN channel for '
                   'VicePulse EN and the BR channel for VicePulse BR. If one Google account owns both, Google asks '
                   'which channel to use.</p>')
        out.append('<h3>Instagram and TikTok</h3><p class=meta>Coming next.</p>')
        return "".join(out)

    def youtube_start(self, form: dict) -> str:
        channel = form.get("channel", "")
        if channel not in {c["key"] for c in self.settings.channels()}:
            raise ValueError("unknown channel")
        state = secrets.token_urlsafe(24)
        self.oauth_states[state] = channel
        return accounts.youtube_auth_url(self.settings.data_dir, self.youtube_redirect, state)

    def youtube_callback(self, q: dict, session=None) -> str:
        if q.get("error"):
            raise ValueError(f"Google sign-in was cancelled ({q['error']})")
        channel = self.oauth_states.pop(q.get("state", ""), None)
        if channel is None:
            raise ValueError("this sign-in link expired; press Connect again")
        info = accounts.youtube_connect(self.settings.data_dir, channel, q.get("code", ""), self.youtube_redirect,
                                        session=session)
        return f"YouTube connected for {channel.upper()}: {info['channel_title']}. Nothing was published."

    # ------------------------------------------------------------------ actions
    def decide(self, conn, form: dict) -> str:
        clip = int(form["clip"])
        approve = form.get("decision") == "approve"
        policy.decide(conn, clip, approve=approve, by=REVIEWER, reason=form.get("reason") or None)
        return f"Clip {clip} {'approved' if approve else 'rejected'}. Nothing was published."

    def authorize(self, conn, form: dict) -> str:
        cid = int(form["creator"])
        discovery.review_creator(conn, cid, decision="authorize", by=REVIEWER, evidence_url=form.get("evidence", "").strip(),
                                 monetisation_allowed=True, edits_allowed=True,
                                 notes="Authorized on the review page: the owner has the creator's permission to clip, edit and monetise.")
        return f"Creator {cid} authorized. Their footage now clears the rights check."


def make_handler(app: ReviewApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VicePulseReview/1"

        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _redirect(self, where: str):
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            msg, err = (q.get("msg") or [""])[0], (q.get("err") or [""])[0] == "1"
            if url.path.startswith("/media/"):
                return self._media(url.path)
            if url.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if url.path == "/oauth/youtube/callback":
                from urllib.parse import quote
                try:
                    done = app.youtube_callback({k: v[0] for k, v in q.items()}, session=getattr(app, "http", None))
                    return self._redirect(f"/accounts?msg={quote(done)}")
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            pages = {"/": ("Waiting for review", "pending", ("pending_approval",), True),
                     "/done": ("Reviewed", "done", ("approved", "rejected", "scheduled", "published"), False)}
            with db.connect(app.settings) as conn:
                if url.path in pages:
                    title, tab, statuses, act = pages[url.path]
                    body = app.page(title, app.clip_cards(conn, statuses, act), tab, msg, err)
                elif url.path == "/creators":
                    body = app.page("Creators", app.creators_page(conn), "creators", msg, err)
                elif url.path == "/accounts":
                    body = app.page("Accounts", app.accounts_page(), "accounts", msg, err)
                else:
                    return self._send(404, b"not found", "text/plain")
            self._send(200, body.encode())

        do_HEAD = do_GET

        def do_POST(self):
            # Only forms from this page: same-origin browsers send Origin; the secret stops cross-site posts.
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self._send(403, b"cross-site request refused", "text/plain")
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            if not secrets.compare_digest(form.get("csrf", ""), app.token):
                return self._send(403, b"form expired - reload the page", "text/plain")
            path = urlparse(self.path).path
            from urllib.parse import quote
            if path == "/accounts/youtube-client":
                try:
                    accounts.save_youtube_client(app.settings.data_dir, form.get("client_id", ""),
                                                 form.get("client_secret", ""))
                    return self._redirect("/accounts?msg=" + quote("Google sign-in app saved. Now press Connect YouTube."))
                except ValueError as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if path == "/accounts/youtube-start":
                try:
                    return self._redirect(app.youtube_start(form))
                except ValueError as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            back = {"/decide": "/", "/authorize": "/creators"}.get(path)
            if back is None:
                return self._send(404, b"not found", "text/plain")
            try:
                with db.connect(app.settings) as conn:
                    msg = app.decide(conn, form) if back == "/" else app.authorize(conn, form)
                self._redirect(f"{back}?msg={quote(msg)}")
            except (ValueError, KeyError) as ex:
                self._redirect(f"{back}?err=1&msg={quote(str(ex))}")

        def _media(self, path: str):
            parts = path.strip("/").split("/")
            if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in PLATFORM_ORDER:
                return self._send(404, b"not found", "text/plain")
            with db.connect(app.settings) as conn:
                r = conn.execute("SELECT file_path FROM renders WHERE clip_id = %s AND platform = %s",
                                 (int(parts[1]), parts[2])).fetchone()
            if not r or not os.path.isfile(r["file_path"]):
                return self._send(404, b"not found", "text/plain")
            # Only files under the data folder are served.
            fpath = Path(r["file_path"]).resolve()
            if app.settings.data_dir.resolve() not in fpath.parents:
                return self._send(403, b"refused", "text/plain")
            size = fpath.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].partition("-")
                if a.strip():
                    start = int(a)
                    end = min(int(b), size - 1) if b.strip() else size - 1
                elif b.strip():
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            length = end - start + 1
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", mimetypes.guess_type(str(fpath))[0] or "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(fpath, "rb") as f:
                f.seek(start)
                left = length
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    return Handler


def make_server(settings: Settings, host: str = "0.0.0.0", port: int = 8090,
                app: ReviewApp | None = None) -> ThreadingHTTPServer:
    app = app or ReviewApp(settings)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.app = app
    return httpd


def serve(settings: Settings, host: str = "0.0.0.0", port: int = 8090) -> None:
    httpd = make_server(settings, host, port)
    print(f'{{"review_page": "http://localhost:{port}"}}', flush=True)
    httpd.serve_forever()
