"""Builders for valid config dicts in tests.

Keeps the noise out of the test bodies: a minimal valid service, a full config,
and small mutators. All return plain dicts so tests can tweak one field and
re-parse through the model to assert a validation error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def minimal_service(
    *,
    git: str = "https://github.com/me/svc.git",
    command: str = "./run",
    **overrides: Any,
) -> dict[str, Any]:
    """A minimal valid service spec; override any field via kwargs."""
    svc: dict[str, Any] = {
        "source": {"git": git, "ref": "main", "sha": "abc1234"},
        "command": command,
    }
    svc.update(overrides)
    return svc


def minimal_config(
    services: Mapping[str, dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A minimal valid config with one service and no routes/exposure."""
    cfg: dict[str, Any] = {
        "version": 1,
        "services": dict(services) if services else {"api": minimal_service()},
    }
    cfg.update(overrides)
    return cfg
