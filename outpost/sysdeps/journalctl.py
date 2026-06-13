"""journalctl --user wrapper for bounded log tails.

``--no-pager`` is mandatory: without it journalctl tries to talk to a tty and
blocks. ``-n N`` bounds the tail to the last ``N`` lines (default ``200`` per
``cli-reference.md`` "logs"), keeping agent/terminal output manageable. The
returned text never includes environment contents — it is the raw journal.
"""

from __future__ import annotations

from outpost.sysdeps.run import Runner, SubprocessError

__all__ = ["DEFAULT_LINES", "SubprocessError", "tail"]

# Default tail length (cli-reference.md "logs"). Bounded so an MCP/CLI consumer
# never pulls the whole journal.
DEFAULT_LINES: int = 200


def tail(runner: Runner, unit: str, lines: int = DEFAULT_LINES) -> str:
    """Return the last ``lines`` journal lines for ``unit`` (user scope)."""
    result = runner.run(["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager"])
    return result.stdout
