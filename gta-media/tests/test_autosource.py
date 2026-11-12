"""Hands-free clip production from legitimately usable footage (Wikimedia Commons CC files, own folder)."""

from __future__ import annotations

import functools
import hashlib
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from conftest import make_footage
from test_discovery import CFG
from vicepulse import autosource, media, rights
from vicepulse.llm import MockLLM


@pytest.fixture()
def file_server(tmp_path):
    root = tmp_path / "served"
    root.mkdir()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class CommonsSession(requests.Session):
    """Answers Commons API searches from a fixed list; real HTTP for downloads (the local file server)."""

    def __init__(self, items):
        super().__init__()
        self.items, self.api_calls, self.user_agents = items, [], []

    def get(self, url, **kw):
        self.user_agents.append((kw.get("headers") or {}).get("User-Agent"))
        if url == autosource.COMMONS_API:
            self.api_calls.append(kw["params"])
            pages = {str(i): {"title": it["title"], "imageinfo": [{
                "url": it["url"], "descriptionurl": it["page"], "mime": it["mime"], "size": it["size"], "sha1": it["sha1"],
                "duration": it.get("duration"),
                "extmetadata": {"LicenseShortName": {"value": it["licence"]}, "Artist": {"value": f"<a>{it['artist']}</a>"},
                                "AttributionRequired": {"value": "true"}}}]} for i, it in enumerate(self.items)}
            r = requests.Response()
            r.status_code, r._content = 200, __import__("json").dumps({"query": {"pages": pages}}).encode()
            return r
        return super().get(url, **kw)


def commons_item(path, base_url, title, licence, artist="Test Author"):
    data = path.read_bytes()
    return {"title": f"File:{title}", "url": f"{base_url}/{path.name}", "page": f"https://commons.example.invalid/wiki/File:{title}",
            "mime": "video/mp4", "size": len(data), "sha1": hashlib.sha1(data).hexdigest(), "licence": licence,
            "artist": artist, "duration": 48}


def test_licence_rules():
    ok = {n: autosource.classify_licence(n) for n in ("CC BY 4.0", "CC BY 3.0", "CC0", "Public domain")}
    assert all(ok.values()) and ok["CC BY 4.0"]["cc_licence"] == "CC-BY-4.0"
    for n in ("CC BY-NC 4.0", "CC BY-ND 4.0", "CC BY-SA 4.0", "CC BY-NC-SA 3.0", "GFDL", "", None):
        assert autosource.classify_licence(n) is None, n
    assert autosource.classify_licence("CC BY-SA 4.0", allow_share_alike=True)["cc_licence"] == "CC-BY-SA-4.0"


def test_commons_requires_contact_details():
    with pytest.raises(ValueError):
        autosource.Commons("")


def test_hands_free_commons_clip_end_to_end(settings, conn, file_server, tmp_path):
    root, base = file_server
    good = make_footage(root / "Open_World_Gameplay.mp4")
    nc = shutil.copy(good, root / "NonCommercial_Gameplay.mp4")
    items = [commons_item(root / "NonCommercial_Gameplay.mp4", base, "NonCommercial_Gameplay.mp4", "CC BY-NC 4.0"),
             commons_item(good, base, "Open_World_Gameplay.mp4", "CC BY 4.0", artist="Xbox México")]
    session = CommonsSession(items)
    commons = autosource.Commons("tests@example.invalid", session=session)
    cfg = {**CFG, "autosource": {**CFG["autosource"], "commons_queries": ["gameplay"], "max_files_per_run": 1}}

    out = autosource.run(conn, settings, MockLLM(), cfg, commons=commons, sources="commons")
    assert any("not usable: CC BY-NC" in s["reason"] for s in out["skipped"])
    assert len(out["processed"]) == 1 and out["processed"][0]["licence"] == "CC BY 4.0"
    assert all("contact: tests@example.invalid" in ua for ua in session.user_agents)      # Wikimedia UA policy

    # rights recorded automatically, with attribution
    r = conn.execute("SELECT * FROM rights_ledger").fetchone()
    assert r["licence_type"] == "creative_commons" and r["cc_licence"] == "CC-BY-4.0" and r["verified_by"].startswith("auto:")
    assert r["credit_text"] == "Video: Xbox México (CC BY 4.0), via Wikimedia Commons"

    # one finished 1080x1920 MP4 with burned subtitles, saved under <data>/finished/<date>/
    [finished] = out["finished_files"]
    info = media.probe(finished)
    assert (info["width"], info["height"], info["video_codec"]) == (1080, 1920, "h264") and info["has_audio"]
    assert 29.5 <= info["duration"] <= 30.5
    clip = conn.execute("SELECT * FROM clips").fetchone()
    assert clip["status"] == "pending_approval" and clip["script"]["captions"]
    assert clip["titles"]["youtube"]["description"].endswith("Video: Xbox México (CC BY 4.0), via Wikimedia Commons")

    # publishing safeguard: an automatically accepted licence cannot be published until a person confirms it
    d = rights.prepublish_gate(conn, clip["id"], "youtube")
    assert not d.ok and any("accepted automatically" in x for x in d.reasons)
    conn.execute("UPDATE rights_ledger SET verified_by = 'owner', verified_at = now() WHERE id = %s", (r["id"],))
    conn.commit()
    assert rights.prepublish_gate(conn, clip["id"], "youtube").ok
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0

    # a second run does not download or clip the same file again
    again = autosource.run(conn, settings, MockLLM(), cfg, commons=commons, sources="commons")
    assert again["processed"] == [] and conn.execute("SELECT count(*) AS n FROM sources").fetchone()["n"] == 1


def test_checksum_mismatch_is_refused(settings, conn, file_server):
    root, base = file_server
    f = make_footage(root / "Tampered.mp4", patterns=["testsrc"], seg_s=4)
    item = commons_item(f, base, "Tampered.mp4", "CC BY 4.0")
    item["sha1"] = "0" * 40
    commons = autosource.Commons("tests@example.invalid", session=CommonsSession([item]))
    out = autosource.run(conn, settings, MockLLM(), {**CFG, "autosource": {**CFG["autosource"], "commons_queries": ["x"]}},
                         commons=commons, sources="commons")
    assert out["processed"] == [] and "checksum" in out["skipped"][0]["reason"]
    assert conn.execute("SELECT count(*) AS n FROM rights_ledger").fetchone()["n"] == 0


def test_own_folder_needs_one_declaration(settings, conn, footage, monkeypatch):
    own = settings.dir("incoming", "own")
    shutil.copy(footage, own / "my_session.mp4")
    monkeypatch.delenv("WIKIMEDIA_CONTACT", raising=False)
    monkeypatch.setenv("VICEPULSE_OWN_FOLDER_DECLARED", "0")
    out = autosource.run(conn, settings, MockLLM(), CFG, sources="own")
    assert out["processed"] == [] and "VICEPULSE_OWN_FOLDER_DECLARED=1" in out["notes"][0]
    monkeypatch.setenv("VICEPULSE_OWN_FOLDER_DECLARED", "1")
    out = autosource.run(conn, settings, MockLLM(), CFG, sources="all", clip_seconds=20)
    assert out["processed"][0]["source"] == "own folder" and len(out["finished_files"]) == 1
    assert any("WIKIMEDIA_CONTACT" in n for n in out["notes"])          # Commons skipped without contact details
    r = conn.execute("SELECT * FROM rights_ledger").fetchone()
    assert r["licence_type"] == "own" and not r["verified_by"].startswith("auto:")
