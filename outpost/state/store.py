"""The persistent state sidecar — ``state.json``.

Per ``prd.md`` / ``rfc.md`` §14 and implementation-plan.md Phase 3, this file holds
**only** what cannot be derived from config or runtime files:

- ``applied_digest`` — the SHA-256 of the last successfully applied spec (so
  ``apply`` can no-op idempotently).
- ``ports`` — allocated TCP ports for listen-less services (stable across
  restarts; reused unless the service definition changes).
- ``applied_at`` — an ISO-8601 timestamp of the last successful apply.

It deliberately stores **nothing else**: no SHA (that lives in ``source.sha``),
no per-service status (computed live at query time), no history. A question the
state cannot answer with these three values plus the files is out of scope for v1.

``StateStore`` is the only read/write surface. ``load`` returns an empty state for
a missing file (first run / fresh host) — that is not an error; ``apply`` treats a
missing ``applied_digest`` as "nothing applied yet". Writes go through
:func:`~outpost.state.io.write_atomic`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from outpost.state.io import read_text, write_atomic

__all__ = ["DEFAULT_STATE_PATH", "State", "StateError", "StateStore"]

# XDG-strict runtime location (rfc.md §6, stack.md §5).
DEFAULT_STATE_PATH = Path.home() / ".local" / "share" / "outpost" / "state.json"


class StateError(Exception):
    """Raised when ``state.json`` exists but is unreadable/unparseable.

    A *missing* file is not an error (fresh host → empty state); a present but
    malformed file is, because silently ignoring it could mask corruption and let
    ``apply`` re-allocate ports already in use. Fail-fast, surface the path.
    """


@dataclass(frozen=True)
class State:
    """The in-memory state model — a frozen value object.

    Frozen (matching the rest of the codebase's "pure, immutable models" rule):
    mutation returns a new instance via the ``with_*`` helpers, so the store's
    current snapshot is never alias-mutated by an in-flight apply. A dataclass
    rather than Pydantic because state is not operator-facing schema and the
    three fields have no validation rules worth a dependency.
    """

    applied_digest: str = ""
    ports: dict[str, int] = field(default_factory=dict)
    applied_at: str = ""

    def with_apply(self, digest: str, ports: dict[str, int]) -> State:
        """Return a new State recording a successful apply at the current time.

        ``ports`` is the full allocation map to persist (allocated ports only;
        declared-listen services are absent). The timestamp is UTC ISO-8601 with
        a ``Z`` suffix — sortable, timezone-explicit, and stable across hosts.
        """
        return State(
            applied_digest=digest,
            ports=dict(ports),
            applied_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )

    def is_empty(self) -> bool:
        """True iff nothing has been applied (the first-run / fresh-host state)."""
        return self.applied_digest == "" and not self.ports and self.applied_at == ""


class StateStore:
    """Read/write ``state.json`` with atomic, fail-fast semantics.

    Constructed against a path (default :data:`DEFAULT_STATE_PATH`).
    :meth:`load` returns an empty :class:`State` when the file is absent.
    :meth:`save` serialises to canonical JSON (sorted keys, for a stable diff)
    and writes atomically.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_STATE_PATH

    def load(self) -> State:
        """Load the state, or return an empty State if the file does not exist.

        Raises :class:`StateError` if the file exists but is not valid JSON or
        does not match the expected shape — a torn or hand-edited state must not
        be silently treated as fresh, since that could re-allocate live ports.
        """
        if not self.path.is_file():
            return State()
        try:
            raw = json.loads(read_text(self.path))
        except json.JSONDecodeError as exc:
            raise StateError(f"state.json is not valid JSON: {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise StateError(f"state.json top level must be an object: {self.path}")
        return _from_dict(raw, self.path)

    def save(self, state: State) -> None:
        """Persist ``state`` atomically to ``state.json``.

        Serialised as canonical JSON (sorted keys, no trailing whitespace) so the
        on-disk form is deterministic — two identical states always byte-match,
        which makes diffing and snapshot tests meaningful.
        """
        payload = json.dumps(
            _to_dict(state), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        write_atomic(self.path, payload.encode("utf-8"))


def _to_dict(state: State) -> dict[str, object]:
    """Serialise a State to its on-disk dict shape (stable across versions).

    ``ports`` is normalised to a plain dict (the dataclass stores one already, but
    being explicit keeps the contract independent of the field's runtime type).
    """
    return {
        "applied_digest": state.applied_digest,
        "ports": dict(state.ports),
        "applied_at": state.applied_at,
    }


def _from_dict(raw: dict[str, object], path: Path) -> State:
    """Parse the on-disk dict into a State, validating the shape.

    Only the three known keys are read; unknown keys are ignored (forward-compat:
    a newer Outpost that added a key must not crash an older one reading it back,
    and an older state lacks fields newer code expects). Each known key, if
    present, must have the right type — a structural mismatch is corruption.
    """
    digest = raw.get("applied_digest", "")
    applied_at = raw.get("applied_at", "")
    ports_raw = raw.get("ports", {})

    if not isinstance(digest, str):
        raise StateError(f"state.json: applied_digest must be a string: {path}")
    if not isinstance(applied_at, str):
        raise StateError(f"state.json: applied_at must be a string: {path}")
    if not isinstance(ports_raw, dict):
        raise StateError(f"state.json: ports must be an object: {path}")

    ports: dict[str, int] = {}
    for name, port in ports_raw.items():
        if not isinstance(name, str) or not isinstance(port, int) or isinstance(port, bool):
            raise StateError(f"state.json: ports entries must be {name!r}: int: {path}")
        ports[name] = port

    return State(applied_digest=digest, ports=ports, applied_at=applied_at)
