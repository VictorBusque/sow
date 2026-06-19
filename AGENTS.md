# AGENTS.md

Guidance for coding agents and contributors working in this repository.

## What sow is

sow is a **deterministic, single-node Linux micro-platform control plane.** It takes one declarative YAML file and turns it into running `systemd --user` services behind a user-level NGINX reverse proxy, exposed publicly through Cloudflare Tunnel. It also ships a stdio MCP server so coding agents can operate the platform through typed tools instead of raw shell access.

It is a **deployment engine, not an orchestrator**: one machine, one config, one execution loop. No containers, no schedulers, no root daemons.

## Current state of this repo

**Implementation in progress.** The v1 codebase is actively built under `sow/` and `tests/`. The design documents under `docs/v1/` remain the source of truth for intended behavior; code must conform to them. 193 unit/integration tests pass, ruff reports clean, and `ty check` passes with zero diagnostics.

## The single loop

> A YAML file defines git-sourced services and host/path routes → sow renders systemd user units and NGINX config → services run behind a Cloudflare Tunnel.

Everything in v1 is derived from this loop. The four primitives are **service**, **route**, **apply**, and **update**.

## Where the design lives

Read these before writing or reviewing code:

- `docs/v1/prd.md` — product requirements: primitives, apply pipeline, listener/port model, health, source/update semantics, CLI + MCP surface.
- `docs/v1/rfc.md` — technical spec: install/init model, directory layout, execution model, state model, security posture. **The canonical v1 CLI command surface is in `rfc.md` §19.**
- `docs/v1/stack.md` — language, libraries, codebase layout, and engineering conventions.

Reference docs (derived from the three above; they elaborate, not override):

- `docs/v1/config-schema.md` — the canonical `sow.yaml` field schema: every key, type, default, and validation rule. This is the contract the Pydantic models implement.
- `docs/v1/cli-reference.md` — full CLI command + MCP tool contracts (flags, args, exit codes, input/output schemas).
- `docs/v1/examples/full-stack.yaml` — an exhaustive, realistic example config exercising every field.
- `docs/v1/implementation-plan.md` — the v1 build roadmap: phased task decomposition, cross-cutting technical decisions, testing strategy, risks. A living plan, not a normative spec.

When the docs disagree, `rfc.md` is the authoritative reference for the exact command surface and file layout, and `prd.md`/`rfc.md` govern the schema; the reference docs are derived, so raise any discrepancy rather than silently picking one.

## Tech stack

- **Language:** Python 3.12+
- **Tooling:** `uv` (env + deps), `ruff` (format + lint), `ty` (strict type checking; Astral, currently in beta), `pytest`
- **Libraries:** Typer (CLI), Pydantic v2 (schema), Jinja2 (config templates), `mcp` SDK (agent interface), native `subprocess` (system calls — no heavy wrappers)

## Codebase layout

```
sow/
├── cli/         # Typer command definitions (human interface)
├── mcp/         # stdio server + tool definitions (agent interface)
├── engine/      # core loop: validate, render, apply, update
├── models/      # Pydantic schemas (Service, Route, ConfigState)
├── templates/   # Jinja2 templates (.service, .conf, cloudflared yaml)
└── sysdeps/     # subprocess wrappers (git, systemctl, nginx)
tests/
├── unit/        # logic + schema validation
├── integration/ # template rendering + state machine
└── mocks/       # mocked sysdeps (no host side-effects)
```

## Engineering rules (non-negotiable)

These come straight from the docs and apply to all system-mutating code.

**Fail-fast.** Catch `subprocess.CalledProcessError`, log the exact stderr, and exit the run loop with a non-zero code. Never attempt partial recovery mid-apply.

**Atomicity.** Render configs to a temp dir, validate (`nginx -t`), then swap via `os.replace()`. State files (`state.json`) and config rewrites (`sow.yaml`) must be written to a `.tmp` file and atomically renamed. v1 applies are all-or-nothing: on invalid config or failed startup health check, revert to the last-known-good backup. NGINX is reloaded only after the startup health check passes, so live traffic never reaches a service that failed its gate. Concurrent-CLI file locking (e.g. `fcntl.flock`) is deferred — v1 assumes a single operator.

