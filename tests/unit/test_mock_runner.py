"""Tests for the FakeRunner test double.

The mock's contract is itself load-bearing (exact-argv match, SubprocessError on
check+nonzero, loud failure on unscripted calls), so it gets its own coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outpost.sysdeps.run import CompletedProcess, SubprocessError
from tests.mocks import FakeRunner, FakeRunnerNotScripted

ARGV = ["systemctl", "--user", "is-active", "--quiet", "api"]


def test_run_records_call_fields():
    fake = FakeRunner()
    fake.script(ARGV, returns=CompletedProcess(0, "", ""))
    fake.run(ARGV, cwd=Path("/srv"), check=False)
    call = fake.calls[0]
    assert call.argv == ARGV
    assert call.cwd == Path("/srv")
    assert call.check is False


def test_argv_is_exact_tuple_match_order_sensitive():
    fake = FakeRunner()
    fake.script(ARGV, returns=CompletedProcess(0, "", ""))
    # Same tokens, different order -> unscripted -> loud failure.
    with pytest.raises(FakeRunnerNotScripted):
        fake.run(list(reversed(ARGV)))


def test_returns_fn_supports_stateful_faking():
    fake = FakeRunner()
    states = iter([CompletedProcess(3, "", ""), CompletedProcess(0, "", "")])
    fake.script(ARGV, returns_fn=lambda _call: next(states))
    assert fake.run(ARGV, check=False).returncode == 3
    assert fake.run(ARGV, check=False).returncode == 0


def test_nonzero_with_check_raises_real_subprocess_error():
    fake = FakeRunner()
    fake.script(ARGV, returns=CompletedProcess(1, "", "boom"))
    with pytest.raises(SubprocessError, match="boom") as exc_info:
        fake.run(ARGV)
    assert exc_info.value.stderr == "boom"
    assert exc_info.value.argv == ARGV


def test_nonzero_without_check_returns_result():
    fake = FakeRunner()
    fake.script(ARGV, returns=CompletedProcess(1, "", "boom"))
    result = fake.run(ARGV, check=False)
    assert result.returncode == 1


def test_unscripted_call_raises_assertion_error():
    fake = FakeRunner()
    with pytest.raises(FakeRunnerNotScripted, match="unscripted"):
        fake.run(["git", "status"])


def test_script_requires_exactly_one_return_source():
    fake = FakeRunner()
    ok = CompletedProcess(0, "", "")
    with pytest.raises(TypeError):
        fake.script(ARGV, returns=ok, returns_fn=lambda _call: ok)
    with pytest.raises(TypeError):
        fake.script(ARGV)
