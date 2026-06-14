"""Render an immutable config model into a systemd user unit.

The pipeline is: model → a structured :class:`UnitSpec` value → Jinja2 template.
Keeping the Python side responsible for *all* policy (env precedence, ``${VAR}``
interpolation, path resolution) and the template responsible only for
presentation means the template stays thin and the logic is unit-testable
without rendering.

Env precedence (config-schema.md §"Platform-injected environment"):
platform-injected (``PORT``/``ADDRESS``/``DATA_DIR``) highest, then inline
``environment``. ``${VAR}`` interpolation is resolved at render time against the
*platform-injected* set — the only vars that exist deterministically at generate
time. An unknown ``${VAR}`` fails fast rather than emitting a literal.

Service references (config-schema.md §"Service references"):
``${VAR}`` also resolves *cross-service* facts via ``${<name>.<FIELD>}``, where
``<name>`` is another service and ``<FIELD>`` is one of the platform facts
(``ADDRESS``, ``PORT``, ``DATA_DIR``). This lets a service reach another without
knowing its (possibly auto-allocated) address ahead of time — e.g.
``URL_B: http://${b.ADDRESS}``. Bare ``${ADDRESS}`` is shorthand for
``${<self>.ADDRESS}``. Only platform facts are exposed cross-service; another
service's operator-defined env is **never** referencable (it may carry secrets,
which must not be copied into other services' unit files — see prd.md §7). The
fact table is :func:`compute_facts`, built from the config + the port
allocation; it is passed into :func:`build_spec` so rendering needs no allocator
coupling of its own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template

from outpost.models import OutpostConfig, Service

__all__ = ["RenderError", "UnitSpec", "build_spec", "compute_facts", "render_unit"]

# ${VAR} — bare self-reference (letters/digits/underscore, braced only; we don't
# interpolate bare $FOO). The name never contains a dot.
# ${svc.FIELD} — cross-service reference to another service's platform fact.
# The two forms share the ``${ ... }`` envelope; the dot discriminates.
_VAR_RE: re.Pattern[str] = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\}")

# Sane crash-loop throttle (config-schema.md: "throttled, not hammering").
_RESTART_SEC: str = "5"
_START_LIMIT_BURST: str = "5"
_START_LIMIT_INTERVAL_SEC: str = "30"

# Runtime layout (XDG-strict, AGENTS.md "Paths").
_REPO_ROOT = Path.home() / ".local" / "share" / "outpost" / "repos"
_DATA_ROOT = Path.home() / ".local" / "share" / "outpost" / "data"

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


class RenderError(Exception):
    """Raised when a service cannot be rendered (e.g. an unknown ``${VAR}``)."""


# The platform facts every service has. These — and ONLY these — are exposed to
# other services via cross-service references. Operator-defined env is excluded
# by design (it may carry secrets; copying it across unit files would violate
# prd.md §7's "never copy secret values into broadly-readable generated files").
_FACT_FIELDS: tuple[str, ...] = ("ADDRESS", "PORT", "DATA_DIR")


@dataclass(frozen=True)
class _Scope:
    """The interpolation scope for one service's render.

    ``facts`` maps ``service_name -> {ADDRESS, PORT, DATA_DIR}`` for *every*
    service in the config (so cross-service refs resolve). ``self_env`` is the
    service being rendered's own merged env (inline ``environment`` + its
    platform fact row) — what bare ``${VAR}`` resolves against. Keeping the two
    channels separate is the security boundary: bare refs see this service's
    operator env (its own secrets, fine), but dotted refs read ONLY the platform
    facts table, never another service's operator env.
    """

    facts: Mapping[str, Mapping[str, str]]
    self_env: Mapping[str, str]


def compute_facts(config: OutpostConfig, ports: Mapping[str, int]) -> dict[str, dict[str, str]]:
    """Build the ``{service -> {ADDRESS, PORT, DATA_DIR}}`` fact table.

    ``ports`` is the allocator output from :func:`~outpost.engine.ports.allocate_all`.
    It only contains entries for **listen-less** services — declared-listen
    services derive their address/port from ``listen`` and get ``None`` from
    ``ports.get()``. Every service gets an ``ADDRESS`` and ``DATA_DIR``;
    ``PORT`` is present only for TCP services (absent for unix sockets, which
    carry no port).

    This is the single source of truth for cross-service references: the fact
    table is what ``${svc.FIELD}`` reads against, and what a service's own
    platform-injected env is built from.
    """
    facts: dict[str, dict[str, str]] = {}
    for name, svc in config.services.items():
        facts[name] = _build_self_fact(name, svc, ports.get(name))
    return facts


def _resolve_address(
    name: str, service: Service, port: int | None
) -> tuple[str, int | None]:
    """Resolve a service's ``(address, port)`` pair.

    ``address`` is ``host:port`` (TCP) or the socket path (unix). ``port`` is the
    TCP port — declared, allocated, or ``None`` for unix sockets. Raises if a
    listen-less service was given no allocated port (a caller bug, not an
    operator config error — the allocator must run before rendering).
    """
    if service.is_unix_listen:
        return service.listen, None
    if service.has_listen:
        assert service.parsed_listen_port() is not None
        return service.listen, service.parsed_listen_port()
    if port is not None:
        return f"127.0.0.1:{port}", port
    raise RenderError(f"service {name!r} has no listen and no allocated port; cannot render")


def _build_self_fact(name: str, service: Service, port: int | None) -> dict[str, str]:
    """Build the platform-fact dict for a single service.

    ``port`` is the allocated TCP port, or ``None`` for unix-socket services and
    for services with a declared TCP ``listen`` (the port is embedded in the
    address in those cases). The result always has ``ADDRESS`` and ``DATA_DIR``;
    ``PORT`` is present only for TCP services.

    Called by both :func:`compute_facts` (the cross-service fact table builder)
    and :func:`build_spec` (the single-service fallback), so the self-fact
    values are guaranteed identical whether or not a fact table was supplied.
    """
    address, resolved_port = _resolve_address(name, service, port)
    fact: dict[str, str] = {
        "ADDRESS": address,
        "DATA_DIR": str(_DATA_ROOT / name),
    }
    if resolved_port is not None and not service.is_unix_listen:
        fact["PORT"] = str(resolved_port)
    return fact


@dataclass(frozen=True)
class UnitSpec:
    """The fully-resolved data a unit template renders from.

    Every field is plain text (the template does no policy). Environment entries
    are pre-formatted ``KEY=value`` lines in precedence order; env files are the
    operator-declared paths verbatim.
    """

    description: str
    working_directory: str
    environment: list[str] = field(default_factory=list)
    environment_files: list[str] = field(default_factory=list)
    exec_start: str = ""
    restart: str = "on-failure"
    restart_sec: str = _RESTART_SEC
    start_limit_burst: str = _START_LIMIT_BURST
    start_limit_interval_sec: str = _START_LIMIT_INTERVAL_SEC


def _template() -> Template:
    """Load the service unit template (shipped with the package)."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("service.j2")


