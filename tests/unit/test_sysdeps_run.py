"""Tests for the strategy seam itself (``run.py``).

These are the *only* tests in Phase 2 that touch a real subprocess — they verify
:class:`RealRunner` translates non-zero exits into :class:`SubprocessError` and
that the exception carries argv + stderr. Everything else is exercised through
the FakeRunner.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from outpost.sysdeps.run import (
    CompletedProcess,
    RealRunner,
    Runner,
    SubprocessError,
)


def test_real_runner_success_returns_completed_process():
    result = RealRunner().run(["sh", "-c", "echo hi; echo oops 1>&2"])
    assert isinstance(result, CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == "hi\n"
    assert result.stderr == "oops\n"


def test_real_runner_failure_raises_subprocess_error():
    with pytest.raises(SubprocessError) as exc_info:
        RealRunner().run(["sh", "-c", "echo bang 1>&2; exit 3"])
    assert exc_info.value.returncode == 3
    assert exc_info.value.argv == ["sh", "-c", "echo bang 1>&2; exit 3"]
    assert exc_info.value.stderr == "bang\n"


def test_real_runner_check_false_returns_nonzero_without_raising():
    result = RealRunner().run(["sh", "-c", "exit 4"], check=False)
    assert result.returncode == 4


def test_subprocess_error_message_contains_argv_and_stderr():
    err = SubprocessError(argv=["nginx", "-t"], returncode=1, stdout="", stderr="boom")
    msg = str(err)
    assert "nginx -t" in msg
    assert "boom" in msg
    assert "1" in msg


def test_completed_process_is_frozen_and_equal():
    a = CompletedProcess(0, "x", "y")
    b = CompletedProcess(0, "x", "y")
    c = CompletedProcess(1, "x", "y")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    with pytest.raises(FrozenInstanceError):
        a.returncode = 1  # ty: ignore[invalid-assignment]  # frozen dataclass must reject mutation


def test_real_runner_satisfies_runner_protocol():
    # runtime_checkable: the production strategy is structurally a Runner.
    assert isinstance(RealRunner(), Runner)
