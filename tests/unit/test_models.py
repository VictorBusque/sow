"""Unit tests for the Pydantic models and every config-schema validation rule.

Each validation rule in ``config-schema.md`` §"Validation rules" gets at least
one passing and one failing test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from outpost.models import OutpostConfig
from tests.unit.conftest import minimal_config, minimal_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse(cfg: dict) -> OutpostConfig:
    return OutpostConfig.model_validate(cfg)


def assert_invalid(cfg: dict, *, contains: str | None = None) -> None:
    with pytest.raises(ValidationError) as exc_info:
        parse(cfg)
    if contains is not None:
        rendered = []
        for e in exc_info.value.errors():
            loc = ".".join(str(x) for x in e["loc"])
            rendered.append(f"{loc} {e['msg']}")
        joined = " ".join(rendered)
        assert contains in joined, f"expected {contains!r} in errors, got: {joined}"


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version_must_be_1():
    assert_invalid(
        {"version": 2, "services": {"api": minimal_service()}}, contains="version must be 1"
    )


def test_version_defaults_ok():
    cfg = parse(minimal_config())
    assert cfg.version == 1


# ---------------------------------------------------------------------------
# services non-empty + required fields
# ---------------------------------------------------------------------------


def test_services_must_be_nonempty():
    assert_invalid({"version": 1, "services": {}}, contains="non-empty")


def test_service_requires_source_with_git():
    assert_invalid(
        {"version": 1, "services": {"api": {"command": "./run"}}},
        contains="source",
    )


def test_command_required():
    assert_invalid(
        {"version": 1, "services": {"api": {"source": {"git": "u", "sha": "abc1234"}}}},
        contains="command",
    )


# ---------------------------------------------------------------------------
# sha
# ---------------------------------------------------------------------------


def test_sha_may_be_empty_preseed():
    svc = minimal_service()
    svc["source"]["sha"] = ""
    cfg = parse(minimal_config({"api": svc}))
    assert cfg.services["api"].source.sha == ""


def test_sha_must_be_hex():
    svc = minimal_service()
    svc["source"]["sha"] = "not-a-sha"
    assert_invalid(minimal_config({"api": svc}), contains="sha")


def test_sha_lowercased():
    svc = minimal_service()
    svc["source"]["sha"] = "ABCDEF1234"
    cfg = parse(minimal_config({"api": svc}))
    assert cfg.services["api"].source.sha == "abcdef1234"


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


def test_restart_accepts_known_values():
    for r in ("no", "on-success", "on-failure", "always", "on-abnormal", "on-abort", "on-watchdog"):
        cfg = parse(minimal_config({"api": minimal_service(restart=r)}))
        assert cfg.services["api"].restart == r


def test_restart_rejects_unknown():
    assert_invalid(
        minimal_config({"api": minimal_service(restart="forever")}),
        contains="restart",
    )


def test_restart_defaults_on_failure():
    cfg = parse(minimal_config())
    assert cfg.services["api"].restart == "on-failure"


# ---------------------------------------------------------------------------
# listen + PORT/ADDRESS env clash
# ---------------------------------------------------------------------------


def test_listen_with_port_in_env_rejected():
    svc = minimal_service(listen="127.0.0.1:8080", environment={"PORT": "9000"})
    assert_invalid(minimal_config({"api": svc}), contains="PORT/ADDRESS")


def test_listen_with_address_in_env_rejected():
    svc = minimal_service(listen="127.0.0.1:8080", environment={"ADDRESS": "x"})
    assert_invalid(minimal_config({"api": svc}), contains="PORT/ADDRESS")


def test_listen_without_env_ok():
    svc = minimal_service(listen="127.0.0.1:8080")
    cfg = parse(minimal_config({"api": svc}))
    assert cfg.services["api"].parsed_listen_port() == 8080


def test_listen_unix_socket_recognised():
    svc = minimal_service(listen="/run/outpost/api.sock")
    cfg = parse(minimal_config({"api": svc}))
    assert cfg.services["api"].is_unix_listen
    assert cfg.services["api"].parsed_listen_port() is None


def test_listen_unix_socket_relative_sock():
    svc = minimal_service(listen="./api.sock")
    cfg = parse(minimal_config({"api": svc}))
    assert cfg.services["api"].is_unix_listen


def test_listen_reserved_nginx_port_rejected():
    svc = minimal_service(listen="127.0.0.1:41999")
    assert_invalid(minimal_config({"api": svc}), contains="reserved for NGINX")


def test_listen_duplicate_tcp_port_rejected():
    cfg = minimal_config(
        {
            "a": minimal_service(listen="127.0.0.1:8080"),
            "b": minimal_service(listen="127.0.0.1:8080"),
        }
    )
    assert_invalid(cfg, contains="already claimed")


def test_listen_duplicate_unix_socket_rejected():
    sock = "/run/outpost/x.sock"
    cfg = minimal_config(
        {
            "a": minimal_service(listen=sock),
            "b": minimal_service(listen=sock),
        }
    )
    assert_invalid(cfg, contains="already claimed")


def test_listen_malformed_rejected():
    svc = minimal_service(listen="not-a-listen")
    assert_invalid(minimal_config({"api": svc}), contains="listen must be")


# ---------------------------------------------------------------------------
# port_range
# ---------------------------------------------------------------------------


def test_port_range_default():
    cfg = parse(minimal_config())
    assert cfg.port_range == "18000-18999"


def test_port_range_custom_ok():
    cfg = parse(minimal_config(port_range="20000-20010"))
    assert cfg.port_range == "20000-20010"


def test_port_range_bad_shape():
    assert_invalid(minimal_config(port_range="oops"), contains="port_range")


def test_port_range_lo_gt_hi():
    assert_invalid(minimal_config(port_range="9000-8000"), contains="port_range")


# ---------------------------------------------------------------------------
# health: exactly one of http/tcp
# ---------------------------------------------------------------------------


def test_health_http_ok():
    cfg = parse(minimal_config({"api": minimal_service(health={"http": {"path": "/healthz"}})}))
    assert cfg.services["api"].health is not None
    assert cfg.services["api"].health.is_http


def test_health_tcp_ok():
    cfg = parse(minimal_config({"api": minimal_service(health={"tcp": True})}))
    assert cfg.services["api"].health is not None
    assert not cfg.services["api"].health.is_http


def test_health_both_rejected():
    assert_invalid(
        minimal_config({"api": minimal_service(health={"http": {"path": "/"}, "tcp": True})}),
        contains="exactly one",
    )


def test_health_neither_rejected():
    assert_invalid(
        minimal_config({"api": minimal_service(health={"timeout": 5})}),
        contains="exactly one",
    )


def test_health_timeout_must_be_positive():
    assert_invalid(
        minimal_config({"api": minimal_service(health={"tcp": True, "timeout": 0})}),
    )


def test_health_timeout_defaults_30():
    cfg = parse(minimal_config({"api": minimal_service(health={"tcp": True})}))
    assert cfg.services["api"].health is not None
    assert cfg.services["api"].health.timeout == 30


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def test_routes_duplicate_host_rejected():
    cfg = minimal_config(
        routes=[
            {"host": "app.example.com", "paths": {"/": {"to": "api"}}},
            {"host": "app.example.com", "paths": {"/x": {"to": "api"}}},
        ]
    )
    assert_invalid(cfg, contains="duplicated")


def test_routes_duplicate_catchall_rejected():
    cfg = minimal_config(
        routes=[
            {"paths": {"/": {"to": "api"}}},
            {"paths": {"/x": {"to": "api"}}},
        ]
    )
    assert_invalid(cfg, contains="duplicated")


def test_routes_unknown_target_rejected():
    cfg = minimal_config(routes=[{"host": "h", "paths": {"/": {"to": "nope"}}}])
    assert_invalid(cfg, contains="not a defined service")


def test_routes_distinct_hosts_ok():
    cfg = parse(
        minimal_config(
            routes=[
                {"host": "a.com", "paths": {"/": {"to": "api"}}},
                {"host": "b.com", "paths": {"/": {"to": "api"}}},
            ]
        )
    )
    assert len(cfg.routes) == 2


# ---------------------------------------------------------------------------
# exposure ⊆ routes
# ---------------------------------------------------------------------------


def test_exposure_host_not_routed_rejected():
    cfg = minimal_config(
        routes=[{"host": "app.example.com", "paths": {"/": {"to": "api"}}}],
        exposure={"cloudflare": {"credentials_file": "c.json", "hosts": ["other.example.com"]}},
    )
    assert_invalid(cfg, contains="not routed")


def test_exposure_host_matching_routed_ok():
    cfg = parse(
        minimal_config(
            routes=[{"host": "app.example.com", "paths": {"/": {"to": "api"}}}],
            exposure={"cloudflare": {"credentials_file": "c.json", "hosts": ["app.example.com"]}},
        )
    )
    assert cfg.exposure is not None
    assert cfg.exposure.cloudflare.hosts == ["app.example.com"]


def test_exposure_host_matching_wildcard_ok():
    cfg = parse(
        minimal_config(
            routes=[{"host": "*.example.com", "paths": {"/": {"to": "api"}}}],
            exposure={"cloudflare": {"credentials_file": "c.json", "hosts": ["sub.example.com"]}},
        )
    )
    assert cfg.exposure is not None
    assert cfg.exposure.cloudflare.hosts == ["sub.example.com"]


def test_exposure_wildcard_does_not_match_bare_domain():
    assert_invalid(
        minimal_config(
            routes=[{"host": "*.example.com", "paths": {"/": {"to": "api"}}}],
            exposure={"cloudflare": {"credentials_file": "c.json", "hosts": ["example.com"]}},
        ),
        contains="not routed",
    )


# ---------------------------------------------------------------------------
# environment normalisation (map or list)
# ---------------------------------------------------------------------------


def test_environment_map_form():
    cfg = parse(minimal_config({"api": minimal_service(environment={"LOG": "info"})}))
    assert dict(cfg.services["api"].environment.vars) == {"LOG": "info"}


def test_environment_list_form():
    svc = minimal_service(environment=["LOG=info", "DEBUG=1"])
    cfg = parse(minimal_config({"api": svc}))
    assert dict(cfg.services["api"].environment.vars) == {"LOG": "info", "DEBUG": "1"}


def test_environment_bad_list_entry():
    assert_invalid(
        minimal_config({"api": minimal_service(environment=["noequals"])}),
        contains="KEY=value",
    )


def test_env_file_string_coerced_to_list():
    cfg = parse(minimal_config({"api": minimal_service(env_file="./s.env")}))
    assert cfg.services["api"].env_file == ["./s.env"]


# ---------------------------------------------------------------------------
# immutability
# ---------------------------------------------------------------------------


def test_models_are_frozen():
    cfg = parse(minimal_config())
    with pytest.raises(ValidationError):
        cfg.services["api"].command = "changed"  # type: ignore[misc]
