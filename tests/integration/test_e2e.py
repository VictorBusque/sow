"""End-to-end integration test: apply - update - no-op - rollback.

Exercises the full lifecycle for one service through the engine, with mocked
sysdeps and a tmp runtime tree. This catches cross-phase regressions that
individual per-phase tests might miss.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from outpost import config as config_mod
from outpost.engine.apply import apply
from outpost.engine.update import update
from outpost.paths import RuntimePaths
from outpost.state.store import StateStore
from outpost.sysdeps.run import CompletedProcess
from tests.mocks.runner import FakeRunner
from tests.unit.conftest import minimal_config


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(base=tmp_path / "run", user_units=tmp_path / "units")


def _setup(fake: FakeRunner, staging: Path, repo: Path) -> None:
    """Wire up the default-succeed responses for all boring commands."""
    fake.script_default(CompletedProcess(0, "", ""))
    fake.script(
        ["nginx", "-p", str(staging), "-t", "-c", str(staging / "nginx.conf")],
        returns=CompletedProcess(0, "nginx: configuration file test is successful\n", ""),
    )


def test_e2e_lifecycle(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)
    store = StateStore(paths.state)
    cfg_path = tmp_path / "outpost.yaml"
    staging = tmp_path / "stage"
    repo = paths.repos / "api"
    (repo / ".git").mkdir(parents=True)

    # Config: one service at sha "abc1234", no health, build step.
    svc = {
        "source": {"git": "https://github.com/me/api.git", "ref": "main", "sha": "abc1234"},
        "command": "./serve",
        "build": "make build",
    }
    cfg_path.write_text(yaml.safe_dump(minimal_config({"api": svc})), encoding="utf-8")
    cfg = config_mod.load(cfg_path)

    # ── Phase 1: Apply ──
    fake1 = FakeRunner()
    _setup(fake1, staging, repo)
    # build marker doesn't exist → build runs
    r = apply(
        runner=fake1, paths=paths, config=cfg,
        store=store, staging_root=staging, config_path=cfg_path,
    )
    assert r.ok, f"first apply failed: {r.message}"
    assert not r.no_op
    assert store.load().applied_digest == config_mod.digest(cfg)
    # build command was called
    assert any(c.argv[:3] == ["sh", "-c", "make build"] for c in fake1.calls)
    units_installed = (paths.systemd / "api.service").is_file()
    assert units_installed, "unit not installed by apply"
    assert (paths.user_units / "api.service").is_symlink()

    # ── Phase 2: No-op re-apply (same digest, all active) ──
    fake2 = FakeRunner()
    _setup(fake2, staging, repo)
    # Make is-active return active (0).
    fake2.script(
        ["systemctl", "--user", "is-active", "--quiet", "api.service"],
        returns=CompletedProcess(0, "", ""),
    )
    r = apply(
        runner=fake2, paths=paths, config=cfg,
        store=store, staging_root=staging, config_path=cfg_path,
    )
    assert r.ok and r.no_op, f"no-op apply failed: {r.message}"

    # ── Phase 3: Update (advance sha) ──
    fake3 = FakeRunner()
    _setup(fake3, staging, repo)
    fake3.script(
        ["git", "-C", str(repo), "rev-parse", "main"],
        returns=CompletedProcess(0, "deadbeefcafe\n", ""),
    )
    r = update(
        "api", runner=fake3, paths=paths,
        store=store, config_path=cfg_path, staging_root=staging,
    )
    assert r.ok, f"update failed: {r.message}"
    assert not r.no_op
    # Config file has the new sha.
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["services"]["api"]["source"]["sha"] == "deadbeefcafe"
    # State has the new digest.
    updated_cfg = config_mod.load(cfg_path)
    assert store.load().applied_digest == config_mod.digest(updated_cfg)

    # ── Phase 4: Update no-op (already at latest) ──
    fake4 = FakeRunner()
    _setup(fake4, staging, repo)
    fake4.script(
        ["git", "-C", str(repo), "rev-parse", "main"],
        returns=CompletedProcess(0, "deadbeefcafe\n", ""),
    )
    r = update(
        "api", runner=fake4, paths=paths,
        store=store, config_path=cfg_path, staging_root=staging,
    )
    assert r.ok and r.no_op, f"update no-op should be no_op: {r.message}"

    # ── Phase 5: Health-fail rollback ──
    # Change the config: add health that nothing answers -> gate fails.
    bad_svc = dict(svc, command="./serve2", health={"http": {"path": "/"}, "timeout": 1})
    bad_config = {"version": 1, "services": {"api": bad_svc}}
    cfg_path.write_text(yaml.safe_dump(bad_config), encoding="utf-8")
    bad_cfg = config_mod.load(cfg_path)

    fake5 = FakeRunner()
    _setup(fake5, staging, repo)
    # No need to script nginx differently; health is the failing gate.
    r = apply(
        runner=fake5, paths=paths, config=bad_cfg,
        store=store, staging_root=staging, config_path=cfg_path,
    )
    assert not r.ok, "apply with failing health should roll back"
    assert "rolled back" in r.message
    # State unchanged (still digest from successful update).
    assert store.load().applied_digest == config_mod.digest(updated_cfg)
