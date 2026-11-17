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

    @property
    def instagram_redirect(self) -> str:
        # Meta may insist on https; then set VICEPULSE_INSTAGRAM_REDIRECT=https://localhost:8090/oauth/instagram/callback
        # and paste the address from the browser after signing in.
        return (os.environ.get("VICEPULSE_INSTAGRAM_REDIRECT") or f"{self.public_url}/oauth/instagram/callback").rstrip("/")

    @property
    def tiktok_redirect(self) -> str:
        # TikTok only accepts https redirect addresses. This local page is plain http, so the browser shows an
        # error page after TikTok sends you back; you paste that address (it carries the code) into the page.
        # VICEPULSE_TIKTOK_REDIRECT overrides it (for example a page on your own website).
        default = self.public_url.replace("http://", "https://", 1) + "/oauth/tiktok/callback"
        return (os.environ.get("VICEPULSE_TIKTOK_REDIRECT") or default).rstrip("/")

    @property
    def tiktok_test_posts(self) -> bool:
        """Private (SELF_ONLY) test uploads from the TikTok screen; off unless switched on in .env."""
        return os.environ.get("VICEPULSE_TIKTOK_TEST_POSTS", "0") == "1"

    # ------------------------------------------------------------------ pages
    def page(self, title: str, body: str, tab: str, msg: str = "", err: bool = False) -> str:
        s = self.settings
        tabs = "".join(f'<a href="{href}" class="{"on" if key == tab else ""}">{label}</a>'
                       for key, href, label in (("pending", "/", "Waiting for review"), ("done", "/done", "Reviewed"),
                                                ("live", "/live", "Live"), ("creators", "/creators", "Creators"),
                                                ("accounts", "/accounts", "Accounts")))
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
                if c["status"] in ("approved", "scheduled", "published") and first:
                    actions += f'<a class=tag href="/tiktok/post/{c["id"]}">Post to TikTok (private test)</a>'
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
        out.append(self.instagram_section(st, csrf))
        out.append(self.tiktok_section(st, csrf))
        out.append(self.twitch_section(csrf))
        return "".join(out)

    @property
    def twitch_redirect(self) -> str:
        return f"{self.public_url}/oauth/twitch/callback"

    def twitch_section(self, csrf: str) -> str:
        info = accounts.load(self.settings.data_dir, "twitch_user")
        cid, _ = accounts.twitch_app()
        out = ['<h3>Twitch (live clips)</h3><p class=meta>VicePulse\'s own Twitch account makes clips of live streams '
               'through Twitch\'s official API. A streamer allows it by adding this account as an <b>editor</b> of '
               'their channel. In the Twitch developer console, add this OAuth Redirect URL to the VicePulse app: '
               f'<code>{e(self.twitch_redirect)}</code></p>']
        if info.get("access_token"):
            out.append(f'<p>connected: <b>{e(info.get("login"))}</b> &#10003; (permissions: {e(", ".join(info.get("scope") or []))})</p>')
        if cid:
            out.append(f'<form method=post action="/accounts/twitch-start">{csrf}<button class='
                       f'{"plain" if info.get("access_token") else "approve"}>'
                       f'{"Reconnect" if info.get("access_token") else "Connect VicePulse Twitch account"}</button></form>')
        else:
            out.append('<p class=meta>Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env first.</p>')
        return "".join(out)

    def twitch_start(self) -> str:
        state = secrets.token_urlsafe(24)
        self.oauth_states[state] = "twitch:vicepulse"
        return accounts.twitch_auth_url(self.twitch_redirect, state)

    def twitch_callback(self, q: dict, session=None) -> str:
        if q.get("error"):
            raise ValueError(f"Twitch sign-in was cancelled ({q.get('error_description') or q['error']})")
        if self.oauth_states.pop(q.get("state", ""), "") != "twitch:vicepulse":
            raise ValueError("this sign-in link expired; press Connect again")
        info = accounts.twitch_connect(self.settings.data_dir, q.get("code", ""), self.twitch_redirect, session=session)
        return f"Twitch connected: {info['login']} ({', '.join(info['scope'])}). Nothing was posted."

    def live_page(self, conn) -> str:
        from . import live

        st = live.status(conn)
        label = {"spike": "SPIKE DETECTED", "capturing": "CAPTURING", "downloading": "CAPTURING",
                 "editing": "EDITING", "ready": "READY FOR REVIEW", "failed": "FAILED", "skipped": "SKIPPED",
                 "merged": "MERGED"}
        colour = {"ready": "var(--ok)", "failed": "var(--no)", "skipped": "var(--muted)"}
        out = ['<script>setTimeout(function(){location.reload()},5000)</script>',
               '<p class=meta>Updates every 5 seconds. Clips are only taken while a stream is live. Finished clips go to '
               '<a href="/">Waiting for review</a>; nothing is published from here.</p><h3>Streams</h3>']
        if not st["sessions"]:
            out.append('<div class=empty>No streams watched yet. Start one with <code>vicepulse live-watch --twitch '
                       '&lt;login&gt; --channel br --creator-id N</code>.</div>')
        else:
            out.append('<table><tr><th>Streamer</th><th>Status</th><th>Chat now</th><th>Normal</th><th>Spike score</th>'
                       '<th>Since</th></tr>')
            for s in st["sessions"]:
                badge = "LIVE" if s["status"] == "live" else s["status"].upper()
                out.append(f'<tr><td><b>{e(s["display_name"] or s["login"])}</b> <span class=tag>{e(s["channel_key"]).upper()}</span>'
                           f'<div class=meta>{e(s["stream_title"] or "")}</div></td>'
                           f'<td><span class=tag style="{"background:var(--no);color:#fff" if badge == "LIVE" else ""}">{badge}</span></td>'
                           f'<td>{e(s["chat_rate"] if s["chat_rate"] is not None else "-")} msg/s</td>'
                           f'<td>{e(s["chat_baseline"] if s["chat_baseline"] is not None else "-")} msg/s</td>'
                           f'<td>{e(s["last_score"] if s["last_score"] is not None else "-")}</td>'
                           f'<td class=meta>{e((s["started_at"] or s["created_at"]).strftime("%d/%m %H:%M"))}</td></tr>')
            out.append('</table>')
        out.append('<h3>Moments</h3>')
        if not st["moments"]:
            out.append('<div class=empty>No moments yet.</div>')
        else:
            out.append('<table><tr><th>#</th><th>Streamer</th><th>Detected</th><th>Score</th><th>Status</th>'
                       '<th>Time to clip</th><th></th></tr>')
            for m in st["moments"]:
                sig = m["signals"] or {}
                score = f'chat {sig.get("chat", {}).get("score", m["score"])}'
                if "audio_excitement" in sig:
                    score += f' &middot; audio {sig["audio_excitement"]} &middot; total {sig.get("combined")}'
                if sig.get("merged"):
                    score += f' &middot; +{sig["merged"]} merged'
                link = "".join(f'<a class=tag href="/media/{c}/youtube" target=_blank>clip {c}</a>' for c in (m["clip_ids"] or []))
                out.append(f'<tr><td>{m["id"]}</td><td>{e(m["login"])} <span class=tag>{e(m["channel_key"]).upper()}</span></td>'
                           f'<td class=meta>{e(m["detected_at"].strftime("%H:%M:%S"))}</td><td class=meta>{score}</td>'
                           f'<td><b style="color:{colour.get(m["status"], "var(--accent)")}">{label.get(m["status"], m["status"])}</b>'
                           f'<div class=meta>{e(m["status_reason"] or "")}</div></td>'
                           f'<td>{e(str(round(m["latency_s"])) + " s" if m["latency_s"] else "")}</td><td>{link}</td></tr>')
            out.append('</table>')
        return "".join(out)

    def tiktok_section(self, st: dict, csrf: str) -> str:
        out = ['<h3>TikTok</h3>']
        if not st["tiktok_client"]:
            out.append(
                '<div class=card><b>Step 1 - TikTok app</b><p class=meta>In the TikTok Developer Portal, open your app '
                '(or its <b>Sandbox</b>), add <b>Login Kit</b> (platform: Web) and <b>Content Posting API</b> with '
                '<b>Direct Post</b> on, tick the scopes <b>user.info.basic</b> and <b>video.publish</b>, and add this '
                f'exact <b>Redirect URI</b>:</p><p><code>{e(self.tiktok_redirect)}</code></p>'
                '<p class=meta>Then copy the <b>Client key</b> and <b>Client secret</b> (App details &gt; Credentials) '
                'and paste them here:</p>'
                f'<form class=row method=post action="/accounts/tiktok-client">{csrf}'
                '<input type=text name=client_key placeholder="Client key" required style="max-width:260px">'
                '<input type=password name=client_secret placeholder="Client secret" required autocomplete=off>'
                '<button class=approve>Save</button></form></div>')
        else:
            out.append(f'<p class=meta>TikTok app: ready. Redirect URI: <code>{e(self.tiktok_redirect)}</code></p>')
        rows = ['<table><tr><th>VicePulse channel</th><th>TikTok account</th><th></th></tr>']
        for key, t in st["tiktok"].items():
            if t["connected"]:
                ok = not t.get("expected") or (t.get("username") or "").lower() == t["expected"].lower().lstrip("@")
                life = (f' &middot; login renews itself ({t["renew_days_left"]} days before you must reconnect)'
                        if t.get("renew_days_left") is not None else "")
                who = (f'connected: <b>{e(t.get("nickname") or "")}</b> @{e(t.get("username") or "")} '
                       f'{"&#10003;" if ok else "(expected @" + e(t["expected"]) + ")"}{life}')
            else:
                who = "not connected" + (f' (expected @{e(t["expected"])})' if t.get("expected") else "")
            btn = ""
            if st["tiktok_client"]:
                label = "Reconnect" if t["connected"] else "Connect TikTok"
                btn = (f'<form method=post action="/accounts/tiktok-start">{csrf}<input type=hidden name=channel '
                       f'value="{e(key)}"><button class={"plain" if t["connected"] else "approve"}>{label}</button></form>')
            rows.append(f'<tr><td><b>{e(t["name"])}</b> <span class=tag>{e(key).upper()}</span></td><td>{who}</td><td>{btn}</td></tr>')
        rows.append("</table>")
        out.append("".join(rows))
        if st["tiktok_client"]:
            out.append(
                '<p class=meta>Press Connect, log in with the TikTok account for that channel and allow access. '
                'TikTok then sends you to an https address this page cannot open, so the browser shows an error page: '
                'copy the whole address from the address bar and paste it here:</p>'
                f'<form class=row method=post action="/accounts/tiktok-paste">{csrf}'
                f'<input type=text name=url placeholder="{e(self.tiktok_redirect)}?code=..." required>'
                '<button class=plain>Finish connecting</button></form>'
                '<p class=meta>Connecting never posts. To try a private test post, open a clip under <b>Reviewed</b> '
                'and press <b>Post to TikTok</b>.</p>')
        return "".join(out)

    def tiktok_start(self, form: dict) -> str:
        channel = form.get("channel", "")
        if channel not in {c["key"] for c in self.settings.channels()}:
            raise ValueError("unknown channel")
        state = secrets.token_urlsafe(24)
        self.oauth_states[state] = "tiktok:" + channel
        return accounts.tiktok_auth_url(self.settings.data_dir, self.tiktok_redirect, state)

    def tiktok_callback(self, q: dict, session=None) -> str:
        if q.get("error"):
            raise ValueError(f"TikTok sign-in was cancelled ({q.get('error_description') or q['error']})")
        tagged = self.oauth_states.pop(q.get("state", ""), "")
        if not tagged.startswith("tiktok:"):
            raise ValueError("this sign-in link expired; press Connect TikTok again")
        channel = tagged.split(":", 1)[1]
        info = accounts.tiktok_connect(self.settings.data_dir, channel, q.get("code", ""), self.tiktok_redirect,
                                       expected_username=self.settings.channel(channel).get("tiktok_username"),
                                       session=session)
        return (f"TikTok connected for {channel.upper()}: {info['nickname'] or ''} @{info['username']} "
                f"(permissions: {', '.join(info['scope'])}). Nothing was posted.")

    def instagram_section(self, st: dict, csrf: str) -> str:
        out = ['<h3>Instagram</h3>',
               '<div class=card><b>Option A - token from Meta (Unified Login, "Generate Token")</b><p class=meta>In Meta: '
               '<b>Use cases &gt; Customize &gt; Instagram API setup &gt; Step 2 Generate access token</b>. Press Generate '
               'Token next to one account, copy it, and paste it below for the matching channel. VicePulse checks which '
               'account it opens, that it is the right one, and that it may publish, then stores it. The token is never '
               'shown again.</p>']
        out.append(f'<form class=row method=post action="/accounts/instagram-token">{csrf}'
                   '<input type=hidden name=channel value="*"><span class=tag>ALL CHANNELS (one token for both)</span>'
                   '<input type=password name=token placeholder="paste the token once" required autocomplete=off>'
                   '<button class=approve>Check and save</button></form><p class=meta>Or per channel:</p>')
        for key, ig in st["instagram"].items():
            out.append(f'<form class=row method=post action="/accounts/instagram-token">{csrf}'
                       f'<input type=hidden name=channel value="{e(key)}">'
                       f'<span class=tag>{e(key).upper()} @{e(ig.get("expected") or "")}</span>'
                       '<input type=password name=token placeholder="paste the access token" required autocomplete=off>'
                       '<input type=text name=ig_user_id placeholder="Instagram account ID (optional)" style="max-width:220px">'
                       '<button class=approve>Check and save</button></form>')
        app_ok = all(accounts.meta_app(self.settings.data_dir))
        out.append('<p class=meta>Helper for system-user tokens: ' + (
            'Meta app saved &#10003; (used only to ask Meta which accounts a token was granted).</p>' if app_ok else
            'save the Meta app ID and app secret (<b>App settings &gt; Basic</b>) so VicePulse can ask Meta which '
            'Instagram accounts a token was granted.</p>'
            f'<form class=row method=post action="/accounts/meta-app">{csrf}'
            '<input type=text name=app_id placeholder="Meta app ID" required style="max-width:220px">'
            '<input type=password name=app_secret placeholder="Meta app secret" required autocomplete=off>'
            '<button class=plain>Save app</button></form>'))
        out.append('</div><p class=meta><b>Option B - log in with Instagram</b> (Instagram Login apps):</p>')
        if not st["instagram_client"]:
            out.append(
                '<div class=card><b>Step 1 - Instagram app</b><p class=meta>In your Meta app: <b>Instagram &gt; API setup '
                'with Instagram login &gt; Set up Instagram business login &gt; Business login settings</b>. Under '
                f'<b>OAuth redirect URIs</b> add:</p><p><code>{e(self.instagram_redirect)}</code></p>'
                '<p class=meta>Then copy the <b>Instagram app ID</b> and <b>Instagram app secret</b> from the same page '
                '(not the Facebook app ID) and paste them here:</p>'
                f'<form class=row method=post action="/accounts/instagram-client">{csrf}'
                '<input type=text name=app_id placeholder="Instagram app ID" required>'
                '<input type=text name=app_secret placeholder="Instagram app secret" required autocomplete=off>'
                '<button class=approve>Save</button></form></div>')
        else:
            out.append(f'<p class=meta>Instagram app: ready. Redirect address: <code>{e(self.instagram_redirect)}</code></p>')
        rows = ['<table><tr><th>VicePulse channel</th><th>Instagram account</th><th></th></tr>']
        for key, ig in st["instagram"].items():
            if not ig["connected"]:
                who = "not connected"
            else:
                life = ("token never expires" if ig.get("token_kind") == "system_user" else
                        f'login renews automatically ({ig["days_left"]} days left)' if ig.get("days_left") is not None else "")
                ok = ig.get("expected") and (ig.get("username") or "").lower() == ig["expected"].lower()
                who = (f'connected: <b>@{e(ig["username"])}</b> {"&#10003;" if ok else "(expected @" + e(ig.get("expected")) + ")"}'
                       f' &middot; ID {e(ig.get("user_id") or "")} &middot; {life}')
            btn = ""
            if st["instagram_client"]:
                label = "Reconnect" if ig["connected"] else "Connect Instagram"
                btn = (f'<form method=post action="/accounts/instagram-start">{csrf}<input type=hidden name=channel '
                       f'value="{e(key)}"><button class={"plain" if ig["connected"] else "approve"}>{label}</button></form>')
            rows.append(f'<tr><td><b>{e(ig["name"])}</b> <span class=tag>{e(key).upper()}</span></td><td>{who}</td><td>{btn}</td></tr>')
        rows.append("</table>")
        out.append("".join(rows))
        if st["instagram_client"]:
            out.append(
                '<p class=meta>Log in with the Instagram account for that channel (it must be a professional account). '
                'If the browser shows an error page after you allow access, copy the whole address from the address '
                'bar and paste it here:</p>'
                f'<form class=row method=post action="/accounts/instagram-paste">{csrf}'
                '<input type=text name=url placeholder="https://localhost:8090/oauth/instagram/callback?code=..." required>'
                '<button class=plain>Finish connecting</button></form>')
        return "".join(out)

    def instagram_start(self, form: dict) -> str:
        channel = form.get("channel", "")
        if channel not in {c["key"] for c in self.settings.channels()}:
            raise ValueError("unknown channel")
        state = secrets.token_urlsafe(24)
        self.oauth_states[state] = "instagram:" + channel
        return accounts.instagram_auth_url(self.settings.data_dir, self.instagram_redirect, state)

    def instagram_callback(self, q: dict, session=None) -> str:
        if q.get("error"):
            raise ValueError(f"Instagram sign-in was cancelled ({q.get('error_description') or q['error']})")
        tagged = self.oauth_states.pop(q.get("state", ""), "")
        if not tagged.startswith("instagram:"):
            raise ValueError("this sign-in link expired; press Connect Instagram again")
        channel = tagged.split(":", 1)[1]
        info = accounts.instagram_connect(self.settings.data_dir, channel, q.get("code", ""), self.instagram_redirect,
                                          session=session)
        return f"Instagram connected for {channel.upper()}: @{info['username']}. Nothing was published."

    def youtube_start(self, form: dict) -> str:
        channel = form.get("channel", "")
        if channel not in {c["key"] for c in self.settings.channels()}:
            raise ValueError("unknown channel")
        state = secrets.token_urlsafe(24)
        self.oauth_states[state] = "youtube:" + channel
        return accounts.youtube_auth_url(self.settings.data_dir, self.youtube_redirect, state)

    def youtube_callback(self, q: dict, session=None) -> str:
        if q.get("error"):
            raise ValueError(f"Google sign-in was cancelled ({q['error']})")
        tagged = self.oauth_states.pop(q.get("state", ""), "")
        if not tagged.startswith("youtube:"):
            raise ValueError("this sign-in link expired; press Connect again")
        channel = tagged.split(":", 1)[1]
        info = accounts.youtube_connect(self.settings.data_dir, channel, q.get("code", ""), self.youtube_redirect,
                                        session=session)
        return f"YouTube connected for {channel.upper()}: {info['channel_title']}. Nothing was published."

    # ------------------------------------------------------------------ TikTok posting screen
    # Built to TikTok's Content Sharing Guidelines: the account's nickname is shown, the privacy level comes from
    # the account's own options with nothing pre-selected, comments/Duet/Stitch start unticked, commercial content
    # starts off, the music consent statement sits above the button, a preview is shown, and the video is sent only
    # after the person presses Post. Uploads are private (SELF_ONLY) test posts behind VICEPULSE_TIKTOK_TEST_POSTS.

    def _tiktok_clip(self, conn, clip_id: int) -> dict:
        row = conn.execute(
            """SELECT cl.id, cl.status, cl.titles, cl.language, ch.key AS channel_key
               FROM clips cl JOIN channels ch ON ch.id = cl.channel_id WHERE cl.id = %s""", (clip_id,)).fetchone()
        if row is None:
            raise ValueError(f"clip {clip_id} not found")
        if row["status"] not in ("approved", "scheduled", "published"):
            raise ValueError(f"clip {clip_id} is {row['status']}; approve it first")
        renders = {r["platform"]: r for r in conn.execute(
            "SELECT platform, file_path, duration_s FROM renders WHERE clip_id = %s", (clip_id,)).fetchall()}
        render = renders.get("tiktok") or next((renders[p] for p in PLATFORM_ORDER if p in renders), None)
        if render is None:
            raise ValueError(f"clip {clip_id} has no rendered video")
        return {**row, "render": render, "render_platform": next(p for p in ("tiktok", *PLATFORM_ORDER) if p in renders)}

    def _tiktok_token(self, channel_key: str, session=None) -> str:
        token = accounts.tiktok_credentials(self.settings.data_dir, channel_key, session=session)
        if not token:
            raise ValueError(f"TikTok is not connected for {channel_key.upper()}; connect it on the Accounts page")
        return token

    def tiktok_post_page(self, conn, clip_id: int, session=None) -> str:
        clip = self._tiktok_clip(conn, clip_id)
        key = clip["channel_key"]
        info = accounts.tiktok_creator_info(self._tiktok_token(key, session), session=session)
        copy = (clip["titles"] or {}).get("tiktok") or (clip["titles"] or {}).get("youtube") or {}
        caption = "\n\n".join(x for x in (copy.get("title"), copy.get("description"),
                                          " ".join(copy.get("hashtags") or [])) if x)[:2200]
        labels = {"PUBLIC_TO_EVERYONE": "Everyone", "MUTUAL_FOLLOW_FRIENDS": "Friends",
                  "FOLLOWER_OF_CREATOR": "Followers", "SELF_ONLY": "Only me"}
        opts = "".join(
            f'<option value="{e(p)}"{"" if p == "SELF_ONLY" else " disabled"}>{e(labels.get(p, p))}'
            f'{"" if p == "SELF_ONLY" else " (after TikTok approves the app)"}</option>'
            for p in info.get("privacy_level_options") or [])
        max_s = info.get("max_video_post_duration_sec")
        dur = clip["render"]["duration_s"]
        too_long = bool(max_s and dur and float(dur) > float(max_s))

        def box(name: str, label: str, off: bool) -> str:
            return (f'<label class=meta><input type=checkbox name={name} value=1{" disabled" if off else ""}> {label}'
                    f'{" (turned off in this account&#39;s TikTok settings)" if off else ""}</label>')

        avatar = (f'<img src="{e(info["creator_avatar_url"])}" alt="" width=40 height=40 style="border-radius:50%">'
                  if info.get("creator_avatar_url") else "")
        switch = ("" if self.tiktok_test_posts else
                  '<div class="msg err">Test posting is switched off, so the Post button will not send anything. '
                  'To record the demo, set <code>VICEPULSE_TIKTOK_TEST_POSTS=1</code> in <code>.env</code> and restart '
                  'the review page.</div>')
        warn = (f'<div class="msg err">This video is {float(dur):.0f}s; this account can post at most {max_s}s.</div>'
                if too_long else "")
        return f"""{switch}{warn}
<div class=card style="max-width:760px">
 <div style="display:flex;gap:10px;align-items:center">{avatar}<div><b>Posting to: {e(info.get("creator_nickname") or "")}</b>
  <div class=meta>@{e(info.get("creator_username") or "")} &middot; VicePulse {e(key).upper()} &middot; clip {clip_id}</div></div></div>
 <div style="display:grid;grid-template-columns:minmax(0,240px) 1fr;gap:16px">
  <video controls preload=metadata src="/media/{clip_id}/{e(clip["render_platform"])}"></video>
  <form method=post action="/tiktok/post" id=ttform style="display:flex;flex-direction:column;gap:10px">
   <input type=hidden name=csrf value="{self.token}"><input type=hidden name=clip value="{clip_id}">
   <label class=meta>Caption (you can edit it)<br><textarea name=caption maxlength=2200 rows=6
     style="width:100%;background:#0c0e12;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px">{e(caption)}</textarea></label>
   <label class=meta>Who can view this video<br><select name=privacy_level id=privacy required
     style="padding:8px;border-radius:8px;background:#0c0e12;color:var(--text);border:1px solid var(--line)">
     <option value="" selected disabled>Choose who can view</option>{opts}</select></label>
   <div><div class=meta>Allow users to:</div>
    {box("allow_comment", "Comment", bool(info.get("comment_disabled")))}
    {box("allow_duet", "Duet", bool(info.get("duet_disabled")))}
    {box("allow_stitch", "Stitch", bool(info.get("stitch_disabled")))}</div>
   <div><label class=meta><input type=checkbox name=disclose id=disclose value=1> Disclose video content
     (turn on if this video promotes yourself, a brand, product or service)</label>
    <div id=discl style="display:none;margin-left:22px">
     <label class=meta><input type=checkbox name=your_brand id=yb value=1> Your brand
       - you are promoting yourself or your own business (labelled "Promotional content")</label><br>
     <label class=meta><input type=checkbox name=branded_content id=bc value=1> Branded content
       - you are promoting another brand or a third party (labelled "Paid partnership")</label>
     <div class=meta id=bcnote style="display:none">Branded content can't be private, so it can't be posted until the
       app is approved for public posts.</div></div></div>
   <p class=meta id=consent>By posting, you agree to TikTok's
     <a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target=_blank rel=noopener>Music Usage Confirmation</a>.</p>
   <button class=approve id=postbtn disabled>Post to TikTok</button>
   <p class=meta>After you press Post, the video is uploaded to TikTok. It can take a few minutes to process before
     it shows on the profile.</p>
  </form></div></div>
<script>
(function(){{var f=document.getElementById('ttform'),p=document.getElementById('privacy'),d=document.getElementById('disclose'),
 box=document.getElementById('discl'),yb=document.getElementById('yb'),bc=document.getElementById('bc'),
 note=document.getElementById('bcnote'),c=document.getElementById('consent'),b=document.getElementById('postbtn');
 var mu='<a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target=_blank rel=noopener>Music Usage Confirmation</a>',
     bp='<a href="https://www.tiktok.com/legal/page/global/bc-policy/en" target=_blank rel=noopener>Branded Content Policy</a>';
 function upd(){{box.style.display=d.checked?'block':'none';
  var branded=d.checked&&bc.checked;note.style.display=branded?'block':'none';
  c.innerHTML='By posting, you agree to TikTok\\'s '+(branded?bp+' and ':'')+mu+'.';
  b.disabled=!p.value||(d.checked&&!yb.checked&&!bc.checked)||(branded&&p.value==='SELF_ONLY')||{str(too_long).lower()};}}
 f.addEventListener('change',upd);upd();}})();
</script>"""

    def tiktok_post(self, conn, form: dict, session=None) -> str:
        """Upload one private test post after the person's explicit choices. Returns the publish_id."""
        from datetime import date

        from . import rights
        from .platforms import PlatformError, PublishRequest
        from .platforms.tiktok import TikTokPublisher

        clip = self._tiktok_clip(conn, int(form.get("clip", "0")))
        privacy = form.get("privacy_level", "")
        if not privacy:
            raise ValueError("choose who can view the video")
        if privacy != "SELF_ONLY":
            raise ValueError("only 'Only me' (private) posts are allowed until TikTok approves the app")
        disclose = form.get("disclose") == "1"
        your_brand, branded = disclose and form.get("your_brand") == "1", disclose and form.get("branded_content") == "1"
        if disclose and not (your_brand or branded):
            raise ValueError("you turned on content disclosure: choose Your brand and/or Branded content")
        if branded:
            raise ValueError("branded content can't be private, so it can't be posted as a test")
        caption = form.get("caption", "").strip()[:2200]
        if not self.tiktok_test_posts:
            raise ValueError("test posting is switched off (VICEPULSE_TIKTOK_TEST_POSTS=1 in .env turns it on); "
                             "nothing was sent")
        gate = rights.prepublish_gate(conn, clip["id"], "tiktok", on=date.today(), monetised=self.settings.monetised)
        if not gate.ok:
            raise ValueError("rights check failed: " + "; ".join(gate.reasons))
        pub = TikTokPublisher(access_token=self._tiktok_token(clip["channel_key"], session), audited=False,
                              session=session)
        req = PublishRequest(file_path=Path(clip["render"]["file_path"]), title=caption, description="", hashtags=[],
                             language=clip["language"], synthetic_media=self.settings.voice_mode == "synthetic",
                             options={"privacy_level": privacy, "allow_comment": form.get("allow_comment") == "1",
                                      "allow_duet": form.get("allow_duet") == "1",
                                      "allow_stitch": form.get("allow_stitch") == "1",
                                      "consent_confirmed": True, "your_brand": your_brand, "branded_content": False})
        try:
            result = pub.publish(req)
        except PlatformError as ex:
            db.log_event(conn, "tiktok_test_post", "clip", clip["id"], "failed", {"error": str(ex)[:300]})
            conn.commit()
            raise ValueError(f"TikTok upload failed: {ex}") from None
        db.log_event(conn, "tiktok_test_post", "clip", clip["id"], "uploaded",
                     {"publish_id": result.external_id, "privacy": privacy, "channel": clip["channel_key"]})
        conn.commit()
        return result.external_id

    def tiktok_status_page(self, channel_key: str, publish_id: str, session=None) -> str:
        from .platforms import PlatformError
        from .platforms.tiktok import TikTokPublisher

        if channel_key not in {c["key"] for c in self.settings.channels()}:
            raise ValueError("unknown channel")
        if not publish_id or len(publish_id) > 128 or not all(ch.isalnum() or ch in "._-~" for ch in publish_id):
            raise ValueError("bad publish id")
        try:
            st = TikTokPublisher(access_token=self._tiktok_token(channel_key, session), session=session).status(publish_id)
        except PlatformError as ex:
            raise ValueError(f"could not read the post status: {ex}") from None
        state = st.get("status") or "UNKNOWN"
        done = state in ("PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX")
        text = {"PROCESSING_UPLOAD": "TikTok is receiving the video...",
                "PROCESSING_DOWNLOAD": "TikTok is fetching the video...",
                "SEND_TO_USER_INBOX": "Sent to the TikTok app inbox to finish there.",
                "PUBLISH_COMPLETE": "Done. The video is on the account as a private (Only me) post.",
                "FAILED": f"TikTok could not post it: {st.get('fail_reason') or 'no reason given'}"}.get(state, state)
        refresh = "" if done else '<script>setTimeout(function(){location.reload()},5000)</script>'
        return (f'{refresh}<div class=card style="max-width:640px"><b>{e(text)}</b>'
                f'<div class=meta>Status: {e(state)} &middot; VicePulse {e(channel_key).upper()} &middot; '
                f'publish id {e(publish_id)}</div>'
                + ('' if done else '<div class=meta>Processing can take a few minutes. This page checks again every '
                   '5 seconds.</div>') + '<p><a href="/done">Back to reviewed clips</a></p></div>')

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
            if url.path == "/oauth/instagram/callback":
                from urllib.parse import quote
                try:
                    done = app.instagram_callback({k: v[0] for k, v in q.items()}, session=getattr(app, "http", None))
                    return self._redirect(f"/accounts?msg={quote(done)}")
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if url.path == "/oauth/twitch/callback":
                from urllib.parse import quote
                try:
                    done = app.twitch_callback({k: v[0] for k, v in q.items()}, session=getattr(app, "http", None))
                    return self._redirect(f"/accounts?msg={quote(done)}")
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if url.path == "/oauth/tiktok/callback":     # only reached if the redirect address opens this page
                from urllib.parse import quote
                try:
                    done = app.tiktok_callback({k: v[0] for k, v in q.items()}, session=getattr(app, "http", None))
                    return self._redirect(f"/accounts?msg={quote(done)}")
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if url.path.startswith("/tiktok/post/") or url.path == "/tiktok/status":
                from urllib.parse import quote
                try:
                    if url.path == "/tiktok/status":
                        body = app.tiktok_status_page((q.get("channel") or [""])[0], (q.get("id") or [""])[0],
                                                      session=getattr(app, "http", None))
                        return self._send(200, app.page("TikTok post status", body, "done", msg, err).encode())
                    tail = url.path.rsplit("/", 1)[1]
                    if not tail.isdigit():
                        return self._send(404, b"not found", "text/plain")
                    with db.connect(app.settings) as conn:
                        body = app.tiktok_post_page(conn, int(tail), session=getattr(app, "http", None))
                    return self._send(200, app.page("Post to TikTok", body, "done", msg, err).encode())
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/done?err=1&msg={quote(str(ex))}")
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
                elif url.path == "/live":
                    body = app.page("Live", app.live_page(conn), "live", msg, err)
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
            if path == "/accounts/meta-app":
                try:
                    accounts.save_meta_app(app.settings.data_dir, form.get("app_id", ""), form.get("app_secret", ""))
                    return self._redirect("/accounts?msg=" + quote("Meta app saved. Paste the token again."))
                except ValueError as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if path == "/accounts/instagram-token":
                try:
                    ch = form.get("channel", "")
                    cfg = {c["key"]: c for c in app.settings.channels()}
                    if ch == "*":
                        done, missed = [], []
                        for key, c in cfg.items():
                            try:
                                i = accounts.instagram_save_token(app.settings.data_dir, key, form.get("token", ""),
                                                                  expected_username=c.get("instagram_username"),
                                                                  session=getattr(app, "http", None))
                                done.append(f"{key.upper()} @{i['username']} (ID {i['user_id']})")
                            except ValueError as ex:
                                missed.append(f"{key.upper()}: {ex}")
                        if not done:
                            raise ValueError(" || ".join(missed))
                        note = "Instagram saved: " + ", ".join(done) + "; publishing permission confirmed."
                        if missed:
                            note += " Not saved - " + " || ".join(missed)
                        return self._redirect("/accounts?msg=" + quote(note + " Nothing was published.")
                                              + ("&err=1" if missed else ""))
                    if ch not in cfg:
                        raise ValueError("unknown channel")
                    info = accounts.instagram_save_token(app.settings.data_dir, ch, form.get("token", ""),
                                                         ig_user_id=form.get("ig_user_id", ""),
                                                         expected_username=cfg[ch].get("instagram_username"),
                                                         session=getattr(app, "http", None))
                    return self._redirect("/accounts?msg=" + quote(
                        f"Instagram {ch.upper()} saved: @{info['username']} (ID {info['user_id']}), publishing permission "
                        "confirmed. Nothing was published."))
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if path in ("/accounts/instagram-client", "/accounts/instagram-start", "/accounts/instagram-paste"):
                try:
                    if path == "/accounts/instagram-client":
                        accounts.save_instagram_client(app.settings.data_dir, form.get("app_id", ""),
                                                       form.get("app_secret", ""))
                        return self._redirect("/accounts?msg=" + quote("Instagram app saved. Now press Connect Instagram."))
                    if path == "/accounts/instagram-start":
                        return self._redirect(app.instagram_start(form))
                    done = app.instagram_callback(accounts.code_from_pasted_url(form.get("url", "")),
                                                  session=getattr(app, "http", None))
                    return self._redirect(f"/accounts?msg={quote(done)}")
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if path in ("/accounts/tiktok-client", "/accounts/tiktok-start", "/accounts/tiktok-paste"):
                try:
                    if path == "/accounts/tiktok-client":
                        accounts.save_tiktok_client(app.settings.data_dir, form.get("client_key", ""),
                                                    form.get("client_secret", ""))
                        return self._redirect("/accounts?msg=" + quote("TikTok app saved. Now press Connect TikTok."))
                    if path == "/accounts/tiktok-start":
                        return self._redirect(app.tiktok_start(form))
                    done = app.tiktok_callback(accounts.code_from_pasted_url(form.get("url", "")),
                                               session=getattr(app, "http", None))
                    return self._redirect(f"/accounts?msg={quote(done)}")
                except (ValueError, KeyError, OSError) as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if path == "/accounts/twitch-start":
                try:
                    return self._redirect(app.twitch_start())
                except ValueError as ex:
                    return self._redirect(f"/accounts?err=1&msg={quote(str(ex))}")
            if path == "/tiktok/post":
                clip = form.get("clip", "")
                try:
                    with db.connect(app.settings) as conn:
                        pid = app.tiktok_post(conn, form, session=getattr(app, "http", None))
                        key = conn.execute("SELECT ch.key FROM clips cl JOIN channels ch ON ch.id = cl.channel_id "
                                           "WHERE cl.id = %s", (int(clip),)).fetchone()["key"]
                    return self._redirect(f"/tiktok/status?channel={quote(key)}&id={quote(pid)}&msg="
                                          + quote("Uploaded to TikTok as a private (Only me) post."))
                except (ValueError, KeyError, OSError) as ex:
                    where = f"/tiktok/post/{int(clip)}" if clip.isdigit() else "/done"
                    return self._redirect(f"{where}?err=1&msg={quote(str(ex))}")
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
