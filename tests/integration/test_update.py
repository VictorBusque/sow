"""Integration tests for the update command (engine/update.py).

Phase 6 DoD: sha-advance + apply; fetch/build/health failure leaves state
unchanged; ref override; already-at-latest no-op. All driven through the
FakeRunner against a tmp runtime tree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from outpost import config as config_mod
from outpost.engine.update import update
from outpost.models import OutpostConfig
from outpost.paths import RuntimePaths
from outpost.state.store import State, StateStore
from outpost.sysdeps.run import CompletedProcess
from tests.mocks.runner import FakeRunner
from tests.unit.conftest import minimal_config, minimal_service

_NGINX_OK = CompletedProcess(0, "nginx: configuration file test is successful\n", "")


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(base=tmp_path / "run", user_units=tmp_path / "units")


def _ok_default(fake: FakeRunner) -> None:
    fake.script_default(CompletedProcess(0, "", ""))


def _script_nginx(fake: FakeRunner, staging: Path, *, ok: bool = True) -> None:
    argv = ["nginx", "-p", str(staging), "-t", "-c", str(staging / "nginx.conf")]
    fake.script(argv, returns=_NGINX_OK if ok else CompletedProcess(1, "", "bad config\n"))


def _config(services: dict, **overrides) -> OutpostConfig:  # type: ignore[no-untyped-def]
    return OutpostConfig.model_validate(minimal_config(services, **overrides))


# ===========================================================================
# Happy path
# ===========================================================================


def test_update_happy_path_advances_sha(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)
    store = StateStore(paths.state)
    # Seed a config with a known sha.
    svc = minimal_service(sha="abc1234")
    cfg_path = tmp_path / "outpost.yaml"
    cfg_path.write_text(yaml.safe_dump(minimal_config({"api": svc})), encoding="utf-8")
    cfg = _config({"api": svc})
    store.save(State(applied_digest=config_mod.digest(cfg), ports={"api": 18000}))
    repo_dir = paths.repos / "api"

    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, tmp_path / "stage")
    # fetch succeeds, resolve ref "main" → new sha
    fake.script(
        ["git", "-C", str(repo_dir), "rev-parse", "main"],
        returns=CompletedProcess(0, "deadbeefcafe\n", ""),
    )

    result = update(
        "api", runner=fake, paths=paths, store=store,
        config_path=cfg_path, staging_root=tmp_path / "stage",
    )

    assert result.ok and not result.no_op
    # Config file has the new sha.
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["services"]["api"]["source"]["sha"] == "deadbeefcafe"
    # State has the new digest (reflects the new sha).
    updated = config_mod.load(cfg_path)
    assert store.load().applied_digest == config_mod.digest(updated)


def test_update_noop_when_already_at_latest(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)
    repo_dir = paths.repos / "api"
    svc = minimal_service(sha="abc1234")
    cfg_path = tmp_path / "outpost.yaml"
    cfg_path.write_text(yaml.safe_dump(minimal_config({"api": svc})), encoding="utf-8")
    store = StateStore(paths.state)
    cfg = _config({"api": svc})
    store.save(State(applied_digest=config_mod.digest(cfg), ports={"api": 18000}))

    fake = FakeRunner()
    _ok_default(fake)
    # resolve ref returns the same sha → no-op.
    fake.script(
        ["git", "-C", str(repo_dir), "rev-parse", "main"],
        returns=CompletedProcess(0, "abc1234\n", ""),
    )

    result = update(
        "api", runner=fake, paths=paths, store=store, config_path=cfg_path,
    )

    assert result.ok and result.no_op
    # Config file unchanged.
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["services"]["api"]["source"]["sha"] == "abc1234"


# ===========================================================================
# Failure paths
# ===========================================================================


def test_update_fetch_failure_leaves_state_unchanged(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)
    store = StateStore(paths.state)
    svc = minimal_service(sha="abc1234")
    cfg_path = tmp_path / "outpost.yaml"
    cfg_path.write_text(yaml.safe_dump(minimal_config({"api": svc})), encoding="utf-8")
    cfg = _config({"api": svc})
    store.save(State(applied_digest=config_mod.digest(cfg), ports={"api": 18000}))
    repo_dir = paths.repos / "api"

    fake = FakeRunner()
    _ok_default(fake)
    # fetch fails.
    fake.script(
        ["git", "-C", str(repo_dir), "fetch", "--quiet", "--all"],
        returns=CompletedProcess(1, "", "fatal: could not fetch\n"),
    )

    result = update("api", runner=fake, paths=paths, store=store, config_path=cfg_path)

    assert not result.ok
    assert "fetch failed" in result.message
    # Config and state unchanged.
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["services"]["api"]["source"]["sha"] == "abc1234"
    assert store.load().applied_digest == config_mod.digest(cfg)


def test_update_apply_failure_still_writes_new_sha(tmp_path: Path) -> None:
    """The sha is written before apply; if apply fails the config still advances."""
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)
    store = StateStore(paths.state)
    svc = minimal_service(sha="abc1234")
    cfg_path = tmp_path / "outpost.yaml"
    cfg_path.write_text(yaml.safe_dump(minimal_config({"api": svc})), encoding="utf-8")
    cfg = _config({"api": svc})
    store.save(State(applied_digest=config_mod.digest(cfg), ports={"api": 18000}))
    repo_dir = paths.repos / "api"

    fake = FakeRunner()
    _ok_default(fake)
    # nginx -t fails → apply will roll back.
    _script_nginx(fake, tmp_path / "stage", ok=False)
    fake.script(
        ["git", "-C", str(repo_dir), "rev-parse", "main"],
        returns=CompletedProcess(0, "deadbeefcafe\n", ""),
    )

    result = update(
        "api", runner=fake, paths=paths, store=store,
        config_path=cfg_path, staging_root=tmp_path / "stage",
    )

    assert not result.ok
    # Config has the new sha (written before apply).
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["services"]["api"]["source"]["sha"] == "deadbeefcafe"
    # State unchanged (old digest).
    assert store.load().applied_digest == config_mod.digest(cfg)


# ===========================================================================
# Ref override
# ===========================================================================


def test_update_with_ref_override(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)
    store = StateStore(paths.state)
    svc = minimal_service(ref="main", sha="abc1234")
    cfg_path = tmp_path / "outpost.yaml"
    cfg_path.write_text(yaml.safe_dump(minimal_config({"api": svc})), encoding="utf-8")
    cfg = _config({"api": svc})
    store.save(State(applied_digest=config_mod.digest(cfg), ports={"api": 18000}))
    repo_dir = paths.repos / "api"

    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, tmp_path / "stage")
    # --ref overrides to "develop"
    fake.script(
        ["git", "-C", str(repo_dir), "rev-parse", "develop"],
        returns=CompletedProcess(0, "f00ba12\n", ""),
    )

    result = update(
        "api", runner=fake, paths=paths, store=store,
        config_path=cfg_path, ref="develop", staging_root=tmp_path / "stage",
    )

    assert result.ok
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["services"]["api"]["source"]["ref"] == "develop"
    assert raw["services"]["api"]["source"]["sha"] == "f00ba12"