**Idempotency.** `apply` compares the spec digest to the stored applied digest and no-ops if they match and services are up. `systemctl`/`git` wrappers must no-op when the desired state is already met.

**Strict typing.** `ty` in strict mode across CLI and MCP schemas. No dynamic type drift in engine logic.

**Pure, immutable models.** Once `sow.yaml` is parsed into a Pydantic model, treat it as immutable; mutations return a new instance to be saved.

## Source-of-truth facts to keep straight

- **Deployed SHA lives in the config** (`services.<name>.source.sha`), not in `state.json`. `state.json` holds only the applied spec digest, allocated ports, and apply timestamps — nothing else (no SQLite in v1).
- **`apply` never advances an existing `sha`; `update` is the only command that advances it.** The sole exception is the one-time seed: on first deploy of a service whose `sha` is empty, `apply` resolves `ref`→sha and writes it once. Thereafter `apply` reconciles to the pinned sha; `update` fetches, resolves `ref`→sha, writes it, then applies.
- **Rootless everywhere.** Services run as `systemd --user` units; no root, no sudoers rule in v1. NGINX is also a user unit listening on `127.0.0.1:41999`; cloudflared points at it.
- **Ports:** declared via `listen:` or allocated from a loopback range; injected into the service as `PORT`, `ADDRESS`, and `DATA_DIR`. Platform-injected vars override operator env; declaring `listen` and also setting `PORT`/`ADDRESS` is a validation error.
- **Health is a startup gate only** — it makes/breaks an apply. No passive/active probes, no live upstream pruning in v1.
- **Secrets:** Compose-style `environment` / `env_file`. No built-in secret store. `status` and `logs` never echo environment contents.

## Paths (XDG-strict)

- Config (user-owned, editable): `~/.config/sow/sow.yaml`
- Runtime (sow-owned, do not hand-edit): `~/.local/share/sow/` — `repos/`, `data/`, `generated/{nginx,cloudflared,systemd}/`, `state.json`

## Releases

Versioning is **SCM-derived**: the package version is never edited by hand. It is computed from git tags at build time via `hatch-vcs` (see `[tool.hatch.version]` in `pyproject.toml`). The runtime `sow.__version__` is read back from installed package metadata (`importlib.metadata.version("sow-cli")`).

To cut a release:

1. Make sure the commit you want shipped is on `main` and CI is green.
2. `git tag vX.Y.Z && git push origin vX.Y.Z` — that tag push is the only thing that triggers `publish`.

Rules:

- **The git tag is the single source of truth.** Never hand-edit a version string; the build derives it from the tag, and a CI gate asserts the tag matches the built wheel/sdist version before publishing.
- **PyPI files are immutable.** A tag must never be reused or moved — re-publishing the same version is impossible. A bad release is fixed by shipping a new patch version.
- **No dev/dirty artifacts on tags.** Tag an exact, clean commit; the gate rejects versions with a `.dev` or local (`+`) suffix so an unclean tree can't slip through.
- Tags must be valid PEP 440: `vMAJOR.MINOR.PATCH`, optionally with a pre-release suffix (e.g. `-rc1`).

## Commit rules

Every commit must pass these checks before push:

```bash
uv sync                 # install deps (do first on fresh clone)
uvx ruff check .        # lint — zero warnings
uvx ruff format .       # format — sync before commit
uvx ty check .          # type check — zero diagnostics
uv run pytest           # tests — all pass
```

Pre-commit hooks are configured in `.pre-commit-config.yaml` (ruff fmt, ruff lint, ty, trailing-whitespace, end-of-file-fixer, YAML/TOML lint). Install with:

```bash
uv run pre-commit install
```

The hooks enforce formatting, linting, and type-checking automatically. They do not run tests — run `uv run pytest` yourself before pushing.

## Conventions for editing this repo

- Keep docs and README internally consistent; the README's CLI table must match `rfc.md` §19.
- Follow `stack.md` for code conventions.
- If a doc says X and another says Y, flag it rather than guessing.
