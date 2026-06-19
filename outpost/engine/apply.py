"""The apply pipeline — the core of Outpost's single loop.

Orchestrates the ``prd.md`` §"Apply semantics" / ``rfc.md`` §9.1 sequence:

1. materialize sources — clone if missing, seed an empty ``sha`` once, check out
   the pinned sha, build iff the build-skip marker is stale;
2. allocate ports (reusing prior allocations);
3. render to a staging dir and validate with ``nginx -t``;
4. back up the live ``generated/`` as last-known-good, atomically swap the staged
   set in, sync unit symlinks, ``daemon-reload``;
5. start/restart affected services and gate on each one's startup health probe
   (a bare ``is-active`` check when no health is defined);
6. only on a fully-passing gate: reload NGINX (new routing goes live), ensure
   cloudflared is started, and commit ``state.json``.

Any failure in steps 4-6 reverts ``generated/`` from the last-known-good backup,
``daemon-reload``s, reloads NGINX back, and leaves the digest (and therefore
``apply``'s idempotency) untouched. NGINX reloads **after** the health gate so
live traffic never reaches a service that failed it.

The whole pipeline is driven through the :class:`~outpost.sysdeps.run.Runner`
seam and injectable :class:`~outpost.paths.RuntimePaths`, so it is fully
exercisable with :class:`~tests.mocks.runner.FakeRunner` against a tmp tree.
``engine/`` knows nothing of Typer or MCP: this function returns an
:class:`ApplyResult`; the CLI/MCP map that to exit codes / JSON.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from outpost import config as config_mod
from outpost.constants import CLOUDFLARED_UNIT
from outpost.engine.health import HealthCheckError, wait_for
from outpost.engine.ports import PortAllocationError, allocate_all
from outpost.engine.render import RenderError
from outpost.engine.stage import stage
from outpost.models import OutpostConfig, Service
from outpost.paths import GENERATED_SUBDIRS, RuntimePaths
from outpost.state.store import StateStore
from outpost.sysdeps import git, nginx, systemctl
from outpost.sysdeps.run import Runner, SubprocessError

__all__ = ["ApplyError", "ApplyResult", "apply"]


class ApplyError(Exception):
    """An operational error during apply that is not a subprocess failure.

    Raised for conditions the pipeline detects itself (a missing credentials
    file, a unix-socket service with an unprobeable health shape) rather than a
    command exiting non-zero. Maps to exit code 1 alongside ``SubprocessError``.
    """


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of an apply, mapped by the CLI/MCP to exit codes and JSON.

    ``ok`` is the load-bearing field; ``no_op`` distinguishes the idempotent
    nothing-changed case (still exit 0) so the CLI can report it. ``message`` is
    a single human line — the CLI/MCP owns any richer formatting.
    """

    ok: bool
    no_op: bool = False
    message: str = ""


