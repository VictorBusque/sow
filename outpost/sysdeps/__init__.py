"""sysdeps: subprocess wrappers behind a single swappable :class:`Runner`.

``run.py`` defines the strategy seam — :class:`~outpost.sysdeps.run.Runner`,
:class:`~outpost.sysdeps.run.RealRunner`, :class:`~outpost.sysdeps.run.SubprocessError`
— and is the **only** place ``subprocess.run`` is called. The four tool modules
(``git``, ``systemctl``, ``nginx``, ``journalctl``) are free functions that take a
``runner`` as their first argument and build argv from it, matching the functional
style of ``outpost.config.load`` / ``digest``.

This package re-exports the seam types for convenience; the tool modules are
imported as namespaces (e.g. ``from outpost.sysdeps import git``). There is no
``SysDeps`` facade in Phase 2 — its only consumer is the engine (Phase 5), which
will take a ``runner`` directly; the functional style means adding one later is
zero-refactor.
"""

from __future__ import annotations

from outpost.sysdeps import git, journalctl, nginx, systemctl
from outpost.sysdeps.run import (
    CompletedProcess,
    RealRunner,
    Runner,
    SubprocessError,
)

__all__ = [
    "CompletedProcess",
    "RealRunner",
    "Runner",
    "SubprocessError",
    "git",
    "journalctl",
    "nginx",
    "systemctl",
]
