"""Tiny HTTP endpoint for the sim-engine: GET /metrics, GET /health, GET|POST /speed?x=N.

Stdlib ThreadingHTTPServer in a daemon thread (no extra framework in the engine process).
/health answers 200 for ok/degraded and 503 for failing.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from vlsim.ops.health import HealthChecker
from vlsim.ops.metrics import SimMetrics

MIN_SPEED, MAX_SPEED = 1.0, 1000.0


class OpsServer:
    def __init__(
        self,
        *,
        port: int,
        metrics: SimMetrics,
        health: HealthChecker,
        get_speed: Callable[[], float],
        set_speed: Callable[[float], None],
        host: str = "0.0.0.0",  # served inside the container network
    ) -> None:
        ops = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                ops._route(self, "GET")

            def do_POST(self) -> None:
                ops._route(self, "POST")

        self.metrics, self.health = metrics, health
        self.get_speed, self.set_speed = get_speed, set_speed
        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _route(self, h: Any, method: str) -> None:
        url = urlparse(h.path)
        if url.path == "/metrics" and method == "GET":
            h._send(200, self.metrics.exposition(), "text/plain; version=0.0.4")
        elif url.path == "/health" and method == "GET":
            st = self.health.status()
            h._send(503 if st["status"] == "failing" else 200, json.dumps(st).encode())
        elif url.path == "/speed":
            if method == "POST":
                try:
                    value = float(parse_qs(url.query)["x"][0])
                    if not MIN_SPEED <= value <= MAX_SPEED:
                        raise ValueError
                except (KeyError, ValueError):
                    h._send(400, b'{"error": "x must be a number from 1 to 1000"}')
                    return
                self.set_speed(value)
            h._send(200, json.dumps({"speed": self.get_speed()}).encode())
        else:
            h._send(404, b'{"error": "not found"}')

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
