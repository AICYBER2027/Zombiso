"""Small HTTP wrapper around the CLI, for n8n.

n8n runs in its own container without Python or FFmpeg, so its workflows call this worker:

    POST /run   {"args": ["daily", "--channel", "en"]}     ->  the CLI's JSON output
    GET  /healthz

Only ``vicepulse`` subcommands can be run (the body is passed to the CLI parser, never to a
shell). Requests need ``Authorization: Bearer $VICEPULSE_WORKER_TOKEN``. Bind it to the
private docker network only; do not expose the port to the internet.
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Settings

BLOCKED_OVER_HTTP = {"serve"}


def make_handler(settings: Settings):
    from . import cli

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, default=cli._json_default, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._send(200, {"ok": True}) if self.path == "/healthz" else self._send(404, {"ok": False})

        def do_POST(self):  # noqa: N802
            if self.path != "/run":
                return self._send(404, {"ok": False, "error": "not found"})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not settings.worker_token or not hmac.compare_digest(token, settings.worker_token):
                return self._send(401, {"ok": False, "error": "unauthorized"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                args = [str(a) for a in body["args"]]
                if not args or args[0] in BLOCKED_OVER_HTTP:
                    raise ValueError("command not allowed")
                result = cli.run(args, settings)
            except SystemExit:
                return self._send(400, {"ok": False, "error": "bad arguments"})
            except Exception as e:
                return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            self._send(200, {"ok": True, **result})

        def log_message(self, fmt, *args):  # keep tokens and bodies out of logs
            pass

    return Handler


def serve(settings: Settings, host: str = "0.0.0.0") -> None:  # pragma: no cover - long-running
    if not settings.worker_token:
        raise RuntimeError("VICEPULSE_WORKER_TOKEN must be set before starting the worker")
    ThreadingHTTPServer((host, settings.worker_port), make_handler(settings)).serve_forever()