def render_unit(
    name: str,
    service: Service,
    port: int | None,
    facts: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Render ``service``'s systemd unit.

    ``port`` is the service's TCP port — declared (from ``listen``) or allocated.
    ``None`` marks a unix-socket service (then ``PORT`` is omitted and
    ``ADDRESS`` is the socket path).

    ``facts`` is the cross-service fact table (:func:`compute_facts`). Omit it
    to resolve only bare ``${VAR}`` self-references (single-service scope); pass
    it to also resolve ``${other.FIELD}`` references to other services.
    """
    spec = build_spec(name, service, port, facts=facts)
    return _template().render(spec=spec)


def build_spec(
    name: str,
    service: Service,
    port: int | None,
    *,
    facts: Mapping[str, Mapping[str, str]] | None = None,
) -> UnitSpec:
    """Compute the resolved :class:`UnitSpec` for ``service``.

    Separated from :func:`render_unit` so tests assert on structured data without
    going through the template.

    ``facts`` is the cross-service fact table (:func:`compute_facts`). When
    omitted, a single-service scope is built (bare ``${VAR}`` only); when
    provided, ``${other.FIELD}`` references resolve against it.
    """
    # When a fact table was supplied, reuse it so self-references and
    # cross-service references see identical values; otherwise derive the
    # self fact via the same helper that :func:`compute_facts` uses.
    self_fact = facts[name] if facts is not None else _build_self_fact(name, service, port)

    # Precedence: platform-injected wins over inline. Interpolation resolves
    # against the merged set so `${DATA_DIR}` in inline env works, and against
    # the fact table so `${b.ADDRESS}` resolves cross-service.
    merged: dict[str, str] = {**dict(service.environment.items()), **self_fact}
    scope = _Scope(
        facts=facts if facts is not None else {name: self_fact},
        self_env=merged,
    )
    resolved = {k: _interpolate(name, v, scope) for k, v in merged.items()}
    environment_lines = [f"{k}={v}" for k, v in resolved.items()]

    # ExecStart = command + interpolated args.
    exec_args = [service.command, *(_interpolate(name, a, scope) for a in service.args)]
    exec_start = " ".join(exec_args)

    clone_dir = _REPO_ROOT / name
    working_dir = clone_dir / service.source.path if service.source.path else clone_dir

    return UnitSpec(
        description=f"Outpost service: {name}",
        working_directory=str(working_dir),
        environment=environment_lines,
        environment_files=list(service.env_file),
        exec_start=exec_start,
        restart=service.restart,
    )


def _interpolate(name: str, value: str, scope: _Scope) -> str:
    """Resolve ``${VAR}`` and ``${svc.FIELD}`` references for service ``name``.

    Bare ``${VAR}`` resolves against this service's own merged env (its inline
    ``environment`` plus its platform fact row). Dotted ``${svc.FIELD}`` resolves
    against that service's fact row. A reference to an unknown service, an
    unexposed field, or a field the target lacks (e.g. ``PORT`` on a unix-socket
    service) fails fast rather than emitting a literal. Operator-defined env is
    never reachable cross-service — only the platform facts in
    :data:`_FACT_FIELDS`.
    """

    def replace(match: re.Match[str]) -> str:
        ref = match.group("name")
        if "." in ref:
            svc_name, _, field_name = ref.rpartition(".")
            target = scope.facts.get(svc_name)
            if target is None:
                raise RenderError(
                    f"service {name!r}: ${{{ref}}} references unknown service "
                    f"{svc_name!r} (known services: {sorted(scope.facts)})"
                )
            if field_name not in _FACT_FIELDS:
                raise RenderError(
                    f"service {name!r}: ${{{ref}}} asks for {field_name!r}, which is not an "
                    f"exposed fact (exposed: {list(_FACT_FIELDS)})"
                )
            if field_name not in target:
                raise RenderError(
                    f"service {name!r}: ${{{ref}}} asks for {field_name} but service "
                    f"{svc_name!r} has no {field_name} (e.g. a unix-socket service has no PORT)"
                )
            return target[field_name]

        if ref in scope.self_env:
            return scope.self_env[ref]
        raise RenderError(
            f"service {name!r}: unresolved ${{{ref}}} in value {value!r} "
            f"(available: {sorted(scope.self_env)})"
        )

    return _VAR_RE.sub(replace, value)
