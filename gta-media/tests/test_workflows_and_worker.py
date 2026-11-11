"""n8n workflow exports are consistent, and the worker HTTP API runs only CLI commands, with a token."""

import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vicepulse import cli, worker
from vicepulse.config import PACKAGE_ROOT

WORKFLOWS = sorted((PACKAGE_ROOT / "n8n" / "workflows").glob("*.json"))
COMMANDS = set(cli.build_parser()._subparsers._group_actions[0].choices)


def test_ten_workflows_match_plan():
    assert len(WORKFLOWS) == 10
    names = [json.loads(p.read_text())["name"] for p in WORKFLOWS]
    for expected in ("Ingest and rights gate", "Signal extraction", "Select, script and render", "Approval queue",
                     "Publisher", "Analytics pull", "Trend and topic scan", "Creator prospecting", "Takedown",
                     "Health check and alerts"):
        assert any(expected in n for n in names), expected


def test_workflow_files_are_up_to_date(tmp_path):
    """The committed JSON must be what build_workflows.py produces."""
    before = {p.name: p.read_text() for p in WORKFLOWS}
    subprocess.run([sys.executable, str(PACKAGE_ROOT / "n8n" / "build_workflows.py")], check=True)
    after = {p.name: p.read_text() for p in sorted((PACKAGE_ROOT / "n8n" / "workflows").glob("*.json"))}
    assert before == after


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.stem for p in WORKFLOWS])
def test_workflow_structure(path):
    wf = json.loads(path.read_text())
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    assert wf["active"] is False                                   # nothing switches itself on after import
    assert "tags" not in wf                                        # shared tags break a multi-file n8n import
    triggers = [n for n in wf["nodes"] if n["type"].endswith(("Trigger", "webhook", "formTrigger"))]
    assert triggers, "every workflow needs a trigger"
    for src, outs in wf["connections"].items():
        assert src in names
        for branch in outs["main"]:
            for target in branch:
                assert target["node"] in names
    reachable, todo = set(), [t["name"] for t in triggers]
    while todo:
        n = todo.pop()
        if n in reachable:
            continue
        reachable.add(n)
        for branch in wf["connections"].get(n, {}).get("main", []):
            todo.extend(t["node"] for t in branch)
    assert reachable == set(names), f"unconnected nodes: {set(names) - reachable}"
    worker_calls = [n for n in wf["nodes"] if n["parameters"].get("url") == "http://worker:8088/run"]
    for n in worker_calls:
        body = n["parameters"]["jsonBody"]
        assert n["credentials"]["httpHeaderAuth"]["name"] == "VicePulse worker"
        if body.startswith("="):
            if "$json.args" in body:
                continue                                           # args built by the Code node before it
            command = re.search(r'args: \["([\w-]+)"', body).group(1)
        else:
            command = json.loads(body)["args"][0]
        assert command in COMMANDS, command
    for n in wf["nodes"]:
        if n["type"] == "n8n-nodes-base.telegram":
            assert n.get("disabled") is True, "Telegram nodes must start disabled (no Telegram credential yet)"
    text = path.read_text()
    assert "$vars" not in text, "n8n Variables are a paid feature; use placeholders or credentials"
    assert not re.search(r"(sk-ant-|EAA[A-Za-z0-9]{20}|ya29\.)", text), "looks like a real secret"


def test_code_nodes_only_call_known_commands():
    for path in WORKFLOWS:
        for n in json.loads(path.read_text())["nodes"]:
            js = n["parameters"].get("jsCode", "")
            for cmd in re.findall(r"args(?: =|:) \['([\w-]+)'", js):
                assert cmd in COMMANDS, (path.name, cmd)


@pytest.fixture()
def server(settings, conn):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), worker.make_handler(settings))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def post(url, body, token):
    req = urllib.request.Request(url + "/run", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_worker_requires_token_and_runs_cli(server):
    assert post(server, {"args": ["health"]}, None)[0] == 401
    assert post(server, {"args": ["health"]}, "wrong")[0] == 401
    code, body = post(server, {"args": ["health"]}, "test-token")
    assert code == 200 and body["ok"] and body["live_publish"] is False and body["llm_mode"] == "mock"
    assert post(server, {"args": ["serve"]}, "test-token")[0] == 500
    assert post(server, {"args": ["rm", "-rf", "/"]}, "test-token")[0] == 400        # not a vicepulse command
    code, body = post(server, {"args": ["queue"]}, "test-token")
    assert code == 200 and body["pending"] == []


def test_cli_end_to_end_commands(settings, conn, footage, monkeypatch, capsys):
    """The CLI that n8n calls: migrate, rights, ingest, signals, daily, queue, all print JSON."""
    def run(*args):
        assert cli.main(list(args)) == 0
        return json.loads(capsys.readouterr().out)

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert run("migrate")["channels"] == ["en", "br"]
    rid = run("rights-add", "--json", json.dumps({"owner_name": "VicePulse", "licence_type": "own",
                                                   "platforms": ["youtube", "instagram", "tiktok"],
                                                   "starts_on": "2026-01-01"}))["rights_id"]
    assert run("ingest", "--file", str(footage), "--origin", "own_recording", "--rights-id", str(rid))["status"] == "quarantined"
    verified = run("rights-verify", "--id", str(rid), "--by", "owner")
    assert verified["verified"] and verified["requarantine_check"][0]["ok"]
    assert run("signals")["sources"][0]["candidates"] > 0
    assert cli.main(["takedown", "--reason", "x"]) == 1                                # needs a clip or a source
    assert "give exactly one" in capsys.readouterr().err
    out = run("topics-add", "--json", json.dumps([{"topic": "GTA 6 map leak rumours", "language": "en"}]))
    assert out["topics"] == 1