def apply(
    *,
    runner: Runner,
    paths: RuntimePaths | None = None,
    config_path: str | Path | None = None,
    config: OutpostConfig | None = None,
    store: StateStore | None = None,
    staging_root: str | Path | None = None,
) -> ApplyResult:
    """Run the full apply pipeline. Returns an :class:`ApplyResult`.

    ``config`` may be passed pre-parsed (tests); otherwise it is loaded from
    ``config_path`` (default XDG location). A :class:`ConfigError` from loading
    propagates — invalid config is exit-code 2 and is not an apply failure.

    ``staging_root`` overrides the temp staging dir (tests pass a known path so
    the path-bearing ``nginx -t`` argv is scriptable against the FakeRunner).
    """
    paths = paths or RuntimePaths()
    store = store or StateStore(paths.state)
    cfg_path = Path(config_path) if config_path is not None else config_mod.DEFAULT_CONFIG_PATH
    if config is None:
        config = config_mod.load(cfg_path)

    # 1. Materialize sources (clone / seed / checkout / build). Clones and builds
    #    are idempotent and outside the last-known-good rollback set, so a failure
    #    here just fails the apply without reverting generated configs.
    try:
        config, seeded = _materialize(config, runner, paths, cfg_path)
    except SubprocessError as exc:
        return ApplyResult(ok=False, message=f"source materialization failed: {_stderr(exc)}")

    digest = config_mod.digest(config)
    state = store.load()

    # Idempotent no-op: same spec already applied and every service is up.
    if digest == state.applied_digest and _all_active(runner, config, paths):
        return ApplyResult(ok=True, no_op=True, message="already applied; services up")

    # 2-5. Allocate, stage, validate NGINX — no live-tree mutation yet.
    try:
        ports = allocate_all(config, preferred=state.ports)
        tunnel = _read_tunnel(config) if config.exposure is not None else None
        staging = (
            Path(staging_root)
            if staging_root is not None
            else Path(tempfile.mkdtemp(prefix="outpost-stage-"))
        )
        staging.mkdir(parents=True, exist_ok=True)
        try:
            tree = stage(config, staging, tunnel=tunnel)
            _run_nginx_test(runner, tree.nginx, prefix=staging)
            changed = _changed_services(config, tree, paths)
        except SubprocessError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except SubprocessError as exc:
        return ApplyResult(ok=False, message=f"nginx config invalid: {_stderr(exc)}")
    except (RenderError, PortAllocationError, ApplyError) as exc:
        return ApplyResult(ok=False, message=str(exc))

    # 6-12. Mutation phase. Any failure here rolls back to last-known-good.
    try:
        paths.generated.mkdir(parents=True, exist_ok=True)
        _backup(paths)
        _install(staging, paths.generated)
        _sync_symlinks(config, paths)
        systemctl.daemon_reload(runner)
        _start_and_gate(runner, config, ports, changed)
        nginx.reload(runner)
        if config.exposure is not None:
            systemctl.start(runner, CLOUDFLARED_UNIT)
        store.save(state.with_apply(digest, ports))
    except (SubprocessError, HealthCheckError) as exc:
        _rollback(paths, runner)
        return ApplyResult(ok=False, message=f"apply failed, rolled back: {exc}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    seeded_msg = f" (seeded {', '.join(sorted(seeded))})" if seeded else ""
    return ApplyResult(ok=True, message=f"applied {len(config.services)} service(s){seeded_msg}")


# ===========================================================================
# Source materialization (clone / seed / checkout / build)
# ===========================================================================


def _materialize(
    config: OutpostConfig, runner: Runner, paths: RuntimePaths, cfg_path: Path
) -> tuple[OutpostConfig, dict[str, str]]:
    """Clone, seed empty shas, check out pinned shas, build if markers are stale.

    Returns the (possibly re-seeded) config and the ``{service: sha}`` seed map.
    Seeding writes the resolved sha back into the YAML via a targeted edit and
    rebuilds the model so the caller's digest reflects the seeded spec.
    """
    paths.repos.mkdir(parents=True, exist_ok=True)
    paths.data.mkdir(parents=True, exist_ok=True)

    seeded: dict[str, str] = {}
    new_services: dict[str, Service] = dict(config.services)

    for name, svc in config.services.items():
        repo_dir = paths.repos / name
        (paths.data / name).mkdir(parents=True, exist_ok=True)
        git.clone(runner, svc.source.git, repo_dir)

        # Resolve the deploy target: the pinned sha, or ref->sha on first deploy.
        if svc.source.needs_seed:
            target_sha = _resolve_seed_sha(runner, repo_dir, svc)
            seeded[name] = target_sha
            new_source = svc.source.model_copy(update={"sha": target_sha})
            new_services[name] = svc.model_copy(update={"source": new_source})
        else:
            target_sha = svc.source.sha

        git.checkout(runner, repo_dir, target_sha)
        _maybe_build(runner, svc, repo_dir, target_sha)

    if seeded:
        for name, sha in seeded.items():
            config_mod.write_source_fields(cfg_path, name, sha=sha)
        config = config.model_copy(update={"services": new_services})

    return config, seeded


def _resolve_seed_sha(runner: Runner, repo_dir: Path, svc: Service) -> str:
    """Resolve ``ref`` (or the remote default branch) to a sha for first deploy.

    A ``None`` ref means "track the remote default branch", expressed as
    ``origin/HEAD`` — ``git clone`` sets that ref up, so rev-parse resolves it.
    The git wrapper deliberately leaves None-handling to engine policy.
    """
    ref = svc.source.ref or "origin/HEAD"
    return git.resolve_ref(runner, repo_dir, ref)


def _maybe_build(runner: Runner, svc: Service, repo_dir: Path, sha: str) -> None:
    """Run ``build`` iff defined and the build-skip marker is missing or stale.

    The marker (``repos/<service>/.outpost/built.sha``) records the sha last
    built; matching it means an unchanged pinned commit is not rebuilt. Services
    with no ``build`` are clone-and-run and skip the marker entirely.
    """
    if not svc.build:
        return
    marker = repo_dir / ".outpost" / "built.sha"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == sha:
        return
    working_dir = repo_dir / svc.source.path if svc.source.path else repo_dir
    runner.run(["sh", "-c", svc.build], cwd=working_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(sha, encoding="utf-8")


# ===========================================================================
# Allocation, staging validation, change detection
# ===========================================================================


def _read_tunnel(config: OutpostConfig) -> str:
    """Read the cloudflared ``TunnelID`` from the operator's credentials file.

    The tunnel UUID is operator-owned (not in ``outpost.yaml``), so the apply
    layer reads it from the credentials JSON the exposure block points at. A
    missing/unreadable file is a fail-fast operational error before any swap.
    """
    assert config.exposure is not None
    creds_path = Path(config.exposure.cloudflare.credentials_file).expanduser()
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ApplyError(f"cloudflared credentials not readable: {creds_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApplyError(f"cloudflared credentials not valid JSON: {creds_path}: {exc}") from exc
    tunnel = data.get("TunnelID")
    if not isinstance(tunnel, str) or not tunnel:
        raise ApplyError(f"cloudflared credentials missing TunnelID: {creds_path}")
    return tunnel


def _run_nginx_test(runner: Runner, servers_conf: Path, *, prefix: Path) -> None:
    """Validate the staged server blocks with a throwaway complete ``nginx.conf``.

    ``nginx -t`` needs a full conf (it cannot test bare server blocks), so we
    write one whose ``http{}`` includes the staged ``servers.conf`` and run
    ``nginx -t -c <conf> -p <prefix>``. The include path is absolute so it
    resolves regardless of the prefix's relative-path resolution.
    """
    conf = prefix / "nginx.conf"
    conf.write_text(
        "events {}\nhttp {\n"
        f"    include {servers_conf.resolve()};\n"
        "}\n",
        encoding="utf-8",
    )
    nginx.test(runner, conf, prefix=prefix)


def _changed_services(
    config: OutpostConfig, tree_from_staged, paths: RuntimePaths
) -> set[str]:
    """Services whose staged unit differs from the live one (or has no live one).

    Drives restart-vs-start: a changed unit is restarted, an unchanged one is
    merely (idempotently) started so a down service comes back up without
    needlessly restarting healthy ones.
    """
    changed: set[str] = set()
    for name in config.services:
        live = paths.systemd / f"{name}.service"
        staged_text = tree_from_staged.units[name].read_text(encoding="utf-8")
        if not live.is_file() or live.read_text(encoding="utf-8") != staged_text:
            changed.add(name)
    return changed


# ===========================================================================
# Start/restart + startup gate
# ===========================================================================


def _start_and_gate(
    runner: Runner,
    config: OutpostConfig,
    ports: dict[str, int],
    changed: set[str],
) -> None:
    """Start/restart each service, then gate on its startup probe.

    Changed units restart; unchanged units start (idempotent — brings a stopped
    one up, no-ops a running one). After start, every service is gated: a defined
    ``health`` is polled to its timeout; an undefined one needs a single
    ``is_active`` pass. A failed gate raises and the caller rolls back.
    """
    for name, svc in config.services.items():
        unit = f"{name}.service"
        if name in changed:
            systemctl.restart(runner, unit)
        else:
            systemctl.start(runner, unit)
        _gate(runner, name, svc, ports)


def _gate(runner: Runner, name: str, svc: Service, ports: dict[str, int]) -> None:
    """The startup gate for one service. Raises :class:`HealthCheckError` on miss."""
    unit = f"{name}.service"
    if svc.health is None:
        # ponytail: point-in-time is_active check — matches prd "succeeds once
        # systemd reports active". Ongoing crash-restart is systemd's job; a
        # sustained-readiness probe is the deferred passive/active check.
        if not systemctl.is_active(runner, unit):
            raise HealthCheckError(f"service {name!r} not active after start")
        return
    host, port, socket_path = _listener(name, svc, ports)
    wait_for(svc.health, host=host, port=port, socket_path=socket_path)


def _listener(
    name: str, svc: Service, ports: dict[str, int]
) -> tuple[str | None, int | None, str | None]:
    """Resolve a service's local listener for the health probe.

    TCP declared: ``(host, port, None)``. Unix socket: ``(None, None, path)``.
    Allocated: ``("127.0.0.1", port, None)``. The probe always hits this local
    listener, never NGINX.
    """
    if svc.is_unix_listen:
        return None, None, svc.listen
    if svc.has_listen:
        host, _, port_str = svc.listen.rpartition(":")
        return host, int(port_str), None
    port = ports.get(name)
    if port is None:
        raise HealthCheckError(f"service {name!r} has no listener to probe")
    return "127.0.0.1", port, None


def _all_active(runner: Runner, config: OutpostConfig, paths: RuntimePaths) -> bool:
    """True iff every service's unit is currently active (the no-op precondition)."""
    del paths  # paths drives live discovery in Phase 9 symlink work; units are name-keyed
    return all(systemctl.is_active(runner, f"{name}.service") for name in config.services)


# ===========================================================================
# Atomic swap, last-known-good backup, rollback
# ===========================================================================


def _backup(paths: RuntimePaths) -> None:
    """Copy the live generated subdirs into the single ``.lkg`` slot."""
    lkg = paths.lkg
    if lkg.exists():
        shutil.rmtree(lkg)
    lkg.mkdir(parents=True)
    for sub in GENERATED_SUBDIRS:
        src = paths.generated / sub
        if src.exists():
            shutil.copytree(src, lkg / sub)


def _install(staging: Path, generated: Path) -> None:
    """Swap each staged subdir over the live one (rmtree + rename)."""
    for sub in GENERATED_SUBDIRS:
        dst = generated / sub
        if dst.exists():
            shutil.rmtree(dst)
        src = staging / sub
        if src.exists():
            shutil.move(str(src), str(dst))


def _sync_symlinks(config: OutpostConfig, paths: RuntimePaths) -> None:
    """Ensure each generated unit is symlinked into the user-unit drop-in dir.

    ``systemctl --user`` only loads units from ``~/.config/systemd/user/``; the
    generated units live under the runtime tree, so each is exposed by a symlink.
    Symlinks point at the stable generated path, so a swap or rollback needs no
    re-pointing. # ponytail: stale symlinks for since-removed services are not
    pruned here (a dangling symlink is harmless and pruning risks touching
    non-Outpost units); add explicit cleanup if it becomes a problem.
    """
    paths.user_units.mkdir(parents=True, exist_ok=True)
    for name in config.services:
        target = (paths.systemd / f"{name}.service").resolve()
        link = paths.user_units / f"{name}.service"
        if link.is_symlink() and Path(os.readlink(link)) == target:
            continue
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(target, link)


def _rollback(paths: RuntimePaths, runner: Runner) -> None:
    """Revert generated/ from last-known-good, daemon-reload, reload NGINX.

    Best-effort by design: this runs in an except block, so a failure here must
    not mask the original error. Rollback depth is one (non-goal: history).
    NGINX's restored config was validated when it became last-known-good, so we
    reload directly rather than re-running ``nginx -t``.
    """
    if not paths.lkg.exists():
        return  # first apply — nothing prior to restore
    try:
        for sub in GENERATED_SUBDIRS:
            dst = paths.generated / sub
            if dst.exists():
                shutil.rmtree(dst)
            src = paths.lkg / sub
            if src.exists():
                shutil.copytree(src, dst)
        systemctl.daemon_reload(runner)
        if (paths.nginx / "servers.conf").is_file():
            nginx.reload(runner)
    except SubprocessError:
        # The original apply failure is the one to report; a rollback fault is a
        # double-fault we cannot usefully escalate from here.
        pass


def _stderr(exc: SubprocessError) -> str:
    """The most useful single line from a subprocess failure (stderr, else repr)."""
    return (exc.stderr or str(exc)).strip()
