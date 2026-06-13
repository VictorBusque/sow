"""Git subprocess wrappers (clone / fetch / checkout / resolve_ref / current_sha).

Every function takes ``runner`` as its first argument and builds a git argv — it
never imports ``subprocess`` (``run.py`` is the only call site). Git is driven via
``-C <dir>`` rather than ``cwd=`` because passing the repo on the command line is
more robust than relying on PWD inside worker clones.

Idempotency is read-then-act through the same runner: ``clone`` no-ops when the
destination already has a ``.git`` directory, ``checkout`` no-ops when the pinned
sha is already ``HEAD``. The engine therefore just calls ``checkout(runner, dir,
sha)`` and trusts it not to redo work — the wrapper is the single authority on
"the desired state is already met."
"""

from __future__ import annotations

from pathlib import Path

from outpost.sysdeps.run import Runner, SubprocessError

__all__ = ["SubprocessError", "checkout", "clone", "current_sha", "fetch", "resolve_ref"]


def clone(runner: Runner, url: str, dest: Path) -> None:
    """Clone ``url`` into ``dest`` if it is not already a git repo.

    No-op when ``(dest / ".git")`` exists: a pathlib check, cheaper than a
    subprocess and race-free enough for v1's single-operator assumption.
    """
    if (dest / ".git").is_dir():
        return
    runner.run(["git", "clone", "--quiet", url, str(dest)])


def fetch(runner: Runner, repo_dir: Path) -> None:
    """Fetch all remotes. Fetch is inherently idempotent, so it always runs."""
    runner.run(["git", "-C", str(repo_dir), "fetch", "--quiet", "--all"])


def checkout(runner: Runner, repo_dir: Path, sha: str) -> None:
    """Check out ``sha`` unless it is already ``HEAD`` (avoids touching mtimes)."""
    if current_sha(runner, repo_dir) == sha:
        return
    runner.run(["git", "-C", str(repo_dir), "checkout", "--quiet", sha])


def resolve_ref(runner: Runner, repo_dir: Path, ref: str) -> str:
    """Resolve a ref (branch/tag/sha) to its full SHA via ``git rev-parse``.

    ``ref`` is a ``str``: resolving ``Source.ref is None`` ("track remote default
    branch") to a concrete ref is engine policy (Phase 5), not this wrapper's job.
    An absent ref is a fail-fast error (the config named a dead branch).
    """
    result = runner.run(["git", "-C", str(repo_dir), "rev-parse", ref])
    return result.stdout.strip()


def current_sha(runner: Runner, repo_dir: Path) -> str:
    """The full SHA currently checked out at ``HEAD``."""
    result = runner.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"])
    return result.stdout.strip()
