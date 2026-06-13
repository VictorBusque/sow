"""A recording, scriptable stand-in for ``RealRunner`` — the test-side strategy.

The whole point of the :class:`~outpost.sysdeps.run.Runner` seam is that wrappers
can be exercised without a host. ``FakeRunner`` records every ``run`` call into
``self.calls`` (so a test asserts on the *exact* argv, cwd, env, and check) and
resolves a canned :class:`~outpost.sysdeps.run.CompletedProcess` per call, keyed
on the **exact** ``tuple(argv)``. Matching is order-sensitive and deliberately
has no globbing: the Phase 2 DoD demands exact command lines, and fuzzy matching
would let a wrong argv slip through.

When the canned returncode is non-zero and the call passed ``check=True`` the
FakeRunner raises the *real* :class:`~outpost.sysdeps.run.SubprocessError` — so a
stderr-propagation test asserts on the same exception type and fields production
raises. An unscripted argv raises :class:`FakeRunnerNotScripted` (an
``AssertionError``), failing the test loudly rather than silently returning a
default: a forgotten script is almost always a bug in the test or a real wrapper
emitting an unexpected command.

Example::

    fake = FakeRunner()
    fake.script(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        returns=CompletedProcess(0, "abc123\\n", ""),
    )
    sha = git.current_sha(fake, repo)
    assert sha == "abc123"
    assert fake.calls[0].argv == ["git", "-C", str(repo), "rev-parse", "HEAD"]
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from outpost.sysdeps.run import CompletedProcess, SubprocessError

__all__ = ["FakeRunner", "FakeRunnerNotScripted", "RecordedCall"]


@dataclass(frozen=True)
class RecordedCall:
    """One invocation of ``FakeRunner.run``, captured for assertion."""

    argv: list[str]
    cwd: Path | None
    env: Mapping[str, str] | None
    check: bool


# Handlers are stored in a single uniform shape — a callable that takes the
# recorded call and returns a CompletedProcess. A fixed ``returns`` is wrapped
# into such a callable at ``script`` time. This keeps the dict type simple for
# the type checker (no callable/literal union) and lets ``returns_fn`` support
# stateful faking (e.g. polling).
_Handler = Callable[[RecordedCall], CompletedProcess]


def _constant(result: CompletedProcess) -> _Handler:
    """Wrap a fixed result as a handler callable (avoids a late-binding lambda)."""

    def handler(_call: RecordedCall) -> CompletedProcess:
        return result

    return handler


class FakeRunner:
    """Records calls and returns scripted responses by exact argv match."""

    _responses: dict[tuple[str, ...], _Handler]
    calls: list[RecordedCall]

    def __init__(self) -> None:
        self._responses = {}
        self._default: _Handler | None = None
        self.calls = []

    def script(
        self,
        argv: list[str],
        *,
        returns: CompletedProcess | None = None,
        returns_fn: Callable[[RecordedCall], CompletedProcess] | None = None,
    ) -> None:
        """Register a response for an exact ``argv`` (matched as a tuple).

        Pass exactly one of ``returns`` (a fixed result) or ``returns_fn``
        (a callable inspected per call, for stateful faking such as polling).
        """
        if (returns is None) == (returns_fn is None):
            raise TypeError("script: pass exactly one of returns= or returns_fn=")
        if returns_fn is not None:
            handler: _Handler = returns_fn
        else:
            assert returns is not None  # narrowed by the TypeError guard above
            handler = _constant(returns)
        self._responses[tuple(argv)] = handler

    def script_default(self, returns: CompletedProcess) -> None:
        """Fallback for any unscripted argv. Prefer exact ``script`` calls."""
        self._default = _constant(returns)

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CompletedProcess:
        recorded = RecordedCall(argv=argv, cwd=cwd, env=env, check=check)
        self.calls.append(recorded)
        response = self._resolve(recorded)
        if check and response.returncode != 0:
            raise SubprocessError(
                argv=argv,
                returncode=response.returncode,
                stdout=response.stdout,
                stderr=response.stderr,
            )
        return response

    @property
    def argvs(self) -> list[list[str]]:
        """Just the argv lists, in call order (convenience for assertions)."""
        return [call.argv for call in self.calls]

    def _resolve(self, call: RecordedCall) -> CompletedProcess:
        handler = self._responses.get(tuple(call.argv))
        if handler is not None:
            return handler(call)
        if self._default is not None:
            return self._default(call)
        raise FakeRunnerNotScripted(call)


class FakeRunnerNotScripted(AssertionError):
    """Raised when ``FakeRunner`` gets a call it was not scripted for.

    Subclasses ``AssertionError`` so pytest reports it as a failure, with the
    unscripted argv named in the message.
    """

    def __init__(self, call: RecordedCall) -> None:
        super().__init__(
            f"FakeRunner got an unscripted call: argv={call.argv} cwd={call.cwd} "
            f"check={call.check}. Call .script([...], returns=...) first."
        )
