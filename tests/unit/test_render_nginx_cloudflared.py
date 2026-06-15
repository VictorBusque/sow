"""Tests for NGINX + cloudflared rendering (engine/render.py).

DoD core: each route renders one ``server`` block with the right ``server_name``
and ``listen``; locations are longest-prefix-first; the catch-all vhost (host
omitted) is ``default_server`` with no ``server_name``; upstreams are
``http://127.0.0.1:<port>`` (TCP), ``http://unix:<socket>`` (unix), or
``http://127.0.0.1:<allocated>``; cloudflared config lists every host pointing at
NGINX with a terminal ``http_status:404``.
"""

from __future__ import annotations

import pytest

from outpost.engine.render import (
    RenderError,
    build_nginx_specs,
    render_cloudflared,
    render_nginx,
)
from outpost.models import OutpostConfig
from tests.unit.conftest import minimal_config, minimal_service


def _config_with_routes(
    services: dict, routes: list, exposure: dict | None = None
) -> OutpostConfig:
    cfg = minimal_config(services)
    cfg["routes"] = routes
    if exposure is not None:
        cfg["exposure"] = exposure
    return OutpostConfig.model_validate(cfg)


# ---------------------------------------------------------------------------
# NGINX server blocks
# ---------------------------------------------------------------------------


def test_each_route_becomes_one_server_block() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [
            {"host": "a.example.com", "paths": {"/": {"to": "web"}}},
            {"host": "b.example.com", "paths": {"/": {"to": "web"}}},
        ],
    )
    text = render_nginx(cfg, {})
    assert text.count("server {") == 2
    assert "server_name a.example.com;" in text
    assert "server_name b.example.com;" in text


def test_listen_is_nginx_port_on_every_block() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [{"host": "a.example.com", "paths": {"/": {"to": "web"}}}],
    )
    text = render_nginx(cfg, {})
    assert "listen 127.0.0.1:41999;" in text


def test_catch_all_vhost_is_default_and_has_no_server_name() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [{"paths": {"/": {"to": "web"}}}],  # no host -> catch-all
    )
    specs = build_nginx_specs(cfg, {})
    assert len(specs) == 1
    assert specs[0].is_default is True
    assert specs[0].server_name == ""
    text = render_nginx(cfg, {})
    assert "listen 127.0.0.1:41999 default_server;" in text
    assert "server_name" not in text


def test_literal_vhost_is_not_default() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
    )
    specs = build_nginx_specs(cfg, {})
    assert specs[0].is_default is False
    text = render_nginx(cfg, {})
    assert "default_server" not in text


def test_locations_are_longest_prefix_first() -> None:
    cfg = _config_with_routes(
        {
            "api": minimal_service(git="https://x/api.git", listen="127.0.0.1:18001"),
            "web": minimal_service(git="https://x/web.git", listen="127.0.0.1:8080"),
        },
        [{"host": "app.example.com", "paths": {"/": {"to": "web"}, "/api": {"to": "api"}}}],
    )
    specs = build_nginx_specs(cfg, {})
    locations = specs[0].locations
    assert [loc.prefix for loc in locations] == ["/api", "/"]


def test_tcp_upstream_is_http_host_port() -> None:
    cfg = _config_with_routes(
        {"api": minimal_service(listen="127.0.0.1:18001")},
        [{"host": "app.example.com", "paths": {"/": {"to": "api"}}}],
    )
    assert build_nginx_specs(cfg, {})[0].locations[0].upstream == "http://127.0.0.1:18001"


def test_allocated_port_upstream() -> None:
    cfg = _config_with_routes(
        {"api": minimal_service()},  # no listen -> allocated
        [{"host": "app.example.com", "paths": {"/": {"to": "api"}}}],
    )
    assert (
        build_nginx_specs(cfg, {"api": 18000})[0].locations[0].upstream == "http://127.0.0.1:18000"
    )


def test_unix_socket_upstream() -> None:
    cfg = _config_with_routes(
        {"api": minimal_service(listen="/run/outpost/api.sock")},
        [{"host": "app.example.com", "paths": {"/": {"to": "api"}}}],
    )
    assert (
        build_nginx_specs(cfg, {})[0].locations[0].upstream == "http://unix:/run/outpost/api.sock"
    )


def test_upstream_has_no_trailing_slash_to_preserve_path() -> None:
    cfg = _config_with_routes(
        {"api": minimal_service(listen="127.0.0.1:18001")},
        [{"host": "app.example.com", "paths": {"/api": {"to": "api"}}}],
    )
    upstream = build_nginx_specs(cfg, {})[0].locations[0].upstream
    assert not upstream.endswith("/")  # prefix location + URI-less proxy_pass


def test_wildcard_host_emitted_verbatim() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [{"host": "*.example.com", "paths": {"/": {"to": "web"}}}],
    )
    text = render_nginx(cfg, {})
    assert "server_name *.example.com;" in text


def test_render_nginx_trailing_newline() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [{"host": "a.example.com", "paths": {"/": {"to": "web"}}}],
    )
    text = render_nginx(cfg, {})
    assert text.endswith("}\n")


# ---------------------------------------------------------------------------
# cloudflared config
# ---------------------------------------------------------------------------


def test_cloudflared_lists_all_hosts_pointing_at_nginx() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [
            {"host": "app.example.com", "paths": {"/": {"to": "web"}}},
            {"host": "*.example.com", "paths": {"/": {"to": "web"}}},
        ],
        exposure={
            "cloudflare": {"credentials_file": "~/.cf/app.json", "hosts": ["app.example.com"]}
        },
    )
    text = render_cloudflared(cfg, "tunnel-uuid")
    assert text.splitlines()[0] == "tunnel: tunnel-uuid"
    assert "credentials-file: ~/.cf/app.json" in text
    assert "service: http://127.0.0.1:41999" in text
    # Every exposed host is an ingress hostname.
    assert text.count("hostname: app.example.com") == 1


def test_cloudflared_has_terminal_catchall_404() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
        exposure={
            "cloudflare": {"credentials_file": "~/.cf/app.json", "hosts": ["app.example.com"]}
        },
    )
    text = render_cloudflared(cfg, "t")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # The last service rule must be the http_status:404 catch-all.
    assert lines[-1] == "    - service: http_status:404"


def test_cloudflared_without_exposure_raises() -> None:
    cfg = _config_with_routes(
        {"web": minimal_service(listen="127.0.0.1:8080")},
        [{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
    )
    with pytest.raises(RenderError, match="exposure is not defined"):
        render_cloudflared(cfg, "t")
