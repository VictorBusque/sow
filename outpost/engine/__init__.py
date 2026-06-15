"""Engine: the core loop — validate, render, apply, update.

Phase 3-4 surface: the render path (service units, NGINX server blocks,
cloudflared config) and the pure port allocator. ``apply``/``update``
orchestration arrives in Phase 5/6.
"""

from __future__ import annotations

from outpost.engine.ports import PortAllocationError, allocate_all
from outpost.engine.render import (
    CloudflaredSpec,
    NginxLocation,
    NginxServerSpec,
    RenderError,
    UnitSpec,
    build_nginx_specs,
    build_spec,
    compute_facts,
    render_cloudflared,
    render_nginx,
    render_unit,
)
from outpost.engine.stage import StagedTree, stage

__all__ = [
    "CloudflaredSpec",
    "NginxLocation",
    "NginxServerSpec",
    "PortAllocationError",
    "RenderError",
    "StagedTree",
    "UnitSpec",
    "allocate_all",
    "build_nginx_specs",
    "build_spec",
    "compute_facts",
    "render_cloudflared",
    "render_nginx",
    "render_unit",
    "stage",
]
