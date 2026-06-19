"""Startup health gate — stdlib HTTP/TCP/unix probes with timeout-polling.

Health in v1 is a **startup check only** (prd.md §5): ``apply`` probes each
service's *local listener* (never NGINX) right after start/restart and fails the
apply — triggering rollback — if the probe doesn't pass within ``health.timeout``.
There are no passive/active probes here; that's a deferred concern.

This module is deliberately dependency-free: ``urllib.request`` for HTTP, the
``socket`` module for TCP and unix-socket connects. A failed attempt (connection
refused mid-startup, a hung accept, a 5xx) returns ``False`` and the caller polls
again — a service coming up is expected to refuse for the first few attempts.

The caller resolves the listener address (declared ``listen`` or allocated port)
and passes it in. Two shapes: a TCP ``(host, port)`` pair, or a unix ``socket``
path. HTTP health over a unix socket is supported (a raw HTTP/1.0 request over
``AF_UNIX``) — common for app servers that serve HTTP on a socket.
"""

from __future__ import annotations

import socket
import time
import urllib.request

from outpost.models import Health

__all__ = ["HealthCheckError", "check_once", "wait_for"]

# Poll cadence and per-attempt cap. A single hung connect must not eat the whole
# timeout budget, so each attempt gets its own short socket timeout.
_POLL_INTERVAL: float = 1.0
_ATTEMPT_TIMEOUT: float = 2.0


class HealthCheckError(Exception):
    """Raised when a service's health gate does not pass within its timeout."""


def wait_for(
    health: Health,
    *,
    host: str | None = None,
    port: int | None = None,
    socket_path: str | None = None,
    timeout: float | None = None,
) -> None:
    """Poll the probe until it passes or the timeout elapses.

    ``timeout`` defaults to ``health.timeout`` (seconds). Raises
    :class:`HealthCheckError` on timeout — the signal ``apply`` catches to roll
    back. A passing first attempt returns immediately (happy path is fast).
    """
    deadline = time.monotonic() + (health.timeout if timeout is None else timeout)
    while True:
        if check_once(health, host=host, port=port, socket_path=socket_path):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HealthCheckError(_describe(health, host, port, socket_path))
        time.sleep(min(_POLL_INTERVAL, remaining))


def check_once(
    health: Health,
    *,
    host: str | None = None,
    port: int | None = None,
    socket_path: str | None = None,
) -> bool:
    """One probe attempt. ``True`` on pass, ``False`` on any connection/HTTP miss.

    Network errors (refused, timed out, DNS) are swallowed into ``False``: a
    service still starting up is the expected cause, not an apply-ending error.
    Only the timeout in :func:`wait_for` turns repeated misses into a failure.
    """
    try:
        if health.is_http:
            return _http(health, host=host, port=port, socket_path=socket_path)
        return _tcp(host=host, port=port, socket_path=socket_path)
    except OSError:
        # Refused/timed-out/unreachable — not ready yet (or genuinely down).
        return False


def _http(
    health: Health,
    *,
    host: str | None,
    port: int | None,
    socket_path: str | None,
) -> bool:
    path = health.http.path if health.http is not None else "/"
    if socket_path is not None:
        return _http_unix(path, socket_path)
    if host is None or port is None:
        raise HealthCheckError("http health requires a TCP host:port listener")
    url = f"http://{host}:{port}{path}"
    # urllib follows 3xx and raises HTTPError (an OSError subclass) on >=400,
    # so a 2xx/3xx result maps to True and everything else to a caught miss.
    with urllib.request.urlopen(url, timeout=_ATTEMPT_TIMEOUT) as resp:
        return 200 <= resp.status < 400


def _http_unix(path: str, socket_path: str) -> bool:
    """A minimal HTTP/1.0 GET over a unix socket — enough to read the status line."""
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.settimeout(_ATTEMPT_TIMEOUT)
        sock.connect(socket_path)
        request = (
            f"GET {path} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        sock.sendall(request)
        head = b""
        while b"\r\n" not in head and len(head) < 1024:
            chunk = sock.recv(128)
            if not chunk:
                break
            head += chunk
    finally:
        sock.close()
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status_line.split(" ", 2)
    if len(parts) < 2:
        return False
    return parts[1].startswith("2") or parts[1].startswith("3")


def _tcp(*, host: str | None, port: int | None, socket_path: str | None) -> bool:
    if socket_path is not None:
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.settimeout(_ATTEMPT_TIMEOUT)
            sock.connect(socket_path)
        finally:
            sock.close()
        return True
    if host is None or port is None:
        raise HealthCheckError("tcp health requires a TCP host:port listener")
    sock = socket.create_connection((host, port), timeout=_ATTEMPT_TIMEOUT)
    sock.close()
    return True


def _describe(
    health: Health,
    host: str | None,
    port: int | None,
    socket_path: str | None,
) -> str:
    where = socket_path if socket_path is not None else f"{host}:{port}"
    kind = "http" if health.is_http else "tcp"
    return f"{kind} health check failed at {where} within {health.timeout}s"
