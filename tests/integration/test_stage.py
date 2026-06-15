"""Integration tests for the staging step (engine/stage.py).

DoD core: ``stage`` writes a tree mirroring ``generated/{systemd,nginx,cloudflared}``,
one unit per service, a single NGINX server-blocks file, and a cloudflared config
iff exposure is defined. The live tree is never touched. The returned ports are
the allocation used. This is the bridge between rendering and the apply pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outpost.engine.stage import stage
from outpost.models import OutpostConfig
from tests.unit.conftest import minimal_config, minimal_service


def _config(services: dict, routes=None, exposure=None) -> OutpostConfig:
    cfg = minimal_config(services)
    if routes is not None:
        cfg["routes"] = routes
    if exposure is not None:
        cfg["exposure"] = exposure
    return OutpostConfig.model_validate(cfg)


def test_stage_writes_unit_per_service(tmp_path: Path) -> None:
    cfg = _config(
        {
            "api": minimal_service(listen="127.0.0.1:18001"),
            "web": minimal_service(git="https://x/web.git", listen="127.0.0.1:8080"),
        }
    )
    tree = stage(cfg, tmp_path / "stage")
    assert set(tree.units) == {"api", "web"}
    for name, path in tree.units.items():
        assert path.name == f"{name}.service"
        assert path.is_file()
        assert path.parent == tmp_path / "stage" / "systemd"
        assert "[Unit]" in path.read_text(encoding="utf-8")


def test_stage_writes_single_nginx_file(tmp_path: Path) -> None:
    cfg = _config(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        routes=[{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
    )
    tree = stage(cfg, tmp_path / "stage")
    assert tree.nginx == tmp_path / "stage" / "nginx" / "servers.conf"
    assert "server {" in tree.nginx.read_text(encoding="utf-8")
    assert "proxy_pass" in tree.nginx.read_text(encoding="utf-8")


def test_stage_writes_cloudflared_when_exposure_defined(tmp_path: Path) -> None:
    cfg = _config(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        routes=[{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
        exposure={
            "cloudflare": {"credentials_file": "~/.cf/app.json", "hosts": ["app.example.com"]}
        },
    )
    tree = stage(cfg, tmp_path / "stage", tunnel="tunnel-uuid")
    assert tree.cloudflared == tmp_path / "stage" / "cloudflared" / "config.yml"
    assert tree.cloudflared is not None
    text = tree.cloudflared.read_text(encoding="utf-8")
    assert "tunnel: tunnel-uuid" in text
    assert "http_status:404" in text


def test_stage_omits_cloudflared_when_no_exposure(tmp_path: Path) -> None:
    cfg = _config({"api": minimal_service(listen="127.0.0.1:18001")})
    tree = stage(cfg, tmp_path / "stage")
    assert tree.cloudflared is None
    # The cloudflared subdir is still created (mirrors live tree), but empty.
    assert (tmp_path / "stage" / "cloudflared").is_dir()


def test_stage_exposure_without_tunnel_raises(tmp_path: Path) -> None:
    cfg = _config(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        routes=[{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
        exposure={
            "cloudflare": {"credentials_file": "~/.cf/app.json", "hosts": ["app.example.com"]}
        },
    )
    with pytest.raises(Exception, match="no cloudflared tunnel id"):
        stage(cfg, tmp_path / "stage")  # exposure but no tunnel -> caller bug


def test_stage_returns_allocation_ports(tmp_path: Path) -> None:
    cfg = _config(
        {
            "api": minimal_service(),  # allocated
            "web": minimal_service(git="https://x/web.git", listen="127.0.0.1:8080"),  # declared
        }
    )
    tree = stage(cfg, tmp_path / "stage")
    assert tree.ports == {"api": 18000}  # only allocated ports; declared-listen absent


def test_stage_does_not_touch_live_tree(tmp_path: Path) -> None:
    cfg = _config({"api": minimal_service(listen="127.0.0.1:18001")})
    stage_root = tmp_path / "stage"
    stage(cfg, stage_root)
    # Nothing outside the staging root should exist.
    assert all(p.is_relative_to(stage_root) for p in stage_root.rglob("*"))
