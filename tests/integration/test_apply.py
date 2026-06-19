"""Integration tests for the apply pipeline (engine/apply.py).

Phase 5 DoD: happy path; invalid NGINX config (no mutation); health failure
(rollback); empty-sha seed; idempotent no-op on digest match; build skipped on
an unchanged sha; port reuse from state. All driven through the FakeRunner against
a tmp runtime tree — no host side-effects.

The FakeRunner matches exact argv, so apply's internal temp paths would be
unscriptable. We pass ``staging_root`` so the path-bearing ``nginx -t`` argv is
known and scriptable. The remaining commands (git/systemctl) are either exact and
path-predictable, or covered by a default success.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from outpost import config as config_mod
from outpost.engine.apply import apply
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
    """Every unscripted command succeeds with empty output (clones, starts, ...)."""
    fake.script_default(CompletedProcess(0, "", ""))


def _script_nginx(fake: FakeRunner, staging: Path, *, ok: bool = True) -> None:
    argv = ["nginx", "-p", str(staging), "-t", "-c", str(staging / "nginx.conf")]
    fake.script(argv, returns=_NGINX_OK if ok else CompletedProcess(1, "", "bad config\n"))


def _script_active(fake: FakeRunner, name: str, *, active: bool = True) -> None:
    fake.script(
        ["systemctl", "--user", "is-active", "--quiet", f"{name}.service"],
        returns=CompletedProcess(0 if active else 3, "", ""),
    )


def _config(services: dict, **overrides) -> OutpostConfig:  # type: ignore[no-untyped-def]
    return OutpostConfig.model_validate(minimal_config(services, **overrides))


# ===========================================================================
# Happy path
# ===========================================================================


def test_apply_happy_path_installs_and_commits(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = StateStore(paths.state)
    cfg = _config({"api": minimal_service()})
    fake = FakeRunner()
    _ok_default(fake)
    staging = tmp_path / "stage"
    _script_nginx(fake, staging)

    result = apply(
        runner=fake,
        paths=paths,
        config=cfg,
        store=store,
        staging_root=staging,
        config_path=tmp_path / "outpost.yaml",
    )

    assert result.ok and not result.no_op
    state = store.load()
    assert state.applied_digest == config_mod.digest(cfg)
    assert state.ports == {"api": 18000}
    assert (paths.systemd / "api.service").is_file()
    assert (paths.user_units / "api.service").is_symlink()
    assert paths.lkg.is_dir()  # backup slot created


def test_apply_idempotent_noop_on_digest_match(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)  # init owns this; the test pre-saves state
    store = StateStore(paths.state)
    cfg = _config({"api": minimal_service()})
    # Pre-seed state as if already applied; the only live check is is-active.
    store.save(State(applied_digest=config_mod.digest(cfg), ports={"api": 18000}))
    fake = FakeRunner()
    _ok_default(fake)
    _script_active(fake, "api", active=True)
    staging = tmp_path / "stage"

    result = apply(
        runner=fake, paths=paths, config=cfg, store=store, staging_root=staging
    )

    assert result.ok and result.no_op
    # No staging happened on a no-op.
    assert not staging.exists()
    # No reload was issued.
    assert not any(
        c.argv[:3] == ["systemctl", "--user", "reload"] for c in fake.calls
    )


# ===========================================================================
# Rollback paths
# ===========================================================================


def test_apply_invalid_nginx_leaves_nothing_installed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = StateStore(paths.state)
    cfg = _config({"api": minimal_service()})
    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, tmp_path / "stage", ok=False)

    result = apply(
        runner=fake, paths=paths, config=cfg, store=store, staging_root=tmp_path / "stage"
    )

    assert not result.ok
    assert "nginx config invalid" in result.message
    assert not paths.generated.exists()  # nothing swapped in
    assert store.load().applied_digest == ""  # state untouched


def test_apply_health_fail_rolls_back_to_last_known_good(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = StateStore(paths.state)
    staging = tmp_path / "stage"

    # Phase 1: a healthy apply (no health gate) installs the good unit "./serve".
    good = _config({"api": minimal_service(command="./serve")})
    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, staging)
    apply(
        runner=fake, paths=paths, config=good, store=store,
        staging_root=staging, config_path=tmp_path / "outpost.yaml",
    )
    good_digest = store.load().applied_digest
    assert good_digest != ""

    # Phase 2: a changed config (different command "./serve2") with an http health
    # gate that nothing answers -> gate fails -> rollback restores phase 1.
    broken = _config(
        {"api": minimal_service(command="./serve2", health={"http": {"path": "/"}, "timeout": 1})}
    )
    fake2 = FakeRunner()
    _ok_default(fake2)
    _script_nginx(fake2, staging)
    result = apply(
        runner=fake2, paths=paths, config=broken, store=store,
        staging_root=staging, config_path=tmp_path / "outpost.yaml",
    )

    assert not result.ok
    assert "rolled back" in result.message
    # State unchanged (still phase 1's digest).
    assert store.load().applied_digest == good_digest
    # The live unit is restored to the good command, not the broken one.
    unit = (paths.systemd / "api.service").read_text(encoding="utf-8")
    assert "serve2" not in unit
    assert "serve" in unit


# ===========================================================================
# Seed, build-skip, port reuse
# ===========================================================================


def test_apply_seeds_empty_sha_and_writes_it_back(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = StateStore(paths.state)
    cfg_path = tmp_path / "outpost.yaml"
    # sha lives under source; minimal_service spreads kwargs on the service dict,
    # so set it on the source block explicitly.
    seeded_svc = minimal_service()
    seeded_svc["source"]["sha"] = ""
    cfg_path.write_text(yaml.safe_dump(minimal_config({"api": seeded_svc})), encoding="utf-8")
    cfg = OutpostConfig.model_validate(minimal_config({"api": seeded_svc}))
    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, tmp_path / "stage")
    fake.script(
        ["git", "-C", str(paths.repos / "api"), "rev-parse", "main"],
        returns=CompletedProcess(0, "deadbeefcafe\n", ""),
    )

    result = apply(
        runner=fake, paths=paths, config=cfg, store=store,
        staging_root=tmp_path / "stage", config_path=cfg_path,
    )

    assert result.ok
    # The seeded sha is written back into the YAML...
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["services"]["api"]["source"]["sha"] == "deadbeefcafe"
    # ...and the committed digest matches the seeded config.
    seeded = config_mod.load(cfg_path)
    assert store.load().applied_digest == config_mod.digest(seeded)


def test_apply_skips_build_when_marker_matches_sha(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = StateStore(paths.state)
    cfg = _config({"api": minimal_service(build="make build", sha="abc1234")})
    repo = paths.repos / "api"
    (repo / ".git").mkdir(parents=True)  # so clone no-ops
    (repo / ".outpost").mkdir(parents=True)
    (repo / ".outpost" / "built.sha").write_text("abc1234", encoding="utf-8")
    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, tmp_path / "stage")

    apply(
        runner=fake, paths=paths, config=cfg, store=store,
        staging_root=tmp_path / "stage", config_path=tmp_path / "outpost.yaml",
    )

    assert not any(c.argv[:3] == ["sh", "-c", "make build"] for c in fake.calls)


def test_apply_rebuilds_when_marker_stale(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = StateStore(paths.state)
    cfg = _config({"api": minimal_service(build="make build", sha="abc1234")})
    repo = paths.repos / "api"
    (repo / ".git").mkdir(parents=True)
    (repo / ".outpost").mkdir(parents=True)
    (repo / ".outpost" / "built.sha").write_text("oldsha", encoding="utf-8")  # stale
    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, tmp_path / "stage")

    apply(
        runner=fake, paths=paths, config=cfg, store=store,
        staging_root=tmp_path / "stage", config_path=tmp_path / "outpost.yaml",
    )

    build_calls = [c for c in fake.calls if c.argv[:3] == ["sh", "-c", "make build"]]
    assert len(build_calls) == 1
    assert build_calls[0].cwd == repo  # build ran in the clone root
    # Marker refreshed to the pinned sha.
    assert (repo / ".outpost" / "built.sha").read_text(encoding="utf-8") == "abc1234"


def test_apply_reuses_previously_allocated_port(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.base.mkdir(parents=True)  # init owns this; the test pre-saves state
    store = StateStore(paths.state)
    cfg = _config(
        {
            "api": minimal_service(),  # listen-less -> allocated
            "web": minimal_service(git="https://x/web.git", listen="127.0.0.1:8080"),
        }
    )
    # Pretend a prior apply allocated api at 18500; the digest differs, so a full
    # apply runs and should reuse 18500 rather than first-fitting to 18000.
    store.save(State(applied_digest="stale-digest", ports={"api": 18500}))
    fake = FakeRunner()
    _ok_default(fake)
    _script_nginx(fake, tmp_path / "stage")

    apply(
        runner=fake, paths=paths, config=cfg, store=store,
        staging_root=tmp_path / "stage", config_path=tmp_path / "outpost.yaml",
    )

    assert store.load().ports["api"] == 18500
