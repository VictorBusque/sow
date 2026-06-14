"""Outpost CLI — the operator interface.

Walking skeleton surface: ``validate`` and ``render <service>``. The full command
set arrives in Phase 7; both commands here are thin adapters that call the engine
and map exceptions to exit codes (``cli-reference.md``):
``0`` success, ``1`` operational error, ``2`` invalid config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from outpost import __version__
from outpost import config as outpost_config
from outpost.engine import (
    PortAllocationError,
    RenderError,
    allocate_all,
    compute_facts,
    render_unit,
)

app = typer.Typer(
    name="outpost",
    help="Turn a YAML file into running systemd user services behind NGINX.",
    no_args_is_help=True,
)

# Exit codes (cli-reference.md "Exit codes").
_EXIT_OK = 0
_EXIT_OPERATIONAL = 1
_EXIT_INVALID_CONFIG = 2

# Operator-owned config; defaults to the XDG location (config.DEFAULT_CONFIG_PATH).
ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        envvar="OUTPOST_CONFIG",
        help="Path to outpost.yaml (default: ~/.config/outpost/outpost.yaml).",
    ),
]


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


@app.command()
def render(
    service: Annotated[str, typer.Argument(help="The service whose unit to render.")],
    config: ConfigOption = outpost_config.DEFAULT_CONFIG_PATH,  # type: ignore[assignment]
) -> None:
    """Render one service's systemd unit to stdout (no filesystem mutation)."""
    try:
        cfg = outpost_config.load(config)
    except outpost_config.ConfigError as exc:
        typer.echo(f"invalid config: {exc}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_CONFIG) from exc

    if service not in cfg.services:
        typer.echo(f"unknown service: {service!r}", err=True)
        raise typer.Exit(code=_EXIT_INVALID_CONFIG)

    svc = cfg.services[service]
    try:
        ports = allocate_all(cfg)
        # Build the cross-service fact table so `${other.ADDRESS}` (etc.) in
        # this service's env/args resolves — ports drive the addresses.
        facts = compute_facts(cfg, ports)
        unit = render_unit(service, svc, ports.get(service), facts=facts)
    except (PortAllocationError, RenderError) as exc:
        typer.echo(f"render failed: {exc}", err=True)
        raise typer.Exit(code=_EXIT_OPERATIONAL) from exc

    typer.echo(unit)
