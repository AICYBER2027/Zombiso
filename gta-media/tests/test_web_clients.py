"""Creator clients and the website: sign-up, sessions, access control, branding, permissions, deliveries,
plans (no payments), EN/PT pages, security headers and the static export. No network, no sockets."""

import io
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

os.environ["VICEPULSE_PBKDF2_ITERATIONS"] = "2000"      # fast hashing in tests only

from vicepulse import clients, db, rights  # noqa: E402
from vicepulse.web import i18n  # noqa: E402
from vicepulse.web.site import App, Request, build_static, markdown  # noqa: E402

PW = "correct-horse-battery"


def _reg(conn, email="ana@example.com", name="Ana Plays", lang="en"):
    return clients.register(conn, email=email, password=PW, display_name=name, language=lang, accept_terms=True)


def _user(conn, uid):
    token, _ = clients.create_session(conn, uid)
    return clients.session_user(conn, token), token


def test_passwords_are_hashed_and_checked():
    h = clients.hash_password(PW)
    assert PW not in h and h.startswith("pbkdf2_sha256$2000$")
    assert clients.verify_password(PW, h) and not clients.verify_password("wrong-password!", h)
    assert not clients.verify_password(PW, "garbage")
    for bad in ("short", "aaaaaaaaaaaa"):
        with pytest.raises(ValueError):
            clients.check_password_rules(bad)


def test_register_login_lockout_and_sessions(conn):
    out = _reg(conn)
    assert out["slug"] == "ana-plays"
    with pytest.raises(ValueError, match="already exists"):
        _reg(conn, name="Other")
    with pytest.raises(ValueError, match="terms"):
        clients.register(conn, email="b@example.com", password=PW, display_name="B b")
    c = conn.execute("SELECT * FROM clients WHERE id = %s", (out["client_id"],)).fetchone()
    assert c["status"] == "pending" and c["plan_key"] == "free"
    assert conn.execute("SELECT status, provider FROM subscriptions").fetchone() == {"status": "trialing", "provider": "none"}

    assert clients.authenticate(conn, "ANA@example.com ", PW)["id"] == out["user_id"]
    now = datetime.now(timezone.utc)
    for _ in range(clients.MAX_FAILED):
        assert clients.authenticate(conn, "ana@example.com", "wrong-password", now=now) is None
    assert clients.authenticate(conn, "ana@example.com", PW, now=now) is None              # locked
    later = now + timedelta(minutes=clients.LOCK_MINUTES + 1)
    assert clients.authenticate(conn, "ana@example.com", PW, now=later) is not None

    token, csrf = clients.create_session(conn, out["user_id"])
    row = conn.execute("SELECT token_sha256 FROM web_sessions").fetchone()
    assert token not in row["token_sha256"]                         # only the hash is stored
    u = clients.session_user(conn, token)
    assert u["csrf"] == csrf and u["client_id"] == out["client_id"]
    assert clients.session_user(conn, token, now=now + timedelta(days=clients.SESSION_DAYS + 1)) is None
    clients.end_session(conn, token)
    assert clients.session_user(conn, token) is None


def test_clients_cannot_see_each_other(conn, tmp_path):
    a, b = _reg(conn), _reg(conn, "bob@example.com", "Bob Clips")
    ua, _ = _user(conn, a["user_id"])
    ub, _ = _user(conn, b["user_id"])
    f = tmp_path / "x.mp4"
    f.write_bytes(b"\0" * 100)
    did = clients.add_delivery(conn, client_id=a["client_id"], file_path=str(f), title="Ana clip")
    assert clients.delivery(conn, ua, did)["title"] == "Ana clip"
    for call in (lambda: clients.delivery(conn, ub, did), lambda: clients.get_client(conn, ub, a["client_id"]),
                 lambda: clients.update_branding(conn, ub, a["client_id"], {"handle": "x"}),
                 lambda: clients.deliveries(conn, ub, a["client_id"])):
        with pytest.raises(clients.AccessDenied):
            call()
    viewer_id = clients.add_user(conn, ua, email="v@example.com", password=PW, role="viewer")
    uv, _ = _user(conn, viewer_id)
    assert clients.deliveries(conn, uv, a["client_id"])
    with pytest.raises(clients.AccessDenied):
        clients.review_delivery(conn, uv, did, approve=True)
    staff_id = clients.create_staff(conn, email="staff@vicepulse.invalid", password=PW)
    us, _ = _user(conn, staff_id)
    assert clients.delivery(conn, us, did)["id"] == did


