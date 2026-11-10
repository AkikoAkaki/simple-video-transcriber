"""Local HTTP API through which the Obsidian plugin and the Explorer menu drive the background service.

It listens on 127.0.0.1 only, and every request must carry the secret from ``appdata.load_api_secret``.
A web page can reach localhost, but it cannot read that file, so it cannot queue files.

The Job fields here are mirrored by obsidian-plugin/src/api.ts.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import appdata
import config
from background.jobs import RETRYABLE
from protocol import STEP_LABELS, overall_progress, step_of

_JOB_FIELDS = ("job_id", "source_path", "status", "message", "output_path", "error",
               "created_at", "finished_at")
_MAX_BODY = 1 << 20
_RECENT_JOBS = 20


def _job(row: dict | None) -> dict | None:
    if not row:
        return None
    return {**{name: row.get(name) for name in _JOB_FIELDS},
            "overall_progress": overall_progress(row["stage"], row["progress"]),
            "step": step_of(row["stage"]),
            "retryable": row["status"] in RETRYABLE}


def snapshot(service) -> dict:
    return {
        "active": _job(service.active_job()),
        "recent": [_job(row) for row in service.recent_jobs(_RECENT_JOBS)],
        "offered": service.offered_recordings(),
        "watcher": {"enabled": service.settings.watcher_enabled, "dir": service.settings.watch_dir},
        "steps": STEP_LABELS,
    }


def _routes(service) -> dict:
    def queued(*rows):
        return {"queued": [row["job_id"] for row in rows if row]}

    return {
        ("GET", r"/api/state"): lambda body: snapshot(service),
        ("POST", r"/api/jobs"): lambda body: queued(*service.add_files(body.get("paths") or [])),
        ("POST", r"/api/jobs/([\w-]+)/retry"): lambda body, job_id: queued(service.retry_job(job_id)),
        ("POST", r"/api/cancel"): lambda body: service.cancel_active() or {},
        ("POST", r"/api/recordings/accept"): lambda body: queued(service.accept_recording(str(body.get("path", "")))),
        ("POST", r"/api/recordings/dismiss"): lambda body: service.dismiss_recording(str(body.get("path", ""))) or {},
        ("POST", r"/api/watcher"): lambda body: service.set_watcher_enabled(bool(body.get("enabled"))) or {},
        ("POST", r"/api/token"): lambda body: service.set_token(str(body.get("token", ""))) or {},
    }


def _handler(service, secret: str):
    routes = [(method, re.compile(pattern + "$"), action) for (method, pattern), action in _routes(service).items()]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied.encode(), secret.encode()):
                return self._reply(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong API secret"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                return self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._reply(HTTPStatus.BAD_REQUEST, {"error": "body is not JSON"})
            for route_method, pattern, action in routes:
                match = pattern.match(self.path)
                if match and route_method == method:
                    try:
                        return self._reply(HTTPStatus.OK, action(body, *match.groups()))
                    except (ValueError, OSError) as exc:
                        return self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            self._reply(HTTPStatus.NOT_FOUND, {"error": f"no route for {method} {self.path}"})

        def _reply(self, status: HTTPStatus, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


class ApiServer:
    def __init__(self, service):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", config.API_PORT),
                                         _handler(service, appdata.load_api_secret(create=True)))
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="local-api", daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        appdata.publish_api_port(self.port)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
