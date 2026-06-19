# sow 🌱

[![CI](https://github.com/VictorBusque/sow/actions/workflows/ci.yml/badge.svg)](https://github.com/VictorBusque/sow/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with ty](https://img.shields.io/badge/checked%20with-ty-000?logo=python)](https://github.com/astral-sh/ty)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

**A deterministic, single-node Linux micro-platform control plane.**

sow is a lightweight CLI and MCP (Model Context Protocol) server that turns a declarative YAML file into running `systemd` user services exposed through a managed NGINX reverse proxy and Cloudflare Tunnel.

It is designed for one machine, one configuration, and one execution loop. No schedulers, no container runtimes, no root daemons. Just your git-sourced Python/Go/Node apps running fast and lean on a VPS, Raspberry Pi, or homelab.

---

## 🎯 Why sow?

There is a massive gap between raw self-hosting primitives and heavy orchestration. Kubernetes and Nomad are overkill for a single node. Coolify and Dokku rely on Docker, which adds unnecessary overhead when your app is just a high-performance ASGI Python app or a compiled Go binary.

**sow is a deployment engine, not an orchestrator.** You define your stack in one file, and sow safely materializes the `systemd` units, renders the NGINX configs, checks out the exact git SHAs, and ensures everything runs as an unprivileged user.

### Features

* **Rootless by Default:** Services run as `systemd --user` units. NGINX binds to a high local port. No `sudo` required after initial host setup.
* **Git-Driven Deployments:** Every service is tied to a git repository and a specific commit SHA.
* **Built-in Ingress & Exposure:** Automatically configures a local NGINX proxy and routes public traffic via Cloudflare Tunnel.
* **Agent-Ready (MCP):** Ships with a native Model Context Protocol server over `stdio`, allowing coding agents (like Claude Code) to inspect logs, update services, and debug infrastructure safely.
* **Fast & Safe:** Written in Python, typed strictly, and managed via Astral's `uv`. Applies are atomic—if a generated NGINX config is invalid or a service fails its startup health check, sow reverts the system to the last known-good state.

---

## 🚀 Getting Started

### 1. Installation

Install the sow CLI (requires `uv`):

```bash
curl -fsSL https://raw.githubusercontent.com/VictorBusque/sow/main/install.sh | sh
```

*Note: sow runs entirely in user-space. Ensure `git`, `nginx`, and `cloudflared` are pre-installed.*

### 2. Initialization

Bootstrap the platform on your host. This ensures systemd user services are lingering and NGINX/Cloudflare are properly configured to talk to each other:

```bash
sow init

```

### 3. Define Your Stack

Edit your declarative config at `~/.config/sow/sow.yaml`:

```yaml
version: 1

services:
  api:
    source:
      git: https://github.com/me/my-fastapi.git
      ref: main
    build: pip install -r requirements.txt
    command: python -m uvicorn main:app
    args: ["--port", "${PORT}"]
    environment:
      LOG_LEVEL: info
      DATABASE_URL: sqlite:///${DATA_DIR}/app.db
    health:
      http: { path: /healthz }

routes:
  - host: api.example.com
    paths:
      /: { to: api }

exposure:
  cloudflare:
    credentials_file: ~/.cloudflared/app.json
    hosts: [api.example.com]

```

### 4. Deploy

Reconcile the configuration and bring the system up:

```bash
sow up

```

To update the service to the latest commit on `main` later:

```bash
sow update api

```

---

## 🛠️ CLI Reference

sow's CLI is the authoritative interface for your infrastructure.

| Command | Description |
| --- | --- |
| `sow init` | Bootstraps host dependencies and the user-level daemon setup. |
| `sow validate` | Checks the YAML against the schema without modifying the system. |
| `sow apply` | Reconciles the YAML to system reality (clone, build, render, reload, health check). |
| `sow update <svc> [--ref <ref>]` | Fetches the latest commit on `source.ref`, writes the SHA, and applies. |
| `sow start <svc>` | Starts one systemd user service. |
| `sow stop <svc>` | Stops one systemd user service. |
| `sow restart <svc>` | Restarts one systemd user service. |
| `sow status` | Displays unified service health, route, and exposure status. |
| `sow logs <svc> [--lines N]` | Tails journald logs for a service (bounded tail; default 200). |
| `sow ps` | Lists running services and their systemd unit states. |
| `sow routes` | Lists configured host/path routes. |
| `sow exposure` | Lists hosts exposed through Cloudflare Tunnel. |
| `sow up` | Applies config and starts all services (the one-shot "make everything run"). |
| `sow down` | Stops all services; leaves config, generated files, clones, and data in place. |

---

## 📚 Documentation

| Doc | What it covers |
| --- | --- |
| [docs/v1/prd.md](https://github.com/VictorBusque/sow/blob/main/docs/v1/prd.md) | Product requirements: the four primitives, apply pipeline, ports, health, source/update semantics. |
| [docs/v1/rfc.md](https://github.com/VictorBusque/sow/blob/main/docs/v1/rfc.md) | Technical spec: install/init, directory layout, execution & state model, security. |
| [docs/v1/stack.md](https://github.com/VictorBusque/sow/blob/main/docs/v1/stack.md) | Language, libraries, codebase layout, engineering conventions. |
| [docs/v1/config-schema.md](https://github.com/VictorBusque/sow/blob/main/docs/v1/config-schema.md) | Canonical `sow.yaml` field schema (types, defaults, validation rules). |
| [docs/v1/cli-reference.md](https://github.com/VictorBusque/sow/blob/main/docs/v1/cli-reference.md) | Full CLI + MCP tool contracts (flags, exit codes, I/O schemas). |
| [docs/v1/examples/full-stack.yaml](https://github.com/VictorBusque/sow/blob/main/docs/v1/examples/full-stack.yaml) | Exhaustive, realistic example config. |
| [docs/v1/implementation-plan.md](https://github.com/VictorBusque/sow/blob/main/docs/v1/implementation-plan.md) | v1 build roadmap: phased tasks, technical decisions, testing, risks. |

---

## 🤝 Contributing

We welcome contributions! sow is built on the [Astral stack](https://astral.sh/) to guarantee high performance, rigorous type safety, and an excellent developer experience.

### Local Development Setup

1. **Install `uv`** (our package manager): `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. **Clone & Sync:**

```bash
git clone https://github.com/VictorBusque/sow.git
cd sow
uv sync
```

### Engineering Standards

Before submitting a Pull Request, please ensure your code adheres to the project's strict quality gates:

* **Formatting & Linting:** Run `uvx ruff check .` and `uvx ruff format .`
* **Type Checking:** We use Astral's `ty` (strict). Run `uvx ty .`. `ty` is still in beta, so `mypy --strict` is the documented fallback — run whichever is current. No dynamic type drift is permitted in system-mutating logic.
* **Testing:** Run the test suite with `uv run pytest`. Because sow mutates system state, all unit tests testing the core `engine/` must mock `subprocess.run` (Git, systemctl, NGINX) and file system operations using `unittest.mock` or `pytest` fixtures.

### Architectural Rules

* **XDG Compliance:** Never hardcode paths to `~/`. Always respect `~/.config/sow/` for user intent and `~/.local/share/sow/` for platform state.
* **Fail-Fast:** Do not attempt partial recoveries in the engine loop. Catch `CalledProcessError` and exit cleanly.
* **Atomic Writes:** Any modifications to `state.json` or `sow.yaml` must be written to a `.tmp` file and atomically replaced via `os.replace()`.

---

## 📄 License

sow is released under the [MIT License](LICENSE).
