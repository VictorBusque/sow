"""Engine: the core loop — validate, render, apply, update.

The walking skeleton (this phase) ships the render path and the pure port
allocator. ``apply``/``update`` orchestration arrives in Phase 5/6.
"""

from __future__ import annotations

from outpost.engine.ports import PortAllocationError, allocate_all
from outpost.engine.render import (
    RenderError,
    UnitSpec,
    build_spec,
    compute_facts,
    render_unit,
)

__all__ = [
    "PortAllocationError",
    "RenderError",
    "UnitSpec",
    "allocate_all",
    "build_spec",
    "compute_facts",
    "render_unit",
]
