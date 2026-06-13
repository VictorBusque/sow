"""systemctl --user wrappers.

Every argv carries ``--user``: Outpost is rootless (``AGENTS.md``), so services
run as user units and there is no ``--system`` path anywhere. These cover the
apply/update lifecycle surface (``rfc.md`` §18): start / stop / restart / status
per service, plus ``daemon-reload`` after a unit-file swap.

Idempotency is read-then-act through the same runner: ``start`` and ``stop``
probe ``is_active`` first and skip when the desired state is already met, so the
engine can call them unconditionally. ``restart`` is not idempotent by nature —
the engine calls it only for units whose file changed. ``is_active`` is the
canonical state probe: it runs with ``check=False`` because a non-zero exit
("inactive") is the expected, non-erroneous answer.
"""

from __future__ import annotations

from outpost.sysdeps.run import Runner, SubprocessError

__all__ = [
    "SubprocessError",
    "daemon_reload",
    "is_active",
    "restart",
    "start",
    "stop",
    "unit_state",
]


def is_active(runner: Runner, unit: str) -> bool:
    """True iff systemd reports ``unit`` active.

    ``is-active --quiet`` exits 0 when active and non-zero otherwise; the
    non-zero case is the *answer*, not an error, so ``check=False``.
    """
    result = runner.run(["systemctl", "--user", "is-active", "--quiet", unit], check=False)
    return result.returncode == 0


def start(runner: Runner, unit: str) -> None:
    """Start ``unit`` unless it is already active."""
    if is_active(runner, unit):
        return
    runner.run(["systemctl", "--user", "start", unit])


def stop(runner: Runner, unit: str) -> None:
    """Stop ``unit`` unless it is already inactive."""
    if not is_active(runner, unit):
        return
    runner.run(["systemctl", "--user", "stop", unit])


def restart(runner: Runner, unit: str) -> None:
    """Restart ``unit``. Not idempotent — the engine calls it only for affected units."""
    runner.run(["systemctl", "--user", "restart", unit])


def unit_state(runner: Runner, unit: str) -> str:
    """The ``ActiveState`` of ``unit`` (e.g. ``active``/``inactive``/``failed``).

    Matches the ``unit`` field surfaced by the ``status``/``get_service_status``
    commands (``cli-reference.md``).
    """
    result = runner.run(
        ["systemctl", "--user", "show", "-p", "ActiveState", "--value", unit]
    )
    return result.stdout.strip()


def daemon_reload(runner: Runner) -> None:
    """Reload the user systemd manager, picking up swapped unit files.

    Called by ``apply`` after the atomic unit-file swap (``prd.md`` §9 step 8).
    """
    runner.run(["systemctl", "--user", "daemon-reload"])
