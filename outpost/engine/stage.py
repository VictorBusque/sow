"""Stage rendered artifacts into a temp dir mirroring ``generated/``.

Phase 4 of the apply pipeline: render every service unit, the NGINX server
blocks, and the cloudflared config into a throwaway directory whose layout
matches the live ``~/.local/share/outpost/generated/`` tree. The apply loop then
validates (``nginx -t``) and atomically swaps this staged set over the live one.

``stage`` is the only writer to a staging area; it never touches the live tree,
so a failed/abandoned apply leaves the host untouched (just a stale temp dir to
gc). It returns the :class:`StagedTree` so the caller knows exactly what was
written and where, without re-deriving paths.

The cloudflared ``tunnel`` UUID is taken as a parameter: the apply layer reads it
from the operator's credentials JSON (prd.md), and rendering stays pure. When the
config has no exposure, no cloudflared file is written (and ``cloudflared`` in the
returned :class:`StagedTree` is ``None``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from outpost.engine.ports import allocate_all
from outpost.engine.render import (
    RenderError,
    compute_facts,
    render_cloudflared,
    render_nginx,
    render_unit,
)
from outpost.models import OutpostConfig

__all__ = [
    "CLOUDFLARED_CONFIG",
    "CLOUDFLARED_DIR",
    "NGINX_DIR",
    "SYSTEMD_DIR",
    "StagedTree",
    "stage",
]

# Filenames within the staging tree. One NGINX file (so ``init``'s single include
# line stays stable across applys); one unit per service; one cloudflared config.
_SYSTEMD_SUBDIR = "systemd"
_NGINX_SUBDIR = "nginx"
_CLOUDFLARED_SUBDIR = "cloudflared"
_UNIT_SUFFIX = ".service"
_NGINX_FILE = "servers.conf"
_CLOUDFLARED_FILE = "config.yml"

# Exposed for the apply layer (live-path mirrors).
SYSTEMD_DIR = "generated/systemd"
NGINX_DIR = "generated/nginx"
CLOUDFLARED_DIR = "generated/cloudflared"
CLOUDFLARED_CONFIG = f"{CLOUDFLARED_DIR}/config.yml"


@dataclass(frozen=True)
class StagedTree:
    """The result of :func:`stage`: where each artifact was written.

    ``root`` is the temp dir; ``units`` maps service name → staged unit path;
    ``nginx`` is the single server-blocks file; ``cloudflared`` is ``None`` when
    the config has no ``exposure``. ``ports`` is the allocation used (allocated
    ports only) — the caller persists this into ``state.json`` on a successful
    apply.
    """

    root: Path
    units: dict[str, Path]
    nginx: Path
    cloudflared: Path | None
    ports: dict[str, int]


def stage(config: OutpostConfig, root: str | Path, tunnel: str | None = None) -> StagedTree:
    """Render all artifacts into ``root`` mirroring the live ``generated/`` tree.

    Creates ``root/{systemd,nginx,cloudflared}``, writes one unit per service,
    the NGINX server blocks, and (if exposure is defined and ``tunnel`` given)
    the cloudflared config. ``tunnel`` is required iff the config declares an
    exposure — omitting it then is a caller bug, surfaced as :class:`RenderError`.

    Returns the :class:`StagedTree` describing what was written. Does **not**
    validate (``nginx -t``) or swap — that is the apply loop's job.
    """
    root_path = Path(root)
    ports = allocate_all(config)
    facts = compute_facts(config, ports)

    systemd_dir = root_path / _SYSTEMD_SUBDIR
    nginx_dir = root_path / _NGINX_SUBDIR
    cloudflared_dir = root_path / _CLOUDFLARED_SUBDIR
    systemd_dir.mkdir(parents=True, exist_ok=True)
    nginx_dir.mkdir(parents=True, exist_ok=True)
    cloudflared_dir.mkdir(parents=True, exist_ok=True)

    units: dict[str, Path] = {}
    for name, svc in config.services.items():
        text = render_unit(name, svc, ports.get(name), facts=facts)
        unit_path = systemd_dir / f"{name}{_UNIT_SUFFIX}"
        unit_path.write_text(text, encoding="utf-8")
        units[name] = unit_path

    nginx_text = render_nginx(config, ports)
    nginx_path = nginx_dir / _NGINX_FILE
    nginx_path.write_text(nginx_text, encoding="utf-8")

    cloudflared_path: Path | None = None
    if config.exposure is not None:
        if tunnel is None:
            raise RenderError(
                "config declares an exposure but no cloudflared tunnel id was provided"
            )
        cloudflared_text = render_cloudflared(config, tunnel)
        cloudflared_path = cloudflared_dir / _CLOUDFLARED_FILE
        cloudflared_path.write_text(cloudflared_text, encoding="utf-8")

    return StagedTree(
        root=root_path,
        units=units,
        nginx=nginx_path,
        cloudflared=cloudflared_path,
        ports=ports,
    )