def test_branding_preferences_plan_limits_and_logo(conn, tmp_path):
    a = _reg(conn)
    u, _ = _user(conn, a["user_id"])
    cid = a["client_id"]
    b = clients.update_branding(conn, u, cid, {"creator_name": "Ana", "handle": "anaplays", "primary": "#ff0055",
                                               "frame_style": "neon", "frame_radius": "30", "monogram": "ap"})
    assert b["handle"] == "@anaplays" and b["theme"]["primary"] == "#FF0055" and b["frame"] == {"style": "neon", "radius": 30}
    assert b["monogram"] == "AP"
    for bad in ({"primary": "red"}, {"frame_style": "sparkles"}, {"frame_radius": "200"}, {"handle": "bad handle!"}):
        with pytest.raises(ValueError):
            clients.update_branding(conn, u, cid, bad)
    with pytest.raises(ValueError, match="not in your plan"):
        clients.update_preferences(conn, u, cid, {"style": "pro"})         # Trial plan: clean only
    clients.staff_set_plan(conn, {"is_staff": True, "email": "s"}, cid, "pro")
    p = clients.update_preferences(conn, u, cid, {"style": "pro", "platforms": ["youtube", "tiktok"],
                                                  "facecam": "0.02,0.63,0.24,0.33:pip", "captions": "1"})
    assert p["style"] == "pro" and p["platforms"] == ["youtube", "tiktok"] and p["captions"] is True
    with pytest.raises(ValueError):
        clients.update_preferences(conn, u, cid, {"facecam": "top-left"})

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1500, 1500), "#FF2D95").save(buf, "JPEG")
    path = clients.save_logo(conn, u, cid, buf.getvalue(), tmp_path)
    assert Image.open(path).size == (1024, 1024)
    with pytest.raises(ValueError, match="PNG, JPEG"):
        clients.save_logo(conn, u, cid, b"<svg onload=alert(1)>", tmp_path)

    c = conn.execute("SELECT * FROM clients WHERE id = %s", (cid,)).fetchone()
    opts = clients.render_options(conn, c)
    assert opts["style"] == "pro" and opts["size"] == "1080x1920" and opts["brand"]["logo"] == path
    assert opts["brand"]["theme"]["primary"] == "#FF0055" and opts["brand"]["frame"]["style"] == "neon"


def test_plan_requests_never_charge(conn, monkeypatch):
    a = _reg(conn)
    u, _ = _user(conn, a["user_id"])
    out = clients.request_plan(conn, u, "studio")
    assert out == {"subscription_id": out["subscription_id"], "plan": "studio", "status": "none", "charged": False}
    assert conn.execute("SELECT plan_key FROM clients WHERE id = %s", (a["client_id"],)).fetchone()["plan_key"] == "free"
    with pytest.raises(clients.PaymentsDisabled):
        clients.payment_provider().checkout_url({}, {})
    monkeypatch.setenv("VICEPULSE_PAYMENTS", "stripe")
    with pytest.raises(clients.PaymentsDisabled):
        clients.payment_provider()
    assert clients.usage(conn, a["client_id"])["clips_left"] == 5


def test_permission_records_need_staff_verification_and_can_be_withdrawn(conn):
    a = _reg(conn)
    u, _ = _user(conn, a["user_id"])
    cid = a["client_id"]
    p1 = clients.request_permission(conn, u, cid, scope="clip_creation", platforms=["twitch"],
                                    evidence_url="https://example.com/permission.pdf")
    p2 = clients.request_permission(conn, u, cid, scope="vicepulse_channel_publishing",
                                    platforms=["youtube", "instagram"])
    with pytest.raises(ValueError):
        clients.request_permission(conn, u, cid, scope="everything")
    with pytest.raises(ValueError, match="https"):
        clients.request_permission(conn, u, cid, scope="branding", evidence_url="javascript:alert(1)")
    # a request alone never creates a usable rights entry
    assert conn.execute("SELECT count(*) AS n FROM rights_ledger").fetchone()["n"] == 0
    with pytest.raises(clients.AccessDenied):
        clients.staff_verify_permission(conn, u, p1, document_url="https://x.example/doc")
    staff = {"is_staff": True, "email": "staff@vicepulse.invalid"}
    with pytest.raises(ValueError, match="signed licence"):
        clients.staff_verify_permission(conn, staff, p1, document_url="")
    rid = clients.staff_verify_permission(conn, staff, p1, document_url="https://drive.example/signed.pdf")
    assert clients.staff_verify_permission(conn, staff, p2, document_url="https://drive.example/signed.pdf") == rid
    row = conn.execute("SELECT * FROM rights_ledger WHERE id = %s", (rid,)).fetchone()
    assert row["licence_type"] == "creator_permission" and row["verified_by"] == staff["email"]
    assert sorted(row["scopes"]) == ["clip_creation", "vicepulse_channel_publishing"]
    assert sorted(row["platforms"]) == ["instagram", "twitch", "youtube"] and not row["monetisation_allowed"]
    assert rights.check_grant(row, platform="youtube", scope="vicepulse_channel_publishing").ok
    assert conn.execute("SELECT status FROM clients WHERE id = %s", (cid,)).fetchone()["status"] == "active"

    clients.revoke_permission(conn, u, p2)                  # stops VicePulse-channel posting at once
    row = conn.execute("SELECT * FROM rights_ledger WHERE id = %s", (rid,)).fetchone()
    assert row["scopes"] == ["clip_creation"]
    assert not rights.check_grant(row, platform="youtube", scope="vicepulse_channel_publishing").ok
    clients.revoke_permission(conn, u, p1)
    assert conn.execute("SELECT revoked_at FROM rights_ledger WHERE id = %s", (rid,)).fetchone()["revoked_at"] is not None


