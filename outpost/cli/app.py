"""Outpost CLI — the operator interface (Phase 7).

Every command maps one-to-one to a ``rfc.md`` §19 command. Each is a thin Typer
adapter that parses CLI args, calls the engine (or a sysdeps wrapper directly),
and maps the outcome to an exit code and text/JSON output.

``engine/`` knows nothing about Typer; CLI knows nothing about subprocess details
(RealRunner is the only runner here). Integration coverage for apply/update lives
in ``tests/integration/``; CliRunner smoke tests here verify exit codes and
JSON shapes without invoking the real pipeline.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from outpost import __version__
from outpost import config as outpost_config
from outpost.engine.apply import apply as _apply
from outpost.engine.update import update as _update
from outpost.paths import RuntimePaths
from outpost.state.store import StateStore
from outpost.sysdeps import systemctl
from outpost.sysdeps.journalctl import tail as _tail_logs
from outpost.sysdeps.run import RealRunner, SubprocessError

app = typer.Typer(
    name="outpost",
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
        envvar="OUTPOST_CONFIG",
        help="Path to outpost.yaml (default: ~/.config/outpost/outpost.yaml).",
    ),
]

ServiceArg = Annotated[str, typer.Argument(help="Service name.")]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"outpost {__version__}")
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
    """Outpost — a Linux micro-platform control plane."""


# ===========================================================================
# Config loader helper
# ===========================================================================


def _load_config(path: Path | None) -> outpost_config.OutpostConfig:
    """Load and validate the config, or exit(2) on failure."""
    try:
        return outpost_config.load(path)
    except outpost_config.ConfigError as exc:
        typer.echo(f"invalid config: {exc}", err=True)
        for line in exc.errors:
            typer.echo(f"  - {line}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_CONFIG) from exc


# ===========================================================================
# validate — already exists, add --json support
# ===========================================================================


@app.command()
def validate(
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Parse and validate the config with no system mutation."""
    try:
        outpost_config.load(config)
    except outpost_config.ConfigError as exc:
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
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Full reconciliation: materialize sources, generate configs, swap, gate, commit."""
    try:
        result = _apply(
            runner=_runner,
            config_path=config,
            store=StateStore(RuntimePaths().state),
        )
    except outpost_config.ConfigError as exc:
        _config_error(exc)
    if result.ok:
        if result.no_op:
            typer.echo("no-op: services already up-to-date")
        else:
            typer.echo(result.message)
        raise typer.Exit(code=_EXIT_OK)
    typer.echo(f"apply failed: {result.message}", err=True)
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
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
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
    except outpost_config.ConfigError as exc:
        _config_error(exc)
    if result.ok:
        if result.no_op:
            typer.echo(f"{service} already at latest commit")
        else:
            typer.echo(f"{service} updated and applied")
        raise typer.Exit(code=_EXIT_OK)
    typer.echo(f"update failed: {result.message}", err=True)
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
# up / down (stack lifecycle)
# ===========================================================================


@app.command()
def up(
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Apply + start every service (bring a stopped stack back up)."""
    cfg = _load_config(config)
    for name in cfg.services:
        systemctl.start(_runner, f"{name}.service")
    typer.echo("all services started")


@app.command()
def down(
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
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
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Unified view: service states, routes, exposure."""
    cfg = _load_config(config)
    services = []
    for name in cfg.services:
        state = _unit_state(name)
        svc = cfg.services[name]
        services.append({
            "name": name, "unit": state, "listen": svc.listen or "(allocated)",
            "sha": svc.source.sha, "ref": svc.source.ref or "(default)",
            "health": svc.health is not None,
        })
    routes = [
        {"host": r.host or "(catch-all)", "paths": list(r.paths)}
        for r in cfg.routes
    ]
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
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
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
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
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
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
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


def _config_error(exc: outpost_config.ConfigError) -> None:
    typer.echo(f"invalid config: {exc}", err=True)
    for line in exc.errors:
        typer.echo(f"  - {line}", err=True)
    raise typer.Exit(code=_EXIT_INVALID_CONFIG) from exc


def _run_syscmd(label: str, fn: Callable[[], None]) -> None:
    """Wrap a simple systemctl command, catching and pretty-printing errors."""
    try:
        fn()
    except (SubprocessError, OSError) as exc:
        typer.echo(f"{label} failed: {exc}", err=True)
        raise typer.Exit(code=_EXIT_OPERATIONAL) from exc


def _print_service_status(s: dict) -> None:
    typer.echo(f"{s['name']:20s} {s['unit']:12s} listen={s['listen']}")
