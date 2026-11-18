"""Creator frame styles in the graphics, automatic facecam default, and rendering a clip for a creator client
(rights-gated, plan-limited, delivered to the dashboard, nothing added to the VicePulse queues)."""

import os

import pytest
from PIL import Image

os.environ.setdefault("VICEPULSE_PBKDF2_ITERATIONS", "2000")

from vicepulse import clients, render  # noqa: E402
from vicepulse.graphics import Assets  # noqa: E402

PW = "correct-horse-battery"


def _alpha_px(path):
    return sum(1 for a in Image.open(path).getchannel("A").tobytes() if a > 40)


def test_frame_styles_follow_the_creator_branding(tmp_path):
    base = {"theme": {"primary": "#00C2FF", "secondary": "#7A00FF"}}
    out = {}
    for style in ("gradient", "solid", "neon", "minimal"):
        g = Assets(tmp_path, (360, 640), {**base, "frame": {"style": style, "radius": 10, "thickness": 8}})
        png, pad = g.frame(200, 120, name="cam", frames=4)
        out[style] = (png, g)
    assert out["gradient"][0].endswith("cam_%02d.png") and out["neon"][0].endswith("cam_%02d.png")
    assert out["solid"][0].endswith("cam.png") and out["minimal"][0].endswith("cam.png")   # nothing to animate
    first = lambda p: p.replace("%02d", "00")
    neon, grad = _alpha_px(first(out["neon"][0])), _alpha_px(first(out["gradient"][0]))
    assert neon > grad > _alpha_px(out["minimal"][0])          # neon glows wider, minimal is a thin line
    solid = Image.open(out["solid"][0]).convert("RGBA")
    raw = solid.tobytes()
    colours = {tuple(raw[i:i + 3]) for i in range(0, len(raw), 4) if raw[i + 3] == 255}
    assert (0, 194, 255) in colours and (122, 0, 255) not in colours    # single brand colour, no gradient
    thick = Assets(tmp_path, (360, 640), {**base, "frame": {"style": "solid", "thickness": 20}})
    assert _alpha_px(thick.frame(200, 120, name="t")[0]) > _alpha_px(out["solid"][0])
    assert len({g.dir for _, g in out.values()}) == 4            # each frame setting gets its own cache folder


def test_facecam_detection_is_on_by_default(monkeypatch):
    monkeypatch.delenv("VICEPULSE_FACECAM", raising=False)
    assert render.default_facecam() == "auto"
    monkeypatch.setenv("VICEPULSE_FACECAM", "none")
    assert render.default_facecam() is None


def test_client_render_needs_verified_permission_and_delivers(conn, settings, footage):
    reg = clients.register(conn, email="ana@example.com", password=PW, display_name="Ana Plays", accept_terms=True)
    token, _ = clients.create_session(conn, reg["user_id"])
    user = clients.session_user(conn, token)
    cid = reg["client_id"]
    clients.update_branding(conn, user, cid, {"creator_name": "Ana", "primary": "#00C2FF", "frame_style": "neon"})
    kw = dict(in_s=0.0, out_s=20.0, title="Big jump", size="360x640", encoder="libx264")

    with pytest.raises(PermissionError, match="no verified permission"):
        render.render_for_client(conn, settings, "ana-plays", str(footage), **kw)
    pid = clients.request_permission(conn, user, cid, scope="branding")
    with pytest.raises(PermissionError):                     # a request is not a permission
        render.render_for_client(conn, settings, "ana-plays", str(footage), **kw)
    staff = {"is_staff": True, "email": "staff@vicepulse.invalid"}
    clients.staff_verify_permission(conn, staff, pid, document_url="https://drive.example/signed.pdf")
    with pytest.raises(PermissionError, match="clip_creation"):      # verified, but only for branding
        render.render_for_client(conn, settings, "ana-plays", str(footage), **kw)
    p2 = clients.request_permission(conn, user, cid, scope="clip_creation", platforms=["twitch"])
    clients.staff_verify_permission(conn, staff, p2, document_url="https://drive.example/signed.pdf")

    before = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
              for t in ("sources", "clips", "publish_jobs")}
    out = render.render_for_client(conn, settings, "ana-plays", str(footage), **kw)
    assert out["style"] == "clean" and out["qc"]["ok"]                  # Trial plan: Clean only
    assert (out["qc"]["width"], out["qc"]["height"]) == (360, 640)
    d = clients.deliveries(conn, user, cid)
    assert len(d) == 1 and d[0]["title"] == "Big jump" and d[0]["file_sha256"]
    assert str(settings.data_dir) in d[0]["file_path"] and f"clients/{cid}/" in d[0]["file_path"].replace("\\", "/")
    after = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in before}
    assert after == before                                  # private to the client: no VicePulse queue rows

    conn.execute("UPDATE plans SET clips_per_month = 1 WHERE key = 'free'")
    conn.commit()
    with pytest.raises(PermissionError, match="used up"):
        render.render_for_client(conn, settings, "ana-plays", str(footage), **kw)
    clients.revoke_permission(conn, user, p2)
    conn.execute("UPDATE plans SET clips_per_month = 5 WHERE key = 'free'")
    conn.commit()
    with pytest.raises(PermissionError):
        render.render_for_client(conn, settings, "ana-plays", str(footage), **kw)
