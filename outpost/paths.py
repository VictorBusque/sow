"""Injectable runtime filesystem paths.

Everything Outpost owns on disk lives under two XDG roots (``rfc.md`` §6):
the runtime tree ``~/.local/share/outpost/`` (generated configs, clones, data,
state) and the user-unit drop-in dir ``~/.config/systemd/user/``. Hard-coding
``Path.home()`` at every call site makes the apply loop untestable against a
real ``$HOME`` and drifts if either root moves. This frozen value object is the
single place those roots live; the engine, CLI, and ``init`` all derive paths
from one ``RuntimePaths`` instance. Tests construct one rooted at ``tmp_path`` so
no apply ever touches the operator's home.

A value object, not a service: pure properties over two roots, no I/O. Default
``Path.home()`` resolves at construction (``default_factory``), so a process
that changes ``$HOME`` mid-run is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["RuntimePaths"]

# XDG runtime + config roots (rfc.md §6, AGENTS.md "Paths").


def _runtime_default() -> Path:
    return Path.home() / ".local" / "share" / "outpost"


def _user_units_default() -> Path:
    return Path.home() / ".config" / "systemd" / "user"

# The three generated subdirs swapped as a unit by the apply pipeline.
_GENERATED_SUBDIRS: tuple[str, ...] = ("systemd", "nginx", "cloudflared")


@dataclass(frozen=True)
class RuntimePaths:
    """The two on-disk roots Outpost writes under, plus derived paths.

    ``base`` is the runtime root (``~/.local/share/outpost``); ``user_units`` is
    the systemd user-unit drop-in dir where generated units are symlinked so
    ``systemctl --user`` discovers them. All other paths are derived so there is
    one source of truth per root.
    """

    base: Path = field(default_factory=_runtime_default)
    user_units: Path = field(default_factory=_user_units_default)

    @property
    def repos(self) -> Path:
        """Per-service git clones (``repos/<service>``)."""
        return self.base / "repos"

    @property
    def data(self) -> Path:
        """Per-service persistent ``DATA_DIR`` roots (``data/<service>``)."""
        return self.base / "data"

    @property
    def generated(self) -> Path:
        """The generated tree swapped atomically (per-subdir) by apply."""
        return self.base / "generated"

    @property
    def systemd(self) -> Path:
        """Generated user units (``generated/systemd``)."""
        return self.generated / "systemd"

    @property
    def nginx(self) -> Path:
        """Generated NGINX server blocks (``generated/nginx``)."""
        return self.generated / "nginx"

    @property
    def cloudflared(self) -> Path:
        """Generated cloudflared config (``generated/cloudflared``)."""
        return self.generated / "cloudflared"

    @property
    def lkg(self) -> Path:
        """The single last-known-good backup slot (``generated/.lkg``).

        Rollback depth is one (non-goal: history) — see implementation-plan.md
        "Last-known-good backup (single slot)".
        """
        return self.generated / ".lkg"

    @property
    def state(self) -> Path:
        """The ``state.json`` sidecar."""
        return self.base / "state.json"


GENERATED_SUBDIRS = _GENERATED_SUBDIRS
