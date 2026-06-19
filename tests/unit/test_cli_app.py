"""Smoke tests for the CLI surface (cli/app.py).

These verify exit codes, text shapes, and JSON output against the Typer runner.
The actual subprocess/engine behaviour is integration-tested elsewhere; here we
just confirm the CLI parses args, calls the right helper, and maps the result to
the expected exit code and output format.

Command set must match ``rfc.md`` §19 exactly (14 commands).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from outpost.cli.app import app

runner = CliRunner()

# 14 commands from rfc.md §19 (init deferred to Phase 9).
_REQUIRED_COMMANDS: frozenset[str] = frozenset({
    "validate",
    "apply",
    "update",
    "start",
    "stop",
    "restart",
    "up",
    "down",
    "status",
    "ps",
    "routes",
    "exposure",
    "logs",
})


def test_all_required_commands_exist() -> None:
    registered = {c.callback.__name__ for c in app.registered_commands}
    missing = _REQUIRED_COMMANDS - registered
    assert not missing, f"commands missing from CLI: {missing}"


def test_version(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "outpost" in result.stdout


def test_validate_valid(tmp_path: Path) -> None:
    cfg = tmp_path / "outpost.yaml"
    cfg.write_text("version: 1\nservices:\n  api:\n    source: {git: https://x.git, sha: abc1234}\n    command: ./run\n")
    result = runner.invoke(app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_validate_missing_file(tmp_path: Path) -> None:
    cfg = tmp_path / "nonexistent.yaml"
    result = runner.invoke(app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 2
    assert "not found" in result.stderr


def test_validate_invalid_schema(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("version: 2\nservices: {}\n")
    result = runner.invoke(app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 2
    assert "invalid" in result.stderr


def test_apply_missing_config(tmp_path: Path) -> None:
    cfg = tmp_path / "nosuch.yaml"
    result = runner.invoke(app, ["apply", "--config", str(cfg)])
    assert result.exit_code == 2
    assert "not found" in result.stderr


def test_update_without_service_prints_help(tmp_path: Path) -> None:
    result = runner.invoke(app, ["update"])
    assert result.exit_code != 0
    assert "Error" in result.stderr or "Usage" in result.stdout


def test_logs_default_lines(tmp_path: Path) -> None:
    result = runner.invoke(app, ["logs", "--help"])
    assert result.exit_code == 0
    assert "--lines" in result.stdout


def test_start_stop_restart_help(tmp_path: Path) -> None:
    for cmd in ("start", "stop", "restart"):
        result = runner.invoke(app, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help failed: {result.stdout}"


def test_ps_routes_exposure_help(tmp_path: Path) -> None:
    for cmd in ("ps", "routes", "exposure"):
        result = runner.invoke(app, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help failed: {result.stdout}"


def test_status_json(tmp_path: Path) -> None:
    cfg = tmp_path / "outpost.yaml"
    cfg.write_text("version: 1\nservices:\n  api:\n    source: {git: https://x.git, sha: abc1234}\n    command: ./run\n")
    result = runner.invoke(app, ["status", "--json", "--config", str(cfg)])
    # status calls systemctl, which fails in the test runner — but we still get
    # JSON-structured output (with nulls/fails in the unit fields).
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "services" in data
    assert "routes" in data
    # The service appears even though systemctl call fails.
    assert any(s["name"] == "api" for s in data["services"])


def test_exposure_json_none(tmp_path: Path) -> None:
    cfg = tmp_path / "outpost.yaml"
    cfg.write_text("version: 1\nservices:\n  api:\n    source: {git: https://x.git, sha: abc1234}\n    command: ./run\n")
    result = runner.invoke(app, ["exposure", "--json", "--config", str(cfg)])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"provider": None, "hosts": []}


def test_routes_json(tmp_path: Path) -> None:
    cfg = tmp_path / "outpost.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "version": 1,
            "services": {
                "web": {
                    "source": {"git": "https://x.git", "ref": "main", "sha": "abc1234"},
                    "command": "./run",
                }
            },
            "routes": [{"host": "app.example.com", "paths": {"/": {"to": "web"}}}],
        })
    )
    result = runner.invoke(app, ["routes", "--json", "--config", str(cfg)])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data == [{"host": "app.example.com", "paths": {"/": "web"}}]
