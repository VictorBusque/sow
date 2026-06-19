"""Tests for config-editing functions (add/remove service, add/remove route).

One compact test per operation verifying the happy path and a conflict error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sow.config import (
    ConfigError,
    add_route,
    add_service,
    remove_route,
    remove_service,
)

_MINIMAL = """\
version: 1
services:
  api:
    source: {git: https://x.git, sha: abc1234, ref: main}
    command: ./run
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text)


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# ── add_service ────────────────────────────────────────────────────────────


def test_add_service_happy(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    add_service(cfg, "web", git="https://g.git", command="./serve")
    raw = _read(cfg)
    assert "web" in raw["services"]
    assert raw["services"]["web"]["command"] == "./serve"


def test_add_service_duplicate(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    with pytest.raises(ConfigError, match="already exists"):
        add_service(cfg, "api", git="https://x.git", command="./x")


def test_add_service_with_env(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    add_service(cfg, "db", git="https://g.git", command="./db", env={"PORT": "5432"})
    raw = _read(cfg)
    assert raw["services"]["db"]["environment"] == {"PORT": "5432"}


def test_add_service_invalid_result(tmp_path: Path) -> None:
    """Adding a service with a duplicate listen port must fail validation."""
    cfg = tmp_path / "sow.yaml"
    _write(
        cfg,
        "version: 1\n"
        "services:\n"
        "  api: {source: {git: https://x.git, sha: abc1234, ref: main}, command: ./run, listen: 127.0.0.1:8080}\n",
    )
    with pytest.raises(ConfigError, match="invalid config after add"):
        add_service(cfg, "dup", git="https://g.git", command="./x", listen="127.0.0.1:8080")
    # Config must be unchanged after failed add.
    raw = _read(cfg)
    assert "dup" not in raw["services"]


# ── remove_service ─────────────────────────────────────────────────────────


def test_remove_service_happy(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    remove_service(cfg, "api")
    raw = _read(cfg)
    assert "api" not in raw.get("services", {})


def test_remove_service_not_found(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    with pytest.raises(ConfigError, match="not found"):
        remove_service(cfg, "ghost")


def test_remove_service_cleans_routes(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(
        cfg,
        "version: 1\n"
        "services:\n"
        "  api: {source: {git: https://x.git, sha: abc1234, ref: main}, command: ./run}\n"
        "routes:\n"
        "  - host: x.com\n"
        "    paths: {'/': {to: api}, '/other': {to: api}}\n",
    )
    remove_service(cfg, "api")
    raw = _read(cfg)
    assert "routes" not in raw  # all route targets removed = no routes left


# ── add_route ──────────────────────────────────────────────────────────────


def test_add_route_new_host(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    add_route(cfg, "app.example.com", "/", "api")
    raw = _read(cfg)
    assert raw["routes"][0]["host"] == "app.example.com"
    assert raw["routes"][0]["paths"]["/"]["to"] == "api"


def test_add_route_existing_host(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(
        cfg,
        "version: 1\n"
        "services:\n"
        "  api: {source: {git: https://x.git, sha: abc1234, ref: main}, command: ./run}\n"
        "routes:\n"
        "  - host: x.com\n"
        "    paths:\n"
        "      '/': {to: api}\n",
    )
    add_route(cfg, "x.com", "/api", "api")
    raw = _read(cfg)
    paths = raw["routes"][0]["paths"]
    assert "/" in paths and "/api" in paths


def test_add_route_duplicate_prefix(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    add_route(cfg, "h.com", "/", "api")
    with pytest.raises(ConfigError, match="already exists"):
        add_route(cfg, "h.com", "/", "api")


# ── remove_route ───────────────────────────────────────────────────────────


def test_remove_route_prefix(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(
        cfg,
        "version: 1\n"
        "services:\n"
        "  api: {source: {git: https://x.git, sha: abc1234, ref: main}, command: ./run}\n"
        "routes:\n"
        "  - host: x.com\n"
        "    paths:\n"
        "      '/': {to: api}\n"
        "      '/api': {to: api}\n",
    )
    remove_route(cfg, "x.com", "/api")
    raw = _read(cfg)
    paths = raw["routes"][0]["paths"]
    assert "/" in paths
    assert "/api" not in paths


def test_remove_route_whole_host(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(
        cfg,
        "version: 1\n"
        "services:\n"
        "  api: {source: {git: https://x.git, sha: abc1234, ref: main}, command: ./run}\n"
        "routes:\n"
        "  - host: x.com\n"
        "    paths: {'/': {to: api}}\n"
        "  - host: y.com\n"
        "    paths: {'/': {to: api}}\n",
    )
    remove_route(cfg, "x.com")
    raw = _read(cfg)
    assert len(raw["routes"]) == 1
    assert raw["routes"][0]["host"] == "y.com"


def test_remove_route_not_found(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    _write(cfg, _MINIMAL)
    with pytest.raises(ConfigError, match="no routes defined"):
        remove_route(cfg, "ghost.com")
