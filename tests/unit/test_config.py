"""Tests for config loading and digest stability.

DoD (Phase 1): same config -> same digest; a sha change -> digest change; load()
parses a real-ish config and reports clean errors on bad input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outpost.config import DEFAULT_CONFIG_PATH, ConfigError, digest, load
from outpost.models import OutpostConfig
from tests.unit.conftest import minimal_config, minimal_service


def _config(services=None, **overrides) -> OutpostConfig:
    return OutpostConfig.model_validate(minimal_config(services, **overrides))


# ---------------------------------------------------------------------------
# digest stability
# ---------------------------------------------------------------------------


def test_digest_stable_across_runs():
    a = _config()
    b = _config()
    assert digest(a) == digest(b)
    assert len(digest(a)) == 64  # sha256 hex


def test_digest_changes_when_sha_changes():
    cfg = _config()
    before = digest(cfg)
    # Frozen model: mutate by replacing the service with a new sha.
    new_svc = cfg.services["api"].source.model_copy(update={"sha": "deadbeef"})
    new_api = cfg.services["api"].model_copy(update={"source": new_svc})
    new_services = {**cfg.services, "api": new_api}
    changed = cfg.model_copy(update={"services": new_services})
    after = digest(changed)
    assert before != after, "digest must change when source.sha changes"


def test_digest_independent_of_field_order():
    svc_a = minimal_service(environment={"B": "2", "A": "1"})
    svc_b = minimal_service(environment={"A": "1", "B": "2"})
    assert digest(_config({"api": svc_a})) == digest(_config({"api": svc_b}))


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.yaml")


def test_load_empty_file(tmp_path: Path):
    p = tmp_path / "outpost.yaml"
    p.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load(p)


def test_load_malformed_yaml(tmp_path: Path):
    p = tmp_path / "outpost.yaml"
    p.write_text("version: 1\n  bad: : : indent\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load(p)


def test_load_validation_error_collected(tmp_path: Path):
    p = tmp_path / "outpost.yaml"
    p.write_text("version: 1\nservices:\n  api:\n    command: ./run\n")  # missing source
    with pytest.raises(ConfigError) as exc_info:
        load(p)
    assert exc_info.value.errors  # at least one human-readable error line


def test_load_valid_yaml(tmp_path: Path):
    p = tmp_path / "outpost.yaml"
    p.write_text(
        "version: 1\n"
        "services:\n"
        "  api:\n"
        "    source: {git: https://x.git, sha: abc1234}\n"
        "    command: ./run\n"
    )
    cfg = load(p)
    assert isinstance(cfg, OutpostConfig)
    assert cfg.services["api"].command == "./run"


def test_default_config_path_is_xdg():
    assert (Path.home() / ".config" / "outpost" / "outpost.yaml") == DEFAULT_CONFIG_PATH
