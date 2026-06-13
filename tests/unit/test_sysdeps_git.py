"""Tests for the git wrappers — exact argv, idempotency, stderr propagation.

DoD core: assert the exact command lines; verify ``clone``/``checkout`` no-op
when the desired state is met; verify a failed command surfaces as
:class:`SubprocessError` with its stderr.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outpost.sysdeps import git
from outpost.sysdeps.run import CompletedProcess, SubprocessError
from tests.mocks import FakeRunner

REPO = Path("/srv/outpost/repos/api")
URL = "https://github.com/me/svc.git"


# ---------------------------------------------------------------------------
# clone
# ---------------------------------------------------------------------------


def test_clone_argv(tmp_path: Path):
    fake = FakeRunner()
    dest = tmp_path / "api"
    fake.script(["git", "clone", "--quiet", URL, str(dest)], returns=CompletedProcess(0, "", ""))
    git.clone(fake, URL, dest)
    assert fake.argvs == [["git", "clone", "--quiet", URL, str(dest)]]


def test_clone_noop_if_dotgit_exists(tmp_path: Path):
    (tmp_path / "api" / ".git").mkdir(parents=True)
    fake = FakeRunner()
    git.clone(fake, URL, tmp_path / "api")
    assert fake.calls == []


def test_clone_propagates_stderr(tmp_path: Path):
    fake = FakeRunner()
    fake.script(
        ["git", "clone", "--quiet", URL, str(tmp_path / "api")],
        returns=CompletedProcess(128, "", "fatal: repository not found"),
    )
    with pytest.raises(SubprocessError, match="fatal: repository not found") as exc_info:
        git.clone(fake, URL, tmp_path / "api")
    assert exc_info.value.stderr == "fatal: repository not found"
    assert exc_info.value.argv == ["git", "clone", "--quiet", URL, str(tmp_path / "api")]


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def test_fetch_argv():
    fake = FakeRunner()
    fake.script(
        ["git", "-C", str(REPO), "fetch", "--quiet", "--all"], returns=CompletedProcess(0, "", "")
    )
    git.fetch(fake, REPO)
    assert fake.argvs == [["git", "-C", str(REPO), "fetch", "--quiet", "--all"]]


# ---------------------------------------------------------------------------
# checkout (idempotent via current_sha)
# ---------------------------------------------------------------------------


def test_checkout_argv():
    fake = FakeRunner()
    fake.script(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], returns=CompletedProcess(0, "other\n", "")
    )
    fake.script(
        ["git", "-C", str(REPO), "checkout", "--quiet", "abc123"],
        returns=CompletedProcess(0, "", ""),
    )
    git.checkout(fake, REPO, "abc123")
    assert fake.argvs == [
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        ["git", "-C", str(REPO), "checkout", "--quiet", "abc123"],
    ]


def test_checkout_noop_when_sha_matches():
    fake = FakeRunner()
    fake.script(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], returns=CompletedProcess(0, "abc123\n", "")
    )
    git.checkout(fake, REPO, "abc123")
    # Only the probe ran; no checkout command was issued.
    assert fake.argvs == [["git", "-C", str(REPO), "rev-parse", "HEAD"]]


# ---------------------------------------------------------------------------
# resolve_ref / current_sha (return stripped stdout)
# ---------------------------------------------------------------------------


def test_resolve_ref_argv_and_strips_stdout():
    fake = FakeRunner()
    fake.script(
        ["git", "-C", str(REPO), "rev-parse", "main"],
        returns=CompletedProcess(0, "abc123fullsha\n", ""),
    )
    assert git.resolve_ref(fake, REPO, "main") == "abc123fullsha"
    assert fake.argvs == [["git", "-C", str(REPO), "rev-parse", "main"]]


def test_current_sha_argv_and_strips_stdout():
    fake = FakeRunner()
    fake.script(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        returns=CompletedProcess(0, "deadbeef\n", ""),
    )
    assert git.current_sha(fake, REPO) == "deadbeef"


def test_resolve_ref_propagates_stderr_for_dead_ref():
    fake = FakeRunner()
    fake.script(
        ["git", "-C", str(REPO), "rev-parse", "ghost"],
        returns=CompletedProcess(128, "", "fatal: ambiguous argument 'ghost'"),
    )
    with pytest.raises(SubprocessError, match="ambiguous argument"):
        git.resolve_ref(fake, REPO, "ghost")
