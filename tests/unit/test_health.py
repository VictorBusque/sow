"""Unit tests for the startup health probes (engine/health.py).

The probes are non-trivial (HTTP/TCP/unix, timeout-polling), so each gets a fast
check against a real ephemeral listener rather than a mock. Connection-refused
cases use a port with no listener, which yields an OSError the probe swallows
into ``False`` — the exact failure mode apply's gate relies on.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from socket import AF_INET, AF_UNIX, SOCK_STREAM

import pytest

from outpost.engine.health import HealthCheckError, check_once, wait_for
from outpost.models import Health, HttpHealth

_HTTP = Health(http=HttpHealth(path="/healthz"), timeout=1)
_TCP = Health(tcp=True, timeout=1)


def _serve_http(port: int, status: bytes) -> socket.socket:
    """A one-shot-per-connection HTTP/1.0 server on 127.0.0.1:port."""
    sock = socket.socket(AF_INET, SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    sock.listen(8)

    def serve() -> None:
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.0 " + status + b"\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock


def test_http_probe_passes_on_2xx() -> None:
    sock = _serve_http(0, b"204 No Content")
    port = sock.getsockname()[1]
    try:
        assert check_once(_HTTP, host="127.0.0.1", port=port) is True
    finally:
        sock.close()


def test_http_probe_fails_on_5xx() -> None:
    sock = _serve_http(0, b"500 Internal Server Error")
    port = sock.getsockname()[1]
    try:
        assert check_once(_HTTP, host="127.0.0.1", port=port) is False
    finally:
        sock.close()


def test_http_probe_fails_on_connection_refused() -> None:
    # An ephemeral port with no listener -> refused -> False (not an exception).
    assert check_once(_HTTP, host="127.0.0.1", port=1) is False


def test_tcp_probe_passes_when_listening() -> None:
    sock = socket.socket(AF_INET, SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert check_once(_TCP, host="127.0.0.1", port=port) is True
    finally:
        sock.close()


def test_tcp_probe_fails_on_connection_refused() -> None:
    assert check_once(_TCP, host="127.0.0.1", port=1) is False


def _bind_unix(path: str) -> socket.socket:
    """Bind a unix socket, removing a stale leftover file first."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    sock = socket.socket(AF_UNIX)
    sock.bind(path)
    return sock


def test_unix_tcp_probe_passes() -> None:
    path = "/tmp/outpost-test-tcp.sock"
    try:
        sock = _bind_unix(path)
    except OSError:
        sock.close()
        pytest.skip("cannot bind unix socket")
    sock.listen(1)
    try:
        assert check_once(_TCP, socket_path=path) is True
    finally:
        sock.close()


def test_unix_http_probe_passes() -> None:
    path = "/tmp/outpost-test-http.sock"
    try:
        sock = _bind_unix(path)
    except OSError:
        sock.close()
        pytest.skip("cannot bind unix socket")
    sock.listen(2)

    def serve() -> None:
        conn, _ = sock.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        assert check_once(_HTTP, socket_path=path) is True
    finally:
        sock.close()


def test_wait_for_returns_on_first_pass() -> None:
    sock = socket.socket(AF_INET, SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        # No polling: a passing first attempt returns immediately.
        wait_for(_TCP, host="127.0.0.1", port=port, timeout=5)
    finally:
        sock.close()


def test_wait_for_raises_on_timeout() -> None:
    with pytest.raises(HealthCheckError, match="tcp health check failed"):
        wait_for(_TCP, host="127.0.0.1", port=1, timeout=1)