def test_deliveries_review_and_analytics(conn, tmp_path):
    a = _reg(conn)
    u, _ = _user(conn, a["user_id"])
    for i, style in enumerate(("pro", "pro", "clean")):
        clients.add_delivery(conn, client_id=a["client_id"], file_path=str(tmp_path / f"{i}.mp4"), title=f"c{i}",
                             style=style)
    ds = clients.deliveries(conn, u, a["client_id"])
    clients.review_delivery(conn, u, ds[0]["id"], approve=True)
    clients.review_delivery(conn, u, ds[1]["id"], approve=False, note="shorter please")
    an = clients.analytics(conn, u, a["client_id"])
    assert an["clips"] == 3 and an["by_style"] == {"pro": 2, "clean": 1} and an["approval_rate"] == 0.5
    assert clients.usage(conn, a["client_id"])["clips_used"] == 3


# ---------------------------------------------------------------- website

@pytest.fixture()
def app(conn, settings):
    class Shared:                       # every request reuses the test connection
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            conn.commit()
            return False

    return App(settings, connect=lambda: Shared(), secure_cookies=True)


def req(method, path, *, body=b"", cookie="", headers=None, query=None):
    h = {"host": "vicepulse.test", **({"cookie": cookie} if cookie else {}), **(headers or {})}
    if method == "POST" and "content-type" not in h:
        h["content-type"] = "application/x-www-form-urlencoded"
    return Request(method, path, query or {}, h, body)


def form(**kw):
    from urllib.parse import urlencode
    return urlencode(kw, doseq=True).encode()


def _signup(app, lang="en", email="ana@example.com"):
    r = app.handle(req("POST", f"/{lang}/signup", body=form(name="Ana Plays", email=email, password=PW,
                                                            language="pt-BR" if lang == "pt" else "en", accept="1")))
    assert r.status == 303, r.body[:300]
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie
    return cookie.split(";")[0]


def _csrf(app, cookie, path="/en/dashboard/branding"):
    page = app.handle(req("GET", path, cookie=cookie)).body.decode()
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_public_pages_in_both_languages(app):
    r = app.handle(req("GET", "/", headers={"accept-language": "pt-BR,pt;q=0.9"}))
    assert r.status == 303 and r.headers["Location"] == "/pt/"
    en = app.handle(req("GET", "/en/")).body.decode()
    pt = app.handle(req("GET", "/pt/")).body.decode()
    assert 'lang="en"' in en and "Four editing styles" in en and "Streamer Pro" in en
    assert 'lang="pt-BR"' in pt and "Quatro estilos de edição" in pt and "Cinematográfico" in pt
    for path in ("/en/pricing", "/pt/pricing"):
        body = app.handle(req("GET", path)).body.decode()
        assert "Studio" in body and "$149" in body
    priv = app.handle(req("GET", "/en/privacy")).body.decode()
    assert "Draft" in priv and "<h1>" in priv and "{{BRAND}}" not in priv
    assert app.handle(req("GET", "/en/nope")).status == 404
    r = app.handle(req("GET", "/en/"))
    assert r.headers["X-Frame-Options"] == "DENY" and "script-src 'none'" in r.headers["Content-Security-Policy"]
    assert app.handle(req("GET", "/static/site.css")).status == 200
    assert app.handle(req("GET", "/static/fonts/Anton-Regular.ttf")).status == 200
    assert app.handle(req("GET", "/static/../../.env")).status == 404
    assert not i18n.missing_keys()["pt"]


