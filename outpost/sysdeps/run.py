"""The strategy seam for all subprocess execution in Outpost.

This module is, by rule (``AGENTS.md`` / ``stack.md`` §5), the **only** place
``subprocess.run`` is called. Every sysdeps wrapper (``git``, ``systemctl``,
``nginx``, ``journalctl``) is a client of the :class:`Runner` protocol: it builds
an argv and calls ``runner.run(...)``. Production wires :class:`RealRunner`; tests
wire ``tests.mocks.runner.FakeRunner``. That single injection point is what makes
exact-command-line, idempotency, and stderr-propagation assertions possible
without touching the host.

The exception contract: ``runner.run`` never raises ``subprocess.CalledProcessError``.
On a non-zero exit with ``check=True`` it raises our own :class:`SubprocessError`,
which carries the argv, returncode, and both streams — everything the CLI needs to
print a precise remediation message. Wrappers and the engine therefore catch and
propagate exactly one exception type.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class SubprocessError(Exception):
    """Raised when ``check=True`` and a command exits non-zero.

    ``stderr`` is the load-bearing field (most tools write their diagnostics
    there); ``stdout`` is included because ``nginx -t`` writes its "test is
    successful" line to stdout and some tools mix diagnostics across streams.
    ``argv`` lets the operator see exactly what ran.
    """

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def __init__(self, argv: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"command failed (exit {returncode}): {' '.join(argv)}\nstderr: {stderr}"
        )


@dataclass(frozen=True)
class CompletedProcess:
    """Immutable result of a process run.

    Frozen so results can be shared and compared (equality on the three fields).
    A dataclass rather than a hand-rolled class: it gives ``__eq__``/``__hash__``
    for free and matches the repo's value-type ethos, while keeping this stdlib
    module free of any Pydantic dependency.
    """

    returncode: int
    stdout: str
    stderr: str


@runtime_checkable
class Runner(Protocol):
    """The swappable subprocess strategy every wrapper depends on.

    ``argv`` is positional; ``cwd``/``env``/``check`` are keyword-only so a test
    asserting on the exact argv reads cleanly. ``check`` defaults to ``True``
    because fail-fast is the common case; the read-only probes (``is_active``,
    ``unit_state``) pass ``check=False`` explicitly, making the deviation visible
    at the call site.
    """

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CompletedProcess: ...


class RealRunner:
    """The production :class:`Runner`; the sole ``subprocess.run`` call site.

    Captures stdout+stderr as text and never lets ``subprocess.CalledProcessError``
    escape — non-zero exits with ``check=True`` become :class:`SubprocessError`
    here, so a single typed exception flows to the wrappers and the engine.
    """

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CompletedProcess:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            check=False,  # we raise ourselves so the exception carries our type + fields
        )
        result = CompletedProcess(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            raise SubprocessError(
                argv=argv,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result
