"""NGINX wrappers: ``nginx -t`` validation and a ``systemctl --user reload``.

The user-level NGINX is a systemd-managed unit named :data:`NGINX_UNIT`; per
``prd.md`` §169, Outpost "reloads via ``systemctl --user reload``" — never
``nginx -s reload``, no sudo, no sudoers. Routing the reload through systemd keeps
its view of the process authoritative and avoids finding a PID file.

``nginx -t`` needs a *complete* ``nginx.conf`` (it cannot test bare server blocks),
so the engine stages one that ``include``s the rendered blocks, then validates it
with both ``-c <conf>`` and ``-p <staging>`` (the prefix lets relative paths in the
conf resolve against the staging dir). On success ``nginx -t`` prints to stdout;
on failure its diagnostics go to stderr — and ``check=True`` surfaces them via
:class:`~outpost.sysdeps.run.SubprocessError`.
"""

from __future__ import annotations

from pathlib import Path

from outpost.sysdeps.run import Runner, SubprocessError
from outpost.sysdeps.systemctl import daemon_reload

__all__ = ["NGINX_UNIT", "SubprocessError", "daemon_reload", "reload", "test"]

# The user NGINX unit name. The docs never literally pin the string; "outpost-nginx"
# avoids any collision with a system nginx unit. ``init`` (Phase 9) creates the unit
# under this name; if it ever chooses another, change it here in one place.
NGINX_UNIT: str = "outpost-nginx"


def test(runner: Runner, conf_path: Path, *, prefix: Path | None = None) -> None:
    """Validate an NGINX config via ``nginx -t``.

    ``-p prefix`` is prepended so relative paths in the throwaway conf resolve
    against the staging dir; the engine passes both ``-c`` and ``-p``.
    """
    argv: list[str] = ["nginx"]
    if prefix is not None:
        argv += ["-p", str(prefix)]
    argv += ["-t", "-c", str(conf_path)]
    runner.run(argv)


def reload(runner: Runner) -> None:
    """Reload the user NGINX via systemd (``prd.md`` §169).

    systemd stays the authority: this is the same supervision path as
    start/stop/restart, not a direct signal to the process.
    """
    runner.run(["systemctl", "--user", "reload", NGINX_UNIT])
