"""Tests for the journalctl wrapper.

DoD core: ``tail`` builds the exact argv with the default ``200`` lines and a
custom count, returns stdout, and ``--no-pager`` is always present.
"""

from __future__ import annotations

import pytest

from outpost.sysdeps import journalctl
from outpost.sysdeps.run import CompletedProcess, SubprocessError
from tests.mocks import FakeRunner

UNIT = "api"


def _argv(lines: str) -> list[str]:
    return ["journalctl", "--user", "-u", UNIT, "-n", lines, "--no-pager"]


def test_tail_argv_default_lines():
    fake = FakeRunner()
    fake.script(_argv("200"), returns=CompletedProcess(0, "log\n", ""))
    journalctl.tail(fake, UNIT)
    assert fake.argvs == [_argv("200")]


def test_tail_argv_custom_lines():
    fake = FakeRunner()
    fake.script(_argv("50"), returns=CompletedProcess(0, "", ""))
    journalctl.tail(fake, UNIT, lines=50)
    assert fake.argvs == [_argv("50")]


def test_tail_returns_stdout():
    fake = FakeRunner()
    fake.script(_argv("200"), returns=CompletedProcess(0, "line1\nline2\n", ""))
    assert journalctl.tail(fake, UNIT) == "line1\nline2\n"


def test_tail_propagates_stderr():
    fake = FakeRunner()
    fake.script(_argv("200"), returns=CompletedProcess(1, "", "No journal files found"))
    with pytest.raises(SubprocessError, match="No journal files found"):
        journalctl.tail(fake, UNIT)


def test_default_lines_constant():
    assert journalctl.DEFAULT_LINES == 200
