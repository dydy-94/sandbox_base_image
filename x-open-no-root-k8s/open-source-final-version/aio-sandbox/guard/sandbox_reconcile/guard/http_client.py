from __future__ import annotations

"""Small standard-library HTTP JSON helpers."""

import json
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HttpJsonError(RuntimeError):
    pass


def http_get_json(url: str, timeout_seconds: float, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    target = str(url or "").strip()
    if not target:
        raise HttpJsonError("http json url is empty")
    request_headers = {"Accept": "application/json"}
    if isinstance(headers, dict):
        for key, value in headers.items():
            header_key = str(key or "").strip()
            if header_key:
                request_headers[header_key] = str(value)
    req = Request(target, method="GET", headers=request_headers)
    try:
        with urlopen(req, timeout=float(timeout_seconds)) as resp:
            status = int(getattr(resp, "status", 200))
            if status < 200 or status >= 300:
                raise HttpJsonError(f"http status {status}")
            raw = resp.read()
    except HTTPError as exc:
        raise HttpJsonError(f"http status {exc.code}") from exc
    except URLError as exc:
        raise HttpJsonError(f"http request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpJsonError("http request timed out") from exc
    except Exception as exc:
        raise HttpJsonError(f"http request failed: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HttpJsonError(f"http response json parse failed: {exc}") from exc
    if not isinstance(data, dict):
        raise HttpJsonError("http response json must be object")
    return data


def http_post_json(url: str, payload: Any, timeout_seconds: float) -> dict[str, Any]:
    target = str(url or "").strip()
    if not target:
        raise HttpJsonError("http json url is empty")
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = Request(
        target,
        data=raw_body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urlopen(req, timeout=float(timeout_seconds)) as resp:
            status = int(getattr(resp, "status", 200))
            if status < 200 or status >= 300:
                raise HttpJsonError(f"http status {status}")
            raw = resp.read()
    except HTTPError as exc:
        raise HttpJsonError(f"http status {exc.code}") from exc
    except URLError as exc:
        raise HttpJsonError(f"http request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpJsonError("http request timed out") from exc
    except Exception as exc:
        raise HttpJsonError(f"http request failed: {exc}") from exc
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def http_post_empty(
    url: str,
    timeout_seconds: float,
    max_response_bytes: int = 4096,
) -> tuple[int, str]:
    """POST an empty body and return the HTTP status and a bounded response body.

    HTTP error responses are returned to the caller so they can be logged. Network
    and timeout failures raise ``HttpJsonError``.
    """
    target = str(url or "").strip()
    if not target:
        raise HttpJsonError("http url is empty")
    try:
        timeout = min(3.0, max(0.1, float(timeout_seconds)))
    except Exception:
        timeout = 3.0
    try:
        response_limit = max(0, int(max_response_bytes))
    except Exception:
        response_limit = 4096

    req = Request(
        target,
        data=b"",
        method="POST",
        headers={"Accept": "*/*", "Content-Length": "0"},
    )
    status = 0
    raw = b""
    truncated = False
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read(response_limit + 1)
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read(response_limit + 1)
    except URLError as exc:
        raise HttpJsonError(f"http request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpJsonError("http request timed out") from exc
    except Exception as exc:
        raise HttpJsonError(f"http request failed: {exc}") from exc

    if len(raw) > response_limit:
        raw = raw[:response_limit]
        truncated = True
    body = raw.decode("utf-8", errors="replace")
    if truncated:
        body += "...<truncated>"
    return status, body


def http_post_json_no_response(url: str, payload: dict[str, Any], timeout_seconds: float) -> None:
    """Best-effort JSON POST that writes the request and does not read the response."""
    target = str(url or "").strip()
    if not target:
        raise HttpJsonError("http json url is empty")
    parts = urlsplit(target)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise HttpJsonError(f"unsupported http url: {target}")

    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = parts.hostname
    host_header = host if parts.port is None else f"{host}:{port}"
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Accept: application/json\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(raw_body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + raw_body

    sock = socket.create_connection((host, port), timeout=float(timeout_seconds))
    try:
        sock.settimeout(float(timeout_seconds))
        if parts.scheme == "https":
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=host) as wrapped:
                wrapped.sendall(request)
        else:
            sock.sendall(request)
    finally:
        try:
            sock.close()
        except Exception:
            pass
