"""The update command — advance a service's pinned git ref to its latest commit.

``prd.md`` §8 / ``rfc.md`` §9.2: ``fetch`` the remote, resolve ``ref`` (or an
overridden ``--ref``) to its SHA, write both ``ref`` and ``sha`` back into the
config, then run ``apply``. Any failure (fetch, resolve, or the subsequent apply)
leaves the running service untouched (the apply's own rollback handles generated
config; the source-sha write-back is idempotent and a retry will use the same
sha — the operator's config is the new desired state, even on partial failure).

The sha is written *before* apply so that a subsequent ``outpost apply`` (without
``update``) retries the new sha. This is by design: ``update`` never leaves the
config pointing at the old sha after a successful fetch.
"""

from __future__ import annotations

from pathlib import Path

from outpost import config as config_mod
from outpost.engine.apply import ApplyResult
from outpost.engine.apply import apply as _apply
from outpost.models import OutpostConfig
from outpost.paths import RuntimePaths
from outpost.state.store import StateStore
from outpost.sysdeps import git
from outpost.sysdeps.run import Runner, SubprocessError

__all__ = ["ApplyResult", "update"]


def update(
    service_name: str,
    *,
    runner: Runner,
    paths: RuntimePaths | None = None,
    config_path: str | Path | None = None,
    store: StateStore | None = None,
    ref: str | None = None,
    staging_root: str | Path | None = None,
) -> ApplyResult:
    """Fetch, resolve, write back, and apply the latest commit for ``service_name``.

    ``ref`` overrides ``source.ref`` (``--ref`` from the CLI); pass ``None`` to
    keep the current ref. Returns an :class:`ApplyResult` — ``ok``, ``no_op`` (if
    ``sha`` is already the latest), or failure with a message.
    """
    paths = paths or RuntimePaths()
    cfg_path = (Path(config_path) if config_path is not None else config_mod.DEFAULT_CONFIG_PATH)
    config = config_mod.load(cfg_path)

    svc = config.services.get(service_name)
    if svc is None:
        return ApplyResult(ok=False, message=f"service {service_name!r} not found in config")

    repo_dir = paths.repos / service_name
    paths.repos.mkdir(parents=True, exist_ok=True)

    # Clone and fetch.
    try:
        git.clone(runner, svc.source.git, repo_dir)
        git.fetch(runner, repo_dir)
    except SubprocessError as exc:
        return ApplyResult(ok=False, message=f"git fetch failed: {_stderr(exc)}")

    # Resolve the target ref to a concrete SHA.
    target_ref = ref or svc.source.ref or "origin/HEAD"
    try:
        new_sha = git.resolve_ref(runner, repo_dir, target_ref)
    except SubprocessError as exc:
        return ApplyResult(ok=False, message=f"ref resolution failed: {_stderr(exc)}")

    current_sha = svc.source.sha
    ref_changed = ref is not None and ref != svc.source.ref
    sha_changed = new_sha != current_sha

    if not sha_changed and not ref_changed:
        return ApplyResult(
            ok=True, no_op=True, message=f"{service_name} already at {new_sha[:12]}"
        )

    # Write ref (if overridden) and sha back to the config file.
    config_mod.write_source_fields(
        cfg_path,
        service_name,
        sha=new_sha if sha_changed else None,
        ref=ref if ref_changed else None,
    )

    # Build the updated model so the subsequent apply uses the right digest.
    updated = _updated_config(config, service_name, sha=new_sha, ref=ref)
    store = store or StateStore(paths.state)

    return _apply(
        runner=runner,
        paths=paths,
        config=updated,
        store=store,
        config_path=cfg_path,
        staging_root=staging_root,
    )


def _updated_config(
    config: OutpostConfig, name: str, *, sha: str, ref: str | None
) -> OutpostConfig:
    """Return a new config with ``name``'s ``source.{sha[, ref]}`` replaced.

    The existing config is unchanged (frozen models, immutable).
    """
    svc = config.services[name]
    updates: dict[str, str] = {"sha": sha}
    if ref is not None:
        updates["ref"] = ref
    new_source = svc.source.model_copy(update=updates)
    new_svc = svc.model_copy(update={"source": new_source})
    new_services = dict(config.services, **{name: new_svc})
    return config.model_copy(update={"services": new_services})


def _stderr(exc: SubprocessError) -> str:
    return (exc.stderr or str(exc)).strip()