def test_signup_dashboard_and_forms(app, conn):
    cookie = _signup(app)
    dash = app.handle(req("GET", "/en/dashboard", cookie=cookie)).body.decode()
    assert "Welcome, Ana Plays" in dash and "Getting started" in dash
    assert app.handle(req("GET", "/en/dashboard")).headers["Location"] == "/en/login"
    for tab in ("clips", "branding", "editing", "permissions", "analytics", "plan", "account"):
        assert app.handle(req("GET", f"/en/dashboard/{tab}", cookie=cookie)).status == 200, tab
        assert app.handle(req("GET", f"/pt/dashboard/{tab}", cookie=cookie)).status == 200, tab
    assert app.handle(req("GET", "/en/dashboard/staff", cookie=cookie)).status == 404

    csrf = _csrf(app, cookie)
    # forms without the session's CSRF token, or from another site, are refused
    assert app.handle(req("POST", "/en/dashboard/branding", cookie=cookie, body=form(handle="x"))).status == 403
    assert app.handle(req("POST", "/en/dashboard/branding", cookie=cookie, body=form(csrf=csrf, handle="x"),
                          headers={"origin": "https://evil.example"})).status == 403
    r = app.handle(req("POST", "/en/dashboard/branding", cookie=cookie,
                       body=form(csrf=csrf, creator_name="Ana", handle="ana_plays", primary="#00FFAA")))
    assert r.status == 303 and "err=1" not in r.headers["Location"]
    r = app.handle(req("POST", "/en/dashboard/branding", cookie=cookie, body=form(csrf=csrf, primary="pink")))
    assert "err=1" in r.headers["Location"]
    r = app.handle(req("POST", "/en/dashboard/editing", cookie=cookie,
                       body=form(csrf=csrf, style="pro", layout="auto")))
    assert "err=1" in r.headers["Location"] and "plan" in r.headers["Location"]
    r = app.handle(req("POST", "/en/dashboard/permissions", cookie=cookie,
                       body=form(csrf=csrf, scope="clip_creation", platforms=["twitch", "youtube"],
                                 evidence_url="https://example.com/ok")))
    assert "err=1" not in r.headers["Location"]
    perms = app.handle(req("GET", "/en/dashboard/permissions", cookie=cookie)).body.decode()
    assert "Waiting for check" in perms and "twitch, youtube" in perms
    r = app.handle(req("POST", "/en/dashboard/plan", cookie=cookie, body=form(csrf=csrf, plan="pro")))
    assert "nothing%20has%20been%20charged" in r.headers["Location"]

    # logo upload (multipart)
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "#123456").save(buf, "PNG")
    bnd = "XyZbound"
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n--{bnd}\r\n"
            f"Content-Disposition: form-data; name=\"logo\"; filename=\"l.png\"\r\nContent-Type: image/png\r\n\r\n"
            ).encode() + buf.getvalue() + f"\r\n--{bnd}--\r\n".encode()
    r = app.handle(req("POST", "/en/dashboard/logo", cookie=cookie, body=body,
                       headers={"content-type": f"multipart/form-data; boundary={bnd}"}))
    assert r.status == 303 and "err=1" not in r.headers["Location"], r.headers
    cid = conn.execute("SELECT id FROM clients WHERE slug = 'ana-plays'").fetchone()["id"]
    logo = app.handle(req("GET", f"/media/logo/{cid}", cookie=cookie))
    assert logo.status == 200 and logo.body[:4] == b"\x89PNG"

    r = app.handle(req("POST", "/en/logout", cookie=cookie, body=form(csrf=csrf)))
    assert "Max-Age=0" in r.headers["Set-Cookie"]
    assert app.handle(req("GET", "/en/dashboard", cookie=cookie)).status == 303


def test_login_errors_and_portuguese_signup(app):
    _signup(app, "pt", email="joao@example.com")
    bad = app.handle(req("POST", "/pt/login", body=form(email="joao@example.com", password="nope-nope-nope")))
    assert bad.status == 401 and "incorretos" in bad.body.decode()
    ok = app.handle(req("POST", "/en/login", body=form(email="joao@example.com", password=PW)))
    assert ok.status == 303 and ok.headers["Location"] == "/en/dashboard"
    weak = app.handle(req("POST", "/en/signup", body=form(name="X Y", email="w@example.com", password="123", accept="1")))
    assert weak.status == 400 and "at least 10" in weak.body.decode()


