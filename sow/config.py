"""Config loading and digest computation.

``load`` parses YAML into the immutable :class:`sowConfig`. ``digest``
computes the spec digest — SHA-256 over canonical JSON (sorted keys) of the full
config **including ``source.sha``**. Including ``sha`` is required: ``update``
changes ``sha``, so the digest must change for a subsequent ``apply`` to be a
correct no-op (implementation-plan.md "Spec digest").
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from sow.models import sowConfig
from sow.state.io import read_text, write_atomic

# Default config path (XDG-strict). See stack.md §5.
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sow" / "sow.yaml"

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


def load(path: str | Path | None = None) -> sowConfig:
    """Parse and validate ``sow.yaml`` at ``path`` (default: XDG location).

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
        return sowConfig.model_validate(raw)
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


def digest(config: sowConfig) -> str:
    """SHA-256 over canonical JSON (sorted keys) of the full config.

    Includes ``source.sha`` so the digest changes when ``update`` advances a sha.
    The serialised form must be stable across runs — hence sorted keys, no
    whitespace, and ``ensure_ascii=False`` for determinism.
    """
    canonical = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_raw(path: str | Path | None) -> tuple[Path, dict]:
    """Load raw YAML dict; raises ConfigError on missing/malformed file."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    raw = yaml.safe_load(read_text(config_path))
    if not isinstance(raw, dict):
        raise ConfigError(f"config top level must be a mapping: {config_path}")
    return config_path, raw


def _write_raw(path: Path, raw: dict) -> None:
    """Atomically write a raw dict back as YAML."""
    payload = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True)
    write_atomic(path, payload.encode("utf-8"))


def add_service(
    path: str | Path | None,
    name: str,
    *,
    git: str,
    command: str,
    build: str = "",
    listen: str = "",
    ref: str | None = None,
    subpath: str = "",
    restart: str = "on-failure",
    env: dict[str, str] | None = None,
    env_file: list[str] | None = None,
) -> None:
    """Add a service definition to the config file.

    Raises ConfigError if the service already exists or the config is malformed.
    """
    config_path, raw = _read_raw(path)
    services = raw.setdefault("services", {})
    if not isinstance(services, dict):
        raise ConfigError(f"services must be a mapping: {config_path}")
    if name in services:
        raise ConfigError(f"service {name!r} already exists")
    source: dict = {"git": git}
    if ref is not None:
        source["ref"] = ref
    if subpath:
        source["path"] = subpath
    entry: dict = {"source": source, "command": command}
    if build:
        entry["build"] = build
    if listen:
        entry["listen"] = listen
    if restart != "on-failure":
        entry["restart"] = restart
    if env:
        entry["environment"] = env
    if env_file:
        entry["env_file"] = env_file
    services[name] = entry
    # Validate the new config before writing.
    try:
        sowConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{_loc(e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(
            f"invalid config after add ({len(errors)} error(s))", errors=errors
        ) from exc
    _write_raw(config_path, raw)


def remove_service(path: str | Path | None, name: str) -> None:
    """Remove a service from the config file.

    Also removes any route targets pointing to the service.
    Raises ConfigError if the service doesn't exist.
    """
    config_path, raw = _read_raw(path)
    services = raw.get("services")
    if not isinstance(services, dict) or name not in services:
        raise ConfigError(f"service {name!r} not found")
    del services[name]
    # Remove route targets pointing to this service.
    routes = raw.get("routes")
    if isinstance(routes, list):
        for route in routes:
            if isinstance(route, dict) and isinstance(route.get("paths"), dict):
                dead = [
                    p
                    for p, t in route["paths"].items()
                    if isinstance(t, dict) and t.get("to") == name
                ]
                for p in dead:
                    del route["paths"][p]
        # Drop routes with no remaining paths.
        raw["routes"] = [r for r in routes if isinstance(r, dict) and r.get("paths")]
        if not raw["routes"]:
            del raw["routes"]
    if not services:
        del raw["services"]
        _write_raw(config_path, raw)
        return
    try:
        sowConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{_loc(e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(
            f"invalid config after remove ({len(errors)} error(s))", errors=errors
        ) from exc
    _write_raw(config_path, raw)


def add_route(
    path: str | Path | None,
    host: str,
    prefix: str,
    to: str,
) -> None:
    """Add a path-prefix route to the config file.

    If a route for ``host`` already exists, the prefix is added to it.
    Raises ConfigError if the prefix already exists on that host or the config is malformed.
    """
    config_path, raw = _read_raw(path)
    routes: list = raw.setdefault("routes", [])
    if not isinstance(routes, list):
        raise ConfigError(f"routes must be a list: {config_path}")
    for route in routes:
        if isinstance(route, dict) and route.get("host") == host:
            paths = route.setdefault("paths", {})
            if not isinstance(paths, dict):
                raise ConfigError(f"paths for host {host!r} must be a mapping")
            if prefix in paths:
                raise ConfigError(f"route {host!r}{prefix!r} already exists")
            paths[prefix] = {"to": to}
            break
    else:
        routes.append({"host": host, "paths": {prefix: {"to": to}}})
    try:
        sowConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{_loc(e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(
            f"invalid config after route add ({len(errors)} error(s))", errors=errors
        ) from exc
    _write_raw(config_path, raw)


def remove_route(
    path: str | Path | None,
    host: str,
    prefix: str | None = None,
) -> None:
    """Remove a route path-prefix, or the whole host if no prefix given.

    Raises ConfigError if the host (or prefix) is not found.
    """
    config_path, raw = _read_raw(path)
    routes = raw.get("routes")
    if not isinstance(routes, list):
        raise ConfigError("no routes defined")
    for idx, route in enumerate(routes):
        if isinstance(route, dict) and route.get("host") == host:
            if prefix is None:
                del routes[idx]
                if not routes:
                    del raw["routes"]
            else:
                assert prefix is not None  # narrowed by the if/else above
                raw_paths = route.get("paths")
                if not isinstance(raw_paths, dict) or prefix not in raw_paths:
                    raise ConfigError(f"route {host!r}{prefix!r} not found")
                paths = cast(dict, raw_paths)
                del paths[prefix]
                if not paths:
                    del routes[idx]
                    if not routes:
                        del raw["routes"]
            break
    else:
        raise ConfigError(f"host {host!r} not found in routes")
    try:
        sowConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{_loc(e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(
            f"invalid config after route remove ({len(errors)} error(s))", errors=errors
        ) from exc
    _write_raw(config_path, raw)


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
