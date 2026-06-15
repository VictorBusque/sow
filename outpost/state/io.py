"""Atomic filesystem writes — the single helper for every persistent mutation.

Outpost's atomicity rule (``AGENTS.md`` / implementation-plan.md "Atomic write
helper"): every state and config rewrite goes through a temp file + ``os.replace``.
``os.replace`` is atomic on POSIX (a single rename syscall) so a reader never sees
a half-written file — even across a crash mid-write. This is the *only* place that
pattern lives; ``state.json`` and every ``outpost.yaml`` rewrite (seed, ``update``)
call into it, so the guarantee is tested once and reused everywhere.

``write_atomic`` is bytes-in: callers own encoding (the state store emits UTF-8
JSON; the config rewriter emits UTF-8 YAML). The temp file is written next to the
target (same directory, same filesystem) so the rename is guaranteed not to be a
cross-device copy. We ``fsync`` the temp file before renaming so the data (not
just the rename) is durable — without it, a crash after the rename but before the
kernel flushed the file's data could leave an empty file in place.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

__all__ = ["read_text", "write_atomic"]


def write_atomic(path: str | Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` via a sibling temp + ``os.replace``.

    The temp file lives in the same directory as ``path`` (so the final rename is
    intra-filesystem and atomic), is ``fsync``-ed before the rename, and is
    cleaned up on any write error. Parent directories are **not** created here —
    callers (``init`` / the apply pipeline) own runtime-tree creation; creating
    them implicitly would mask a misconfigured layout.
    """
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Remove the half-written temp so a later, successful run isn't confused
        # by a stale ``.tmp``. Swallow the unlink error: the original failure is
        # the one to surface.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def read_text(path: str | Path) -> str:
    """Read ``path`` as UTF-8 text. The atomic write counterpart for callers that
    only need text (config load, state read)."""
    return Path(path).read_text(encoding="utf-8")