def test_delivery_preview_download_range_and_isolation(app, conn, tmp_path):
    ca = _signup(app)
    cb = _signup(app, email="bob@example.com")
    cid = conn.execute("SELECT id FROM clients WHERE slug = 'ana-plays'").fetchone()["id"]
    f = tmp_path / "clip.mp4"
    f.write_bytes(bytes(range(256)) * 4)
    did = clients.add_delivery(conn, client_id=cid, file_path=str(f), title="Huge <jump>", style="pro", duration_s=18.2)
    page = app.handle(req("GET", f"/en/dashboard/clips/{did}", cookie=ca)).body.decode()
    assert "Huge &lt;jump&gt;" in page and f"/media/delivery/{did}" in page and "Approve" in page
    full = app.handle(req("GET", f"/media/delivery/{did}", cookie=ca))
    assert full.status == 200 and len(full.body) == 1024
    part = app.handle(req("GET", f"/media/delivery/{did}", cookie=ca, headers={"range": "bytes=10-19"}))
    assert part.status == 206 and part.body == bytes(range(10, 20)) and part.headers["Content-Range"] == "bytes 10-19/1024"
    dl = app.handle(req("GET", f"/media/delivery/{did}", cookie=ca, query={"download": ["1"]}))
    assert "attachment" in dl.headers["Content-Disposition"]
    assert app.handle(req("GET", f"/media/delivery/{did}", cookie=cb)).status == 403         # other client
    assert app.handle(req("GET", f"/media/delivery/{did}")).status == 401                     # signed out
    assert app.handle(req("GET", f"/en/dashboard/clips/{did}", cookie=cb)).status == 403
    csrf = _csrf(app, ca)
    r = app.handle(req("POST", f"/en/dashboard/clips/{did}", cookie=ca, body=form(csrf=csrf, decision="approve")))
    assert r.status == 303
    assert conn.execute("SELECT status FROM client_deliveries WHERE id = %s", (did,)).fetchone()["status"] == "approved"


def test_staff_page_verifies_permissions(app, conn):
    cookie = _signup(app)
    csrf = _csrf(app, cookie)
    app.handle(req("POST", "/en/dashboard/permissions", cookie=cookie, body=form(csrf=csrf, scope="branding")))
    clients.create_staff(conn, email="staff@vicepulse.invalid", password=PW)
    r = app.handle(req("POST", "/en/login", body=form(email="staff@vicepulse.invalid", password=PW)))
    staff = r.headers["Set-Cookie"].split(";")[0]
    page = app.handle(req("GET", "/en/dashboard/staff", cookie=staff)).body.decode()
    assert "Ana Plays" in page
    scsrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    pid = conn.execute("SELECT id FROM client_permissions").fetchone()["id"]
    r = app.handle(req("POST", "/en/dashboard/staff", cookie=staff,
                       body=form(csrf=scsrf, id=pid, decision="verify", document_url="https://drive.example/doc.pdf")))
    assert "err=1" not in r.headers["Location"]
    assert conn.execute("SELECT status FROM client_permissions WHERE id = %s", (pid,)).fetchone()["status"] == "verified"
    # a creator cannot use the staff action
    r = app.handle(req("POST", "/en/dashboard/staff", cookie=cookie, body=form(csrf=csrf, id=pid, decision="verify")))
    assert r.status == 403


def test_static_export(settings, tmp_path):
    files = build_static(settings, tmp_path / "site", app_url="https://app.vicepulse.example")
    assert {"en/index.html", "pt/index.html", "en/privacy.html", "pt/terms.html", "en/pricing.html"} <= set(files)
    home = (tmp_path / "site" / "en" / "index.html").read_text(encoding="utf-8")
    assert "https://app.vicepulse.example/en/signup" in home and "/en/pricing.html" in home
    assert (tmp_path / "site" / "static" / "fonts" / "Anton-Regular.ttf").is_file()
    assert "Studio" in (tmp_path / "site" / "pt" / "pricing.html").read_text(encoding="utf-8")


def test_markdown_escapes_html():
    out = markdown("# Title\n\nHello <script>x</script> **bold** [link](https://a.example)\n\n- one\n- two")
    assert "<h1>Title</h1>" in out and "&lt;script&gt;" in out and "<b>bold</b>" in out
    assert '<a href="https://a.example">link</a>' in out and "<li>two</li>" in out
    assert "javascript:" not in markdown("[x](javascript:alert(1))").split("href")[-1] or "href" not in markdown("[x](javascript:alert(1))")
