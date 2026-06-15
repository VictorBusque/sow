"""State: the minimal ``state.json`` sidecar + atomic write helper.

The state store holds only the applied digest, allocated ports, and the last
apply timestamp (rfc.md §14). ``write_atomic`` is the single atomic-write helper
reused by the state store and every ``outpost.yaml`` rewrite.
"""

from __future__ import annotations

from outpost.state.io import read_text, write_atomic
from outpost.state.store import DEFAULT_STATE_PATH, State, StateError, StateStore

__all__ = ["DEFAULT_STATE_PATH", "State", "StateError", "StateStore", "read_text", "write_atomic"]
