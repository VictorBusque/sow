"""Pure port allocator for services that omit a declared ``listen``.

First-fit over ``config.port_range``: for each listen-less service, take the
lowest free port in the range, excluding :data:`~outpost.constants.NGINX_PORT`
and every operator-declared ``listen`` TCP port. Exhaustion raises
:class:`PortAllocationError` (fail-fast — v1 never silently reuses a port).

This is the *pure* core Phase 3 wraps with ``state.json`` persistence: given the
same config, ``allocate_all`` is deterministic. Allocation memory lives only for
the call; nothing is read from or written to disk here.
"""

from __future__ import annotations

from collections.abc import Mapping

from outpost.constants import NGINX_PORT
from outpost.models import OutpostConfig

__all__ = ["PortAllocationError", "allocate_all"]


class PortAllocationError(Exception):
    """Raised when there are more listen-less services than free ports in range."""


def _parse_port_range(value: str) -> tuple[int, int]:
    """Parse a validated ``"lo-hi"`` string into an inclusive ``(lo, hi)`` tuple.

    The model has already bounds-checked this at parse time, so it always
    succeeds; we re-split here rather than couple to the model's private
    Pydantic-raise validator.
    """
    lo_str, hi_str = value.split("-", 1)
    return int(lo_str), int(hi_str)


def allocate_all(
    config: OutpostConfig, preferred: Mapping[str, int] | None = None
) -> dict[str, int]:
    """Assign a TCP port to every service without a declared ``listen``.

    Returns ``{service_name: port}`` only for allocated services — declared
    ``listen`` (TCP or unix-socket) services are absent (they own their address
    and need no allocation).

    ``preferred`` is the previously-persisted allocation (``state.json`` ports).
    An existing allocation is reused when it still falls in range and does not
    collide with a reserved/declared port, so a service keeps a stable port
    across applys (and cross-service ``${svc.PORT}`` references stay valid).
    # ponytail: we reuse whenever the port is still valid rather than detecting
    # "service definition changed" — reuse is almost always the desired outcome,
    # and a service that no longer exists simply drops out of the allocation.
    """
    lo, hi = _parse_port_range(config.port_range)

    # Exclusions: the reserved NGINX port plus every declared TCP listen port.
    # Declared ports are already mutually collision-free (enforced by the model's
    # topology validator), so a set suffices.
    taken: set[int] = {NGINX_PORT}
    for svc in config.services.values():
        port = svc.parsed_listen_port()
        if port is not None:
            taken.add(port)

    pref = preferred or {}
    allocations: dict[str, int] = {}
    for name, svc in config.services.items():
        # Only listen-less services get an allocated TCP port.
        if svc.has_listen:
            continue

        existing = pref.get(name)
        if existing is not None and lo <= existing <= hi and existing not in taken:
            port = existing
        else:
            port = _first_free(lo, hi, taken)
            if port is None:
                raise PortAllocationError(
                    f"port range {lo}-{hi} exhausted; cannot allocate a port for "
                    f"service {name!r} (in use: {sorted(taken)})"
                )
        taken.add(port)
        allocations[name] = port

    return allocations


def _first_free(lo: int, hi: int, taken: set[int]) -> int | None:
    """Lowest port in ``[lo, hi]`` not in ``taken``, or ``None`` if none free."""
    for port in range(lo, hi + 1):
        if port not in taken:
            return port
    return None
