from __future__ import annotations

import logging
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from .environment import SandboxContextError, SandboxContextLoader
from .policy import authorize
from .verifier import TrustedTokenVerifier


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18081


class RateLimitedLogger:
    def __init__(self, interval_seconds: float = 10.0) -> None:
        self._interval_seconds = interval_seconds
        self._next_log_at: dict[str, float] = {}

    def warning(self, reason: str, status: int) -> None:
        now = time.monotonic()
        if now < self._next_log_at.get(reason, 0.0):
            return
        self._next_log_at[reason] = now + self._interval_seconds
        logging.getLogger("sandbox-port-auth").warning(
            "port authorization rejected: status=%s reason=%s", status, reason
        )


class PortAuthServer(HTTPServer):
    allow_reuse_address = True
    request_queue_size = 256

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, PortAuthHandler)
        self.context_loader = SandboxContextLoader()
        self.token_verifier = TrustedTokenVerifier.from_path()
        self.rejection_logger = RateLimitedLogger()


class PortAuthHandler(BaseHTTPRequestHandler):
    server_version = "SandboxPortAuth/1.0"
    sys_version = ""

    def _handle(self) -> None:
        if self.path == "/health":
            self._respond(204)
            return
        if self.path != "/auth":
            self._respond(404)
            return

        try:
            context = self.server.context_loader.get()  # type: ignore[attr-defined]
        except SandboxContextError as exc:
            self.server.rejection_logger.warning(exc.reason, 503)  # type: ignore[attr-defined]
            self._respond(503)
            return
        result = authorize(
            context,
            self.headers.get("Id-Token"),
            self.server.token_verifier,  # type: ignore[attr-defined]
        )
        if result.status != 204:
            self.server.rejection_logger.warning(  # type: ignore[attr-defined]
                result.reason, result.status
            )
        self._respond(result.status)

    def _respond(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PORT_AUTH_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s sandbox-port-auth %(message)s",
    )
    server = PortAuthServer((LISTEN_HOST, LISTEN_PORT))
    logging.getLogger("sandbox-port-auth").info(
        "listening on http://%s:%s", LISTEN_HOST, LISTEN_PORT
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
