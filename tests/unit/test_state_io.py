"""Tests for the atomic write helper (state/io.py).

DoD core: write_atomic produces the exact bytes; a concurrent/torn write can
never be observed because the rename is atomic; the temp file is cleaned up on
failure; read_text round-trips.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from outpost.state.io import read_text, write_atomic


def test_write_atomic_writes_exact_bytes(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_atomic(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'


def test_write_atomic_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_bytes(b"OLD")
    write_atomic(target, b"NEW")
    assert target.read_bytes() == b"NEW"


def test_write_atomic_leaves_no_tmp_file_on_success(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_atomic(target, b"x")
    assert not (tmp_path / "out.json.tmp").exists()


def test_write_atomic_cleans_up_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.json"

    # Sabotage os.replace so the write fails after the temp file is created.
    def _boom(src: str, dst: str) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        write_atomic(target, b"x")

    # The temp file must be removed, and the target never created.
    assert not (tmp_path / "out.json.tmp").exists()
    assert not target.exists()


def test_read_text_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    write_atomic(target, "héllo\n".encode())
    assert read_text(target) == "héllo\n"
