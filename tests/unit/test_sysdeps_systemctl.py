"""Tests for the systemctl --user wrappers.

DoD core: every argv carries ``--user``; ``start``/``stop`` no-op when the
desired state is already met (read-then-act through the same runner); ``restart``
always acts; ``is_active`` maps returncode→bool with ``check=False``; failures
surface as :class:`SubprocessError`.
"""

from __future__ import annotations

import pytest

from outpost.sysdeps import systemctl
from outpost.sysdeps.run import CompletedProcess, SubprocessError
from tests.mocks import FakeRunner

UNIT = "api"


def _is_active_argv(unit: str = UNIT) -> list[str]:
    return ["systemctl", "--user", "is-active", "--quiet", unit]


# ---------------------------------------------------------------------------
# is_active (check=False; returncode -> bool)
# ---------------------------------------------------------------------------


def test_is_active_true_on_zero():
    fake = FakeRunner()
    fake.script(_is_active_argv(), returns=CompletedProcess(0, "", ""))
    assert systemctl.is_active(fake, UNIT) is True
    assert fake.calls[-1].check is False


def test_is_active_false_on_nonzero():
    fake = FakeRunner()
    fake.script(_is_active_argv(), returns=CompletedProcess(3, "inactive\n", ""))
    # check=False, so a non-zero exit must NOT raise.
    assert systemctl.is_active(fake, UNIT) is False


# ---------------------------------------------------------------------------
# start (no-op when active)
# ---------------------------------------------------------------------------


def test_start_noop_when_active():
    fake = FakeRunner()
    fake.script(_is_active_argv(), returns=CompletedProcess(0, "", ""))
    systemctl.start(fake, UNIT)
    # Only the probe ran; no start command issued.
    assert fake.argvs == [_is_active_argv()]


def test_start_acts_when_inactive():
    fake = FakeRunner()
    fake.script(_is_active_argv(), returns=CompletedProcess(3, "", ""))
    fake.script(["systemctl", "--user", "start", UNIT], returns=CompletedProcess(0, "", ""))
    systemctl.start(fake, UNIT)
    assert fake.argvs == [
        _is_active_argv(),
        ["systemctl", "--user", "start", UNIT],
    ]


def test_start_propagates_stderr():
    fake = FakeRunner()
    fake.script(_is_active_argv(), returns=CompletedProcess(3, "", ""))
    fake.script(
        ["systemctl", "--user", "start", UNIT],
        returns=CompletedProcess(1, "", "Failed to start api.service: unit not found"),
    )
    with pytest.raises(SubprocessError, match="unit not found"):
        systemctl.start(fake, UNIT)


# ---------------------------------------------------------------------------
# stop (no-op when inactive)
# ---------------------------------------------------------------------------


def test_stop_noop_when_inactive():
    fake = FakeRunner()
    fake.script(_is_active_argv(), returns=CompletedProcess(3, "", ""))
    systemctl.stop(fake, UNIT)
    assert fake.argvs == [_is_active_argv()]


def test_stop_acts_when_active():
    fake = FakeRunner()
    fake.script(_is_active_argv(), returns=CompletedProcess(0, "", ""))
    fake.script(["systemctl", "--user", "stop", UNIT], returns=CompletedProcess(0, "", ""))
    systemctl.stop(fake, UNIT)
    assert fake.argvs == [
        _is_active_argv(),
        ["systemctl", "--user", "stop", UNIT],
    ]


# ---------------------------------------------------------------------------
# restart (never idempotent)
# ---------------------------------------------------------------------------


def test_restart_argv_always_acts():
    fake = FakeRunner()
    fake.script(["systemctl", "--user", "restart", UNIT], returns=CompletedProcess(0, "", ""))
    systemctl.restart(fake, UNIT)
    assert fake.argvs == [["systemctl", "--user", "restart", UNIT]]


# ---------------------------------------------------------------------------
# unit_state (stripped stdout)
# ---------------------------------------------------------------------------


def test_unit_state_returns_active():
    fake = FakeRunner()
    fake.script(
        ["systemctl", "--user", "show", "-p", "ActiveState", "--value", UNIT],
        returns=CompletedProcess(0, "active\n", ""),
    )
    assert systemctl.unit_state(fake, UNIT) == "active"


def test_unit_state_argv():
    fake = FakeRunner()
    fake.script(
        ["systemctl", "--user", "show", "-p", "ActiveState", "--value", UNIT],
        returns=CompletedProcess(0, "failed\n", ""),
    )
    systemctl.unit_state(fake, UNIT)
    assert fake.argvs == [["systemctl", "--user", "show", "-p", "ActiveState", "--value", UNIT]]


# ---------------------------------------------------------------------------
# daemon_reload
# ---------------------------------------------------------------------------


def test_daemon_reload_argv():
    fake = FakeRunner()
    fake.script(["systemctl", "--user", "daemon-reload"], returns=CompletedProcess(0, "", ""))
    systemctl.daemon_reload(fake)
    assert fake.argvs == [["systemctl", "--user", "daemon-reload"]]


def test_daemon_reload_propagates_stderr():
    fake = FakeRunner()
    fake.script(
        ["systemctl", "--user", "daemon-reload"],
        returns=CompletedProcess(1, "", "Failed to reload daemon"),
    )
    with pytest.raises(SubprocessError, match="Failed to reload daemon"):
        systemctl.daemon_reload(fake)
