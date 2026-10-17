from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config, db, jobs, preferences, rpc

LOG = logging.getLogger("topshot-engine")

TOKEN = os.environ.get("TOPSHOT_TOKEN") or secrets.token_urlsafe(24)


class Handler(BaseHTTPRequestHandler):
    server_version = "TopshotEngine/" + __version__

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Topshot-Token")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = self.headers.get("X-Topshot-Token")
        if not token:
            values = query.get("token")
            token = values[0] if values else None
        return bool(token) and secrets.compare_digest(token, TOKEN)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._json(200, {"ok": True, "version": __version__, "jobs": len(jobs.manager.jobs)})
            return
        if parsed.path.startswith("/proxy/"):
            if not self._authorized(query):
                self._json(403, {"error": "forbidden"})
                return
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3:
                self._json(404, {"error": "not found"})
                return
            try:
                file_id = int(parts[1])
            except ValueError:
                self._json(404, {"error": "not found"})
                return
            variant = parts[2]
            suffix = {"thumb": "_t.jpg", "preview": "_p.jpg"}.get(variant)
            if suffix is None:
                self._json(404, {"error": "not found"})
                return
            conn = db.connect()
            try:
                row = conn.execute("SELECT project_id FROM files WHERE id=?", (file_id,)).fetchone()
            finally:
                conn.close()
            if row is None:
                self._json(404, {"error": "not found"})
                return
            path = config.proxy_dir(int(row["project_id"])) / f"{file_id}{suffix}"
            if not path.is_file():
                self._json(404, {"error": "proxy not ready"})
                return
            try:
                data = path.read_bytes()
            except OSError as exc:
                self._json(500, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/rpc":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized(parse_qs(parsed.query)):
            self._json(403, {"error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return
        request_id = request.get("id")
        method = request.get("method") or ""
        params = request.get("params") or {}
        if not isinstance(params, dict):
            self._json(
                200,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "params must be an object"}},
            )
            return
        try:
            result = rpc.handle(method, params)
            self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except rpc.MethodNotFound:
            self._json(
                200,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}},
            )
        except (ValueError, KeyError, TypeError) as exc:
            self._json(
                200,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}},
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("RPC failure: %s", method)
            self._json(
                200,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}},
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Topshot local engine")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_db()
    db.mark_interrupted_projects()
    seed_conn = db.connect()
    try:
        preferences.seed_existing(seed_conn, 1)
    finally:
        seed_conn.close()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    readiness = {"port": port, "token": TOKEN, "pid": __import__("os").getpid(), "version": __version__}
    print(json.dumps(readiness), flush=True)
    LOG.info("Topshot engine listening on %s:%s", args.host, port)

    def shutdown(signum, frame) -> None:  # noqa: ARG001
        LOG.info("Signal %s received, shutting down", signum)
        jobs.manager.stop_all()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
