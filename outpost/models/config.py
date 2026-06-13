"""Pydantic v2 models for ``outpost.yaml``.

Every model is ``frozen=True``: parsed config is immutable and any mutation
returns a new instance (implementation-plan.md "Pure, immutable models"). The
validators here implement the field-level rules in ``config-schema.md`` §"Validation
rules". Cross-model rules (port collisions, duplicate hosts, ``to`` references,
``exposure.hosts`` ⊆ routed hosts) are enforced on :class:`OutpostConfig` after
the whole tree is parsed.

Config *loading* (YAML → model) and *digest* computation live in
``outpost/config.py``; this module stays pure schema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator
from pydantic_core import PydanticCustomError

from outpost.constants import DEFAULT_PORT_RANGE, NGINX_PORT

# systemd ``Restart=`` values, per systemd.service(5).
_RESTART_VALUES: frozenset[str] = frozenset(
    {"no", "on-success", "on-failure", "always", "on-abnormal", "on-abort", "on-watchdog"}
)

# Full SHA-1 (40 hex) or abbreviated (>=7 hex), either case. ``sha`` may be
# empty before first deploy (seeded once by ``apply``). Outpost never verifies
# it as a real git object — git does that at checkout — so we only enforce the
# shape and normalise to lowercase.
_SHA_RE: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{7,40}$")

# A declared ``listen`` of the form ``host:port``. Unix socket paths contain
# ``/`` or a ``.sock`` suffix, so they never match this and are recognised
# separately by ``Service.is_unix_listen``.
_HOST_PORT_RE: re.Pattern[str] = re.compile(r"^(?P<host>[^:/]+|\[[0-9a-fA-F:]+\]):(?P<port>\d+)$")


def _validate_sha(value: str) -> str:
    """Empty is allowed (pre-seed); otherwise >=7 hex chars (SHA-1 or prefix)."""
    if value == "":
        return value
    if not _SHA_RE.fullmatch(value):
        raise PydanticCustomError(
            "bad_sha",
            "sha must be empty (pre-seed) or 7-40 hex chars; got {value!r}",
            {"value": value},
        )
    return value.lower()


Sha = Annotated[str, AfterValidator(_validate_sha)]


def _validate_port_range(value: str) -> tuple[int, int]:
    """Parse ``"lo-hi"`` into an inclusive ``(lo, hi)`` tuple.

    ``NGINX_PORT`` and declared ``listen`` ports are excluded by the *allocator*,
    not here — this only constrains the allocation band's endpoints.
    """
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value)
    if m is None:
        raise PydanticCustomError(
            "bad_port_range",
            "port_range must be 'lo-hi' (two integers); got {value!r}",
            {"value": value},
        )
    lo, hi = int(m.group(1)), int(m.group(2))
    if not (0 < lo <= hi <= 65535):
        raise PydanticCustomError(
            "bad_port_range",
            "port_range endpoints must satisfy 0 < lo <= hi <= 65535; got {lo}-{hi}",
            {"lo": lo, "hi": hi},
        )
    return (lo, hi)


# Exposed to callers as the canonical ``"lo-hi"`` string (matching the config
# schema), but bounds-validated.
PortRange = Annotated[
    str, AfterValidator(lambda v: f"{_validate_port_range(v)[0]}-{_validate_port_range(v)[1]}")
]


class Source(BaseModel):
    """A git source for a service. The only source kind in v1 is git."""

    git: str = Field(..., description="Remote URL, HTTPS or SSH.")
    ref: str | None = Field(
        default=None,
        description="Branch/tag tracked by `update` (the update target). "
        "Omit to track the remote default branch.",
    )
    sha: Sha = Field(default="", description="Exact deployed commit; empty before first deploy.")
    path: str = Field(default="", description="Subdirectory within the repo for monorepos.")

    model_config = {"frozen": True}

    @property
    def needs_seed(self) -> bool:
        """True iff ``apply`` must resolve ``ref``→sha on first deploy."""
        return self.sha == ""


class HttpHealth(BaseModel):
    """``health.http`` — an HTTP GET path to probe on the local listener."""

    path: str = Field(default="/", description="Path to GET; 2xx/3xx passes.")

    model_config = {"frozen": True}


class Health(BaseModel):
    """A startup health gate. Exactly one of ``http`` / ``tcp`` may be set."""

    http: HttpHealth | None = Field(default=None, description="HTTP GET check.")
    tcp: bool = Field(default=False, description="TCP connect check.")
    timeout: int = Field(default=30, ge=1, description="Seconds apply waits before failure.")

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _exactly_one_of(self) -> Self:
        if self.http is not None and self.tcp:
            raise PydanticCustomError(
                "health_exactly_one",
                "health: exactly one of http/tcp may be set, not both",
            )
        if self.http is None and not self.tcp:
            raise PydanticCustomError(
                "health_exactly_one",
                "health: exactly one of http/tcp must be set",
            )
        return self

    @property
    def is_http(self) -> bool:
        return self.http is not None


class Environment(BaseModel):
    """A service's inline environment, normalised from map or list form.

    Accepts either ``KEY: value`` (a YAML mapping) or ``KEY=value`` (a list of
    strings). Both are stored as an ordered mapping; ``${VAR}`` interpolation is
    resolved later at render time, not here.
    """

    vars: Mapping[str, str] = Field(
        default_factory=dict,
        description="Ordered environment variables, map-form after normalisation.",
    )

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _coerce_map_or_list(cls, data: Any) -> Any:
        """Accept ``{KEY: value}`` or ``["KEY=value", ...]`` and store as a map.

        A bare mapping (the YAML ``environment: {KEY: value}`` form) is wrapped
        into ``{"vars": ...}``; a list (``["KEY=value"]``) is parsed pair by pair.
        """
        if isinstance(data, cls):
            return data
        if isinstance(data, list):
            coerced: dict[str, str] = {}
            for entry in data:
                if not isinstance(entry, str) or "=" not in entry:
                    raise PydanticCustomError(
                        "bad_env_entry",
                        "environment list entries must be 'KEY=value'; got {value!r}",
                        {"value": entry},
                    )
                key, _, value = entry.partition("=")
                coerced[key] = value
            return {"vars": coerced}
        if isinstance(data, Mapping):
            # Bare ``{KEY: value}`` form -> wrap so it binds to the ``vars`` field.
            return {"vars": dict(data)}
        return data

    def items(self) -> list[tuple[str, str]]:
        return list(self.vars.items())


class Service(BaseModel):
    """One git repo run as one process behind one bind address."""

    source: Source
    command: str
    args: list[str] = Field(default_factory=list, description="Extra args appended to command.")
    build: str = Field(default="", description="Shell command run after checkout, before start.")
    listen: str = Field(
        default="", description="host:port or a unix socket path; empty => allocated."
    )
    restart: str = Field(default="on-failure", description="Maps to systemd Restart=.")
    environment: Environment = Field(
        default_factory=Environment, description="Inline environment (map or list)."
    )
    env_file: list[str] = Field(
        default_factory=list, description="Paths to env files loaded verbatim."
    )
    health: Health | None = Field(default=None, description="Startup health gate.")

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _coerce_env_file(cls, data: Any) -> Any:
        """Accept ``env_file: <str>`` or ``env_file: [<str>, ...]``."""
        if isinstance(data, Mapping) and "env_file" in data:
            ef = data["env_file"]
            if isinstance(ef, str):
                data = {**dict(data), "env_file": [ef]}
        return data

    @model_validator(mode="after")
    def _restart_is_known(self) -> Self:
        if self.restart not in _RESTART_VALUES:
            raise PydanticCustomError(
                "bad_restart",
                "restart must be one of {values}; got {value!r}",
                {"values": sorted(_RESTART_VALUES), "value": self.restart},
            )
        return self

    @model_validator(mode="after")
    def _listen_or_port_not_in_env(self) -> Self:
        """Declaring ``listen`` AND setting PORT/ADDRESS in environment is an error."""
        if self.listen:
            clashing = set(self.environment.vars) & {"PORT", "ADDRESS"}
            if clashing:
                raise PydanticCustomError(
                    "listen_env_clash",
                    "listen is set, so PORT/ADDRESS must not appear in environment "
                    "(clashed on {clash})",
                    {"clash": sorted(clashing)},
                )
        return self

    # ---- listen classification (used by the allocator + templates) -------

    @property
    def has_listen(self) -> bool:
        return self.listen != ""

    @property
    def is_unix_listen(self) -> bool:
        """True iff ``listen`` is a unix socket path rather than host:port."""
        if not self.listen:
            return False
        if "/" in self.listen:
            return True
        return self.listen.endswith(".sock") and ":" not in self.listen

    def parsed_listen_port(self) -> int | None:
        """The TCP port if ``listen`` is ``host:port``; ``None`` otherwise."""
        if not self.has_listen or self.is_unix_listen:
            return None
        m = _HOST_PORT_RE.match(self.listen)
        if m is None:
            raise PydanticCustomError(
                "bad_listen",
                "listen must be 'host:port' or a unix socket path; got {value!r}",
                {"value": self.listen},
            )
        return int(m.group("port"))


class PathTarget(BaseModel):
    """The target of one path prefix: ``{ to: <service> }``."""

    to: str = Field(..., description="Service name this prefix routes to.")

    model_config = {"frozen": True}


class Route(BaseModel):
    """A virtual-host: a ``host`` (literal or wildcard) + path-prefix map."""

    host: str = Field(default="", description="Literal or wildcard (*.x). Empty = catch-all.")
    paths: Mapping[str, PathTarget] = Field(
        ..., description="Path prefix -> { to: service }. Longest-prefix wins."
    )

    model_config = {"frozen": True}


class CloudflareExposure(BaseModel):
    """Cloudflare Tunnel exposure."""

    credentials_file: str = Field(..., description="Path to cloudflared credentials JSON.")
    hosts: list[str] = Field(
        ..., min_length=1, description="Hosts exposed publicly via the tunnel."
    )

    model_config = {"frozen": True}


class Exposure(BaseModel):
    """Top-level exposure block. Cloudflare-only in v1."""

    cloudflare: CloudflareExposure

    model_config = {"frozen": True}


class OutpostConfig(BaseModel):
    """The root config model: the whole ``outpost.yaml`` tree."""

    version: int
    port_range: PortRange = Field(
        default=DEFAULT_PORT_RANGE, description="Allocation band 'lo-hi'."
    )
    services: Mapping[str, Service]
    routes: list[Route] = Field(default_factory=list)
    exposure: Exposure | None = None

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _check_topology(self) -> Self:
        if self.version != 1:
            raise PydanticCustomError(
                "bad_version", "version must be 1; got {value}", {"value": self.version}
            )
        if not self.services:
            raise PydanticCustomError("empty_services", "services must be non-empty")

        # listen collisions: NGINX_PORT, duplicates, shared unix sockets.
        seen_tcp: dict[int, str] = {}
        seen_unix: dict[str, str] = {}
        for name, svc in self.services.items():
            if not svc.has_listen:
                continue
            if svc.is_unix_listen:
                if svc.listen in seen_unix:
                    raise PydanticCustomError(
                        "listen_collision",
                        "service {name}: listen socket {sock!r} already claimed by {other!r}",
                        {"name": name, "sock": svc.listen, "other": seen_unix[svc.listen]},
                    )
                seen_unix[svc.listen] = name
            else:
                port = svc.parsed_listen_port()
                assert port is not None  # malformed shapes raise in parsed_listen_port()
                if port == NGINX_PORT:
                    raise PydanticCustomError(
                        "listen_collision",
                        "service {name}: listen port {port} is reserved for NGINX",
                        {"name": name, "port": port},
                    )
                if port in seen_tcp:
                    raise PydanticCustomError(
                        "listen_collision",
                        "service {name}: listen port {port} already claimed by {other!r}",
                        {"name": name, "port": port, "other": seen_tcp[port]},
                    )
                seen_tcp[port] = name

        # routes: duplicate hosts (catch-all sentinel ""), and every
        # paths.*.to must reference a defined service.
        seen_hosts: set[str] = set()
        for idx, route in enumerate(self.routes):
            if route.host in seen_hosts:
                raise PydanticCustomError(
                    "duplicate_host",
                    "routes[{idx}]: host {host!r} is duplicated",
                    {"idx": idx, "host": route.host or "<catch-all>"},
                )
            seen_hosts.add(route.host)
            for prefix, target in route.paths.items():
                if target.to not in self.services:
                    raise PydanticCustomError(
                        "unknown_target",
                        "routes[{idx}] path {prefix!r}: to {svc!r} is not a defined service",
                        {"idx": idx, "prefix": prefix, "svc": target.to},
                    )

        # exposure.hosts must each match a routed host (literal or wildcard).
        if self.exposure is not None:
            for host in self.exposure.cloudflare.hosts:
                if not self._host_is_routed(host, seen_hosts):
                    raise PydanticCustomError(
                        "exposure_not_routed",
                        "exposure host {host!r} is not routed (declare it under routes first)",
                        {"host": host},
                    )
        return self

    @staticmethod
    def _host_is_routed(host: str, routed: set[str]) -> bool:
        """True iff ``host`` matches a literal routed host or a routed wildcard.

        A routed ``*.example.com`` covers ``a.example.com`` and ``a.b.example.com``
        but not bare ``example.com``.
        """
        if host in routed:
            return True
        for pattern in routed:
            if pattern.startswith("*."):
                suffix = pattern[1:]  # ".example.com"
                if host.endswith(suffix) and len(host) > len(suffix):
                    return True
        return False

    @property
    def service_names(self) -> list[str]:
        return list(self.services)
