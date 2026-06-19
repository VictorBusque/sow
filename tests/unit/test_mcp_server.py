"""Tests for the MCP server (mcp/server.py).

Verifies the 11 tool definitions match the reference, and exercises the dispatch
handlers against a tmp config with a swapped runner. The actual subprocess
behaviour is integration-tested elsewhere; here we confirm the MCP layer maps
inputs to the correct engine calls and formats outputs.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import yaml

from sow.mcp import server as mcp_srv
from sow.sysdeps.run import CompletedProcess
from tests.mocks.runner import FakeRunner

# 15 tools from ``cli-reference.md``.
_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        "list_services",
        "get_service_status",
        "start_service",
        "stop_service",
        "restart_service",
        "update_service",
        "apply_config",
        "validate_config",
        "show_routes",
        "show_exposure",
        "tail_logs",
        "add_service",
        "remove_service",
        "add_route",
        "remove_route",
    }
)


def test_all_required_tools_are_defined() -> None:
    """All 15 tools from cli-reference.md have a dispatch arm."""
    src = inspect.getsource(mcp_srv)
    for tool in _REQUIRED_TOOLS:
        assert f'case "{tool}"' in src, f"missing dispatch arm for {tool}"


def test_validate_config_returns_valid(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "services": {
                    "api": {
                        "source": {"git": "https://x.git", "ref": "main", "sha": "abc1234"},
                        "command": "./run",
                    }
                },
            }
        )
    )
    mcp_srv._config_path = str(cfg)
    # We call validate_config via the dispatch. Since validate_config
    # does not use a runner, it works with just a config path.
    import asyncio

    resp = asyncio.run(mcp_srv._validate_config())
    assert not resp.isError
    text = resp.content[0]
    assert isinstance(text, mcp_srv.TextContent)
    data = json.loads(text.text)
    assert data["valid"] is True


def test_validate_config_invalid(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("version: 2\nservices: {}\n")
    mcp_srv._config_path = str(cfg)
    import asyncio

    resp = asyncio.run(mcp_srv._validate_config())
    text = resp.content[0]
    assert isinstance(text, mcp_srv.TextContent)
    data = json.loads(text.text)
    assert data["valid"] is False
    assert len(data["errors"]) > 0


def test_show_routes_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "services": {
                    "web": {
                        "source": {"git": "https://x.git", "sha": "abc1234", "ref": "main"},
                        "command": "./run",
                    }
                },
            }
        )
    )
    mcp_srv._config_path = str(cfg)
    import asyncio

    resp = asyncio.run(mcp_srv._show_routes())
    assert not resp.isError
    text = resp.content[0]
    assert isinstance(text, mcp_srv.TextContent)
    data = json.loads(text.text)
    assert data == []


def test_show_exposure_configured(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "services": {
                    "web": {
                        "source": {"git": "https://x.git", "sha": "abc1234", "ref": "main"},
                        "command": "./run",
                    }
                },
                "routes": [{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
                "exposure": {
                    "cloudflare": {
                        "credentials_file": "~/.cf/app.json",
                        "hosts": ["app.example.com"],
                    }
                },
            }
        )
    )
    mcp_srv._config_path = str(cfg)
    import asyncio

    resp = asyncio.run(mcp_srv._show_exposure())
    assert not resp.isError
    text = resp.content[0]
    assert isinstance(text, mcp_srv.TextContent)
    data = json.loads(text.text)
    assert data == {"provider": "cloudflare", "hosts": ["app.example.com"]}


def test_list_services_with_mocked_runner(tmp_path: Path) -> None:
    """list_services catches sysdep errors gracefully (unit == 'unknown')."""
    cfg = tmp_path / "sow.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "services": {
                    "api": {
                        "source": {"git": "https://x.git", "sha": "abc1234", "ref": "main"},
                        "command": "./run",
                    }
                },
            }
        )
    )
    mcp_srv._config_path = str(cfg)
    fake = FakeRunner()
    fake.script_default(CompletedProcess(0, "", ""))
    # unit_state runs systemctl --user show ... --value. Script it.
    fake.script(
        ["systemctl", "--user", "show", "-p", "ActiveState", "--value", "api.service"],
        returns=CompletedProcess(0, "active\n", ""),
    )
    old_runner = mcp_srv._runner
    mcp_srv._runner = fake
    import asyncio

    try:
        resp = asyncio.run(mcp_srv._list_services())
    finally:
        mcp_srv._runner = old_runner
    assert not resp.isError
    text = resp.content[0]
    assert isinstance(text, mcp_srv.TextContent)
    data = json.loads(text.text)
    assert len(data) == 1
    assert data[0]["name"] == "api"
    assert data[0]["unit"] == "active"


def test_show_exposure_none(tmp_path: Path) -> None:
    cfg = tmp_path / "sow.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "services": {
                    "web": {
                        "source": {"git": "https://x.git", "sha": "abc1234", "ref": "main"},
                        "command": "./run",
                    }
                },
            }
        )
    )
    mcp_srv._config_path = str(cfg)
    import asyncio

    resp = asyncio.run(mcp_srv._show_exposure())
    text = resp.content[0]
    assert isinstance(text, mcp_srv.TextContent)
    data = json.loads(text.text)
    assert data == {"provider": None, "hosts": []}
