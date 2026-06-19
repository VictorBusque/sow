"""Config loading and digest computation.

``load`` parses YAML into the immutable :class:`OutpostConfig`. ``digest``
computes the spec digest — SHA-256 over canonical JSON (sorted keys) of the full
config **including ``source.sha``**. Including ``sha`` is required: ``update``
changes ``sha``, so the digest must change for a subsequent ``apply`` to be a
correct no-op (implementation-plan.md "Spec digest").
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from outpost.models import OutpostConfig
from outpost.state.io import read_text, write_atomic

# Default config path (XDG-strict). See stack.md §5.
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "outpost" / "outpost.yaml"

# Exit codes (shared with the CLI in Phase 7; duplicated here only for the
# loader's typed result — see cli-reference.md "Exit codes").
EXIT_OK = 0
EXIT_OPERATIONAL = 1
EXIT_INVALID_CONFIG = 2


class ConfigError(Exception):
    """Raised when a config file cannot be loaded or fails validation.

    Carries the list of human-readable validation messages for the CLI to print.
    """

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or [message]


def load(path: str | Path | None = None) -> OutpostConfig:
    """Parse and validate ``outpost.yaml`` at ``path`` (default: XDG location).

    Raises :class:`ConfigError` on a missing file, malformed YAML, or a
    validation failure. Never mutates the filesystem.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid YAML: {config_path}: {exc}") from exc

    if raw is None:
        raise ConfigError(f"config is empty: {config_path}")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config top level must be a mapping, got {type(raw).__name__}: {config_path}"
        )

    try:
        return OutpostConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{_loc(e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(
            f"config validation failed ({len(errors)} error(s))", errors=errors
        ) from exc


def _loc(loc: tuple[object, ...]) -> str:
    """Render a Pydantic error location tuple as a dotted/quoted path."""
    parts: list[str] = []
    for item in loc:
        parts.append(str(item))
    return ".".join(parts) if parts else "<root>"


def digest(config: OutpostConfig) -> str:
    """SHA-256 over canonical JSON (sorted keys) of the full config.

    Includes ``source.sha`` so the digest changes when ``update`` advances a sha.
    The serialised form must be stable across runs — hence sorted keys, no
    whitespace, and ``ensure_ascii=False`` for determinism.
    """
    canonical = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_source_fields(
    path: str | Path | None, name: str, *, sha: str | None = None, ref: str | None = None
) -> None:
    """Targeted write-back of one service's ``source.{sha,ref}`` into the YAML.

    Used by ``apply`` (the one-time ``sha`` seed) and ``update`` (advancing
    ``sha``/``ref``). Rather than round-tripping the whole Pydantic model — which
    would reshape operator fields like the list/map ``environment`` form — this
    loads the raw YAML, mutates only the named source fields, and writes back
    atomically. Comments are not preserved (a PyYAML limitation; comment-preserving
    round-trips would need a new dependency, declined for v1).

    Raises :class:`ConfigError` if the service or its ``source`` block is absent.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(read_text(config_path))
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
        raise ConfigError(f"cannot write back: config is malformed: {config_path}")
    services = raw["services"]
    if name not in services or not isinstance(services[name], dict):
        raise ConfigError(f"cannot write back: service {name!r} not in config: {config_path}")
    source = services[name].setdefault("source", {})
    if not isinstance(source, dict):
        raise ConfigError(f"cannot write back: source block of {name!r} is malformed")
    if sha is not None:
        source["sha"] = sha
    if ref is not None:
        source["ref"] = ref
    payload = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True)
    write_atomic(config_path, payload.encode("utf-8"))
