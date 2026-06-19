"""sow CLI — the operator interface (Phase 7).

Every command maps one-to-one to a ``rfc.md`` §19 command. Each is a thin Typer
adapter that parses CLI args, calls the engine (or a sysdeps wrapper directly),
and maps the outcome to an exit code and text/JSON output.

``engine/`` knows nothing about Typer; CLI knows nothing about subprocess details
(RealRunner is the only runner here). Integration coverage for apply/update lives
in ``tests/integration/``; CliRunner smoke tests here verify exit codes and
JSON shapes without invoking the real pipeline.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from sow import __version__
from sow import config as sow_config
from sow.config import add_route, add_service, remove_route, remove_service
from sow.engine.apply import apply as _apply
from sow.engine.init import init as _init
from sow.engine.update import update as _update
from sow.paths import RuntimePaths
from sow.state.store import StateStore
from sow.sysdeps import systemctl
from sow.sysdeps.journalctl import tail as _tail_logs
from sow.sysdeps.run import RealRunner, SubprocessError

app = typer.Typer(
    name="sow",
    help="Turn a YAML file into running systemd user services behind NGINX.",
    no_args_is_help=True,
)

_EXIT_OK = 0
_EXIT_OPERATIONAL = 1
_EXIT_INVALID_CONFIG = 2

# Injected by tests; production uses a real host runner.
_runner = RealRunner()

ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        envvar="SOW_CONFIG",
        help="Path to sow.yaml (default: ~/.config/sow/sow.yaml).",
    ),
]

ServiceArg = Annotated[str, typer.Argument(help="Service name.")]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sow {__version__}")
        raise typer.Exit


@app.callback()
def _main(
    version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """sow — a Linux micro-platform control plane."""


# ===========================================================================
# Config loader helper
# ===========================================================================


def _load_config(path: Path | None) -> sow_config.sowConfig:
    """Load and validate the config, or exit(2) on failure."""
    try:
        return sow_config.load(path)
    except sow_config.ConfigError as exc:
        typer.echo(f"invalid config: {exc}", err=True)
        for line in exc.errors:
            typer.echo(f"  - {line}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_CONFIG) from exc


# ===========================================================================
# validate — already exists, add --json support
# ===========================================================================


@app.command()
def validate(
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Parse and validate the config with no system mutation."""
    try:
        sow_config.load(config)
    except sow_config.ConfigError as exc:
        typer.echo(f"invalid config: {exc}", err=True)
        for line in exc.errors:
            typer.echo(f"  - {line}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_CONFIG) from exc
    typer.echo("config is valid")


# ===========================================================================
# apply
# ===========================================================================


@app.command()
def apply(
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Full reconciliation: materialize sources, generate configs, swap, gate, commit."""
    try:
        result = _apply(
            runner=_runner,
            config_path=config,
            store=StateStore(RuntimePaths().state),
        )
    except sow_config.ConfigError as exc:
        _config_error(exc)
    if result.ok:
        if result.no_op:
            typer.echo("no-op: services already up-to-date")
        else:
            typer.echo(result.message)
        raise typer.Exit(code=_EXIT_OK)
    typer.echo(f"apply failed: {result.message}", err=True)
    typer.echo("hint: run `sow logs <service>` or check the error above", err=True)
    raise typer.Exit(code=_EXIT_OPERATIONAL)


# ===========================================================================
# update
# ===========================================================================


@app.command()
def update(
    service: ServiceArg,
    ref: Annotated[
        str | None,
        typer.Option("--ref", help="Override the tracked ref (branch/tag)."),
    ] = None,
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Advance a service's pinned git SHA to its latest commit, then apply."""
    try:
        result = _update(
            service,
            runner=_runner,
            config_path=config,
            store=StateStore(RuntimePaths().state),
            ref=ref,
        )
    except sow_config.ConfigError as exc:
        _config_error(exc)
    if result.ok:
        if result.no_op:
            typer.echo(f"{service} already at latest commit")
        else:
            typer.echo(f"{service} updated and applied")
        raise typer.Exit(code=_EXIT_OK)
    typer.echo(f"update failed: {result.message}", err=True)
    typer.echo("hint: check git credentials and network access", err=True)
    raise typer.Exit(code=_EXIT_OPERATIONAL)


# ===========================================================================
# start / stop / restart
# ===========================================================================


@app.command()
def start(
    service: ServiceArg,
) -> None:
    """Start a service (thin systemctl wrapper)."""
    _run_syscmd(
        f"systemctl --user start {service}",
        lambda: systemctl.start(_runner, f"{service}.service"),
    )


@app.command()
def stop(
    service: ServiceArg,
) -> None:
    """Stop a service (thin systemctl wrapper)."""
    _run_syscmd(
        f"systemctl --user stop {service}",
        lambda: systemctl.stop(_runner, f"{service}.service"),
    )


@app.command()
def restart(
    service: ServiceArg,
) -> None:
    """Restart a service (thin systemctl wrapper)."""
    _run_syscmd(
        f"systemctl --user restart {service}",
        lambda: systemctl.restart(_runner, f"{service}.service"),
    )


# ===========================================================================
# init
# ===========================================================================


@app.command()
def init() -> None:
    """Bootstrap the platform: check deps, create configs, enable units."""
    result = _init(runner=_runner)
    typer.echo(result.message)
    if not result.ok:
        raise typer.Exit(code=_EXIT_OPERATIONAL)


# ===========================================================================
# up / down (stack lifecycle)
# ===========================================================================


@app.command()
def up(
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Apply + start every service (bring a stopped stack back up)."""
    cfg = _load_config(config)
    for name in cfg.services:
        systemctl.start(_runner, f"{name}.service")
    typer.echo("all services started")


@app.command()
def down(
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Stop all services (leaves config, clones, data)."""
    cfg = _load_config(config)
    for name in cfg.services:
        systemctl.stop(_runner, f"{name}.service")
    typer.echo("all services stopped")


# ===========================================================================
# status / ps / routes / exposure (read commands, support --json)
# ===========================================================================


def _unit_state(name: str) -> str:
    """Live unit state, or ``"unknown"`` when systemctl fails."""
    try:
        return systemctl.unit_state(_runner, f"{name}.service")
    except (SubprocessError, OSError):
        return "unknown"


@app.command()
def status(
    json_format: Annotated[bool, typer.Option("--json", help="JSON output.")] = False,
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Unified view: service states, routes, exposure."""
    cfg = _load_config(config)
    services = []
    for name in cfg.services:
        state = _unit_state(name)
        svc = cfg.services[name]
        services.append(
            {
                "name": name,
                "unit": state,
                "listen": svc.listen or "(allocated)",
                "sha": svc.source.sha,
                "ref": svc.source.ref or "(default)",
                "health": svc.health is not None,
            }
        )
    routes = [{"host": r.host or "(catch-all)", "paths": list(r.paths)} for r in cfg.routes]
    exposure: dict | None = None
    if cfg.exposure is not None:
        exposure = {"provider": "cloudflare", "hosts": list(cfg.exposure.cloudflare.hosts)}
    if json_format:
        typer.echo(
            json.dumps(
                {"services": services, "routes": routes, "exposure": exposure},
                indent=2,
            )
        )
        return
    for s in services:
        _print_service_status(s)
    if routes:
        typer.echo("\nroutes:")
        for route in cfg.routes:
            host = route.host or "(catch-all)"
            targets = ", ".join(f"{p}->{t.to}" for p, t in route.paths.items())
            typer.echo(f"  {host}: {targets}")
    if exposure:
        typer.echo(f"\nexposure: cloudflare tunnel -> {', '.join(exposure['hosts'])}")


@app.command()
def ps(
    json_format: Annotated[bool, typer.Option("--json", help="JSON output.")] = False,
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """List services and their systemd unit states."""
    cfg = _load_config(config)
    rows = []
    for name in cfg.services:
        rows.append({"name": name, "unit": _unit_state(name)})
    if json_format:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        typer.echo(f"{r['name']:20s} {r['unit']}")


@app.command()
def routes(
    json_format: Annotated[bool, typer.Option("--json", help="JSON output.")] = False,
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """List configured host/path routes."""
    cfg = _load_config(config)
    if json_format:
        out = [
            {"host": r.host or "(catch-all)", "paths": {p: t.to for p, t in r.paths.items()}}
            for r in cfg.routes
        ]
        typer.echo(json.dumps(out, indent=2))
        return
    for route in cfg.routes:
        host = route.host or "(catch-all)"
        typer.echo(f"  {host}:")
        for prefix, target in route.paths.items():
            typer.echo(f"    {prefix} -> {target.to}")


@app.command()
def exposure(
    json_format: Annotated[bool, typer.Option("--json", help="JSON output.")] = False,
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """List hosts exposed through Cloudflare Tunnel."""
    cfg = _load_config(config)
    if cfg.exposure is None:
        if json_format:
            typer.echo(json.dumps({"provider": None, "hosts": []}))
        else:
            typer.echo("no exposure configured")
        return
    out = {"provider": "cloudflare", "hosts": list(cfg.exposure.cloudflare.hosts)}
    if json_format:
        typer.echo(json.dumps(out, indent=2))
    else:
        typer.echo(f"cloudflare tunnel -> {', '.join(out['hosts'])}")


# ===========================================================================
# logs
# ===========================================================================


@app.command()
def logs(
    service: ServiceArg,
    lines: Annotated[
        int, typer.Option("--lines", "-n", help="Number of lines (default 200).")
    ] = 200,
) -> None:
    """Tail journald logs for a service (bounded, default 200 lines)."""
    try:
        text = _tail_logs(_runner, f"{service}.service", lines=lines)
    except SubprocessError as exc:
        typer.echo(f"error reading logs: {exc.stderr or exc}", err=True)
        raise typer.Exit(code=_EXIT_OPERATIONAL) from exc
    typer.echo(text, nl=False)


# ===========================================================================
# Internal helpers
# ===========================================================================


def _config_error(exc: sow_config.ConfigError) -> None:
    typer.echo(f"invalid config: {exc}", err=True)
    for line in exc.errors:
        typer.echo(f"  - {line}", err=True)
    typer.echo("hint: edit ~/.config/sow/sow.yaml and re-run", err=True)
    raise typer.Exit(code=_EXIT_INVALID_CONFIG) from exc


def _run_syscmd(label: str, fn: Callable[[], None]) -> None:
    """Wrap a simple systemctl command, catching and pretty-printing errors."""
    try:
        fn()
    except (SubprocessError, OSError) as exc:
        typer.echo(f"{label} failed: {exc}", err=True)
        typer.echo("hint: check `systemctl --user status` or `journalctl --user -xe`", err=True)
        raise typer.Exit(code=_EXIT_OPERATIONAL) from exc


def _print_service_status(s: dict) -> None:
    typer.echo(f"{s['name']:20s} {s['unit']:12s} listen={s['listen']}")


# ===========================================================================
# add / rm (config editing)
# ===========================================================================


@app.command()
def add(
    name: str = typer.Argument(help="Service name."),
    git: str = typer.Option(..., "--git", "-g", help="Git remote URL (HTTPS or SSH)."),
    command: str = typer.Option(..., "--command", "-c", help="Command to run."),
    build: str = typer.Option("", "--build", "-b", help="Build command (shell)."),
    listen: str = typer.Option("", "--listen", "-l", help="host:port or unix socket path."),
    ref: str | None = typer.Option(None, "--ref", "-r", help="Git ref (branch/tag)."),
    subpath: str = typer.Option("", "--path", help="Subdirectory within repo."),
    restart: str = typer.Option("on-failure", "--restart", help="Restart policy."),
    env: Annotated[
        list[str] | None,
        typer.Option("--env", "-e", help="Environment var KEY=VALUE (repeatable)."),
    ] = None,
    env_file: Annotated[
        list[str] | None,
        typer.Option("--env-file", "-E", help="Env file path (repeatable)."),
    ] = None,
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Add a service definition to the config file."""
    env_dict = _parse_env_list(env or [])
    try:
        add_service(
            config,
            name,
            git=git,
            command=command,
            build=build,
            listen=listen,
            ref=ref,
            subpath=subpath,
            restart=restart,
            env=env_dict or None,
            env_file=env_file or None,
        )
    except sow_config.ConfigError as exc:
        _config_error(exc)
    typer.echo(f"service {name!r} added")


@app.command()
def rm(
    name: str = typer.Argument(help="Service name to remove."),
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Remove a service (and its route targets) from the config file."""
    try:
        remove_service(config, name)
    except sow_config.ConfigError as exc:
        _config_error(exc)
    typer.echo(f"service {name!r} removed")


@app.command(name="route-add")
def route_add(
    host: str = typer.Argument(help="Host (literal, wildcard, or empty for catch-all)."),
    prefix: str = typer.Argument(help="Path prefix (e.g. / or /api)."),
    to: str = typer.Option(..., "--to", help="Target service name."),
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Add a host/path route to the config file."""
    try:
        add_route(config, host, prefix, to)
    except sow_config.ConfigError as exc:
        _config_error(exc)
    typer.echo(f"route {host!r}{prefix!r} -> {to!r} added")


@app.command(name="route-rm")
def route_rm(
    host: str = typer.Argument(help="Host."),
    prefix: str | None = typer.Argument(None, help="Path prefix (omit to remove the whole host)."),
    config: ConfigOption = sow_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Remove a route (one prefix or the whole host) from the config file."""
    try:
        remove_route(config, host, prefix)
    except sow_config.ConfigError as exc:
        _config_error(exc)
    label = f"{host!r}{prefix!r}" if prefix else f"host {host!r}"
    typer.echo(f"route {label} removed")


def _parse_env_list(env: list[str]) -> dict[str, str]:
    """Parse ['KEY=VALUE', ...] into a dict, failing on bad entries."""
    result: dict[str, str] = {}
    for entry in env:
        if "=" not in entry:
            typer.echo(f"bad --env: {entry!r} (must be KEY=VALUE)", err=True)
            raise typer.Exit(code=_EXIT_INVALID_CONFIG)
        k, _, v = entry.partition("=")
        result[k] = v
    return result


# ===========================================================================
# MCP server (Phase 8)
# ===========================================================================


@app.command(hidden=True)
def mcp_server() -> None:
    """Run the MCP stdio server (used by coding-agent harnesses)."""
    from sow.mcp.server import run_server as _run_mcp

    asyncio.run(_run_mcp())
