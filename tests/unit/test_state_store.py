"""Tests for the state store (state/store.py).

DoD core: load returns empty State for a missing file (fresh host); save→load
round-trips the digest/ports/timestamp; malformed JSON and bad shapes raise
StateError (never silently treated as fresh — that could re-allocate live ports);
with_apply returns a new immutable instance; on-disk JSON is canonical (sorted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from outpost.state.store import State, StateError, StateStore


def test_load_missing_file_is_empty_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.load() == State()
    assert store.load().is_empty()


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    state = State(applied_digest="abc123", ports={"api": 18000, "web": 18001})
    saved = state.with_apply("abc123", {"api": 18000, "web": 18001})

    store.save(saved)
    loaded = store.load()

    assert loaded.applied_digest == "abc123"
    assert loaded.ports == {"api": 18000, "web": 18001}
    assert loaded.applied_at != ""  # timestamp populated by with_apply
    assert loaded.applied_at.endswith("Z")  # UTC, ISO-8601, Z-suffixed


def test_save_is_canonical_sorted_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(State(applied_digest="d", ports={"web": 18001, "api": 18000}))

    raw = json.loads(path.read_text(encoding="utf-8"))
    # JSON object keys are sorted; ports entries appear in sorted key order.
    assert list(raw.keys()) == sorted(raw.keys())
    assert list(raw["ports"].keys()) == ["api", "web"]


def test_with_apply_is_immutable_and_does_not_mutate_original() -> None:
    original = State()
    updated = original.with_apply("digest", {"api": 18000})
    assert original.is_empty()  # original untouched
    assert updated.applied_digest == "digest"
    assert updated.ports == {"api": 18000}


def test_malformed_json_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StateError, match="not valid JSON"):
        StateStore(path).load()


def test_non_object_top_level_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(StateError, match="top level must be an object"):
        StateStore(path).load()


def test_bad_ports_type_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"ports": [1, 2]}), encoding="utf-8")
    with pytest.raises(StateError, match="ports must be an object"):
        StateStore(path).load()


def test_bad_port_value_type_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"ports": {"api": "18000"}}), encoding="utf-8")
    with pytest.raises(StateError, match="ports entries must be"):
        StateStore(path).load()


def test_unknown_keys_are_ignored_for_forward_compat(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"applied_digest": "d", "ports": {"api": 18000}, "future_field": 42}),
        encoding="utf-8",
    )
    state = StateStore(path).load()
    assert state.applied_digest == "d"
    assert state.ports == {"api": 18000}


def test_save_creates_atomic_swap_no_tmp_left(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    StateStore(path).save(State(applied_digest="d"))
    assert path.is_file()
    assert not (tmp_path / "state.json.tmp").exists()
