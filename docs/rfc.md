# RFC: Outpost — Linux Micro-Platform Control Plane (v1)

## 1. Summary

Outpost is a lightweight control plane for running git-sourced services on a single systemd Linux machine. It provides a CLI and MCP interface that turns a declarative YAML file into running systemd user services exposed through a managed NGINX reverse proxy and Cloudflare Tunnel.

Outpost is not a scheduler, container platform, or proxy system. It is a deterministic deployment engine that owns the lifecycle of small services on a single host.

v1 is intentionally minimal: one machine, one configuration, one execution loop.

---

## 2. Core Principle

Outpost implements a single loop:

> A YAML file defines services and routes → Outpost materializes systemd units and NGINX config → services run behind a tunnel.

Everything else is derived from this loop.

---

## 3. System Overview

Outpost consists of:

* A CLI binary (`outpost`)
* A system daemon (optional lightweight coordinator, but mostly CLI-driven in v1)
* A managed runtime directory (`~/.outpost`)
* systemd user services
* NGINX user instance
* Cloudflare Tunnel (cloudflared)

---

## 4. Installation Model

### 4.1 install.sh

Installation is performed via:

```bash
curl -fsSL <repo>/install.sh | sh
```

The installer is responsible for:

* Installing the `outpost` CLI binary into `/usr/local/bin`
* Ensuring required dependencies exist:

  * git
  * systemd (user mode)
  * nginx (user instance capable)
  * cloudflared
* Installing missing dependencies where possible (or guiding installation if not)
* Creating `~/.outpost` directory structure
* Running `outpost init`

### Key design rule

> `install.sh` makes the system runnable. `outpost init` makes it operational.

---

## 5. Initialization (`outpost init`)

`init` is an opinionated bootstrap that ensures the system is fully functional.

It performs:

### 5.1 Environment validation

* git installed and authenticated (SSH or HTTPS working)
* systemd user services enabled (`enable-linger`)
* nginx available and runnable as user service
* cloudflared installed and authenticated

### 5.2 Bootstrap configuration

If missing, `init`:

* creates `~/.outpost/outpost.yaml`
* creates default config structure
* prepares nginx include directory
* configures cloudflared base config pointing to local NGINX

### 5.3 Service activation

* starts or enables:

  * user nginx instance
  * cloudflared tunnel
* ensures system is reachable end-to-end after init

### 5.4 MCP setup (optional guided step)

* offers to install MCP stdio entry command
* prints config snippet for Claude Code / agent integration

---

## 6. Runtime Directory Layout

Outpost owns a single global runtime directory:

```
~/.outpost/
  configs/
    outpost.yaml
    nginx/
    cloudflared/
  repos/
    <service-name>/
  docs/
    *.md   (agent-readable system documentation)
  state.json
```

### Rules

* Fully managed by Outpost
* Users should not manually edit contents
* CLI and MCP are the only mutation interfaces
* Outpost may overwrite anything inside this directory

---

## 7. System Model

Outpost defines two primitives:

### 7.1 Service

A service is:

* a git repository
* a commit SHA
* a build step (optional)
* a command
* a port (explicit or assigned)
* environment variables

Each service maps to exactly one systemd user unit.

---

### 7.2 Route

A route maps:

* host + path → service

Routes compile into NGINX configuration.

---

## 8. Configuration Model

Single file:

```
~/.outpost/configs/outpost.yaml
```

This file is:

* source of truth for system state
* mutated by CLI (e.g. `update` updates `source.sha`)
* optionally version-controlled by user, but not required

Outpost CLI is the authoritative writer.

---

## 9. Execution Model

### 9.1 apply

`outpost apply` performs a full reconciliation:

1. Parse YAML
2. Validate schema
3. For each service:

   * clone repo if missing
   * checkout pinned `sha`
   * run build step (if defined)
4. Generate systemd user units
5. Generate NGINX config
6. Validate NGINX config
7. Atomically replace runtime config
8. Reload systemd user daemon
9. Restart affected services
10. Reload NGINX
11. Start cloudflared if needed
12. Run startup health check (if defined)

### Failure model

* If config invalid → rollback to last known good config
* If service fails startup health → rollback system state
* Otherwise system is committed

---

### 9.2 update

`outpost update <service>`:

1. fetch latest commit from `source.ref`
2. update `source.sha` in config
3. run `apply`

If any step fails:

* system remains unchanged

---

## 10. Exposure Model

### Cloudflare Tunnel only

Outpost assumes a single exposure mechanism:

* cloudflared connects to local NGINX
* NGINX handles routing

Flow:

```
Internet → Cloudflare Tunnel → NGINX (localhost) → Service
```

Outpost only generates configuration; it does not manage TLS, certificates, or routing logic inside Cloudflare.

---

## 11. NGINX Model

* single user-level nginx instance
* listens on fixed local port (e.g. 41999)
* includes generated config directory
* purely reverse proxy

No advanced features in v1:

* no rate limiting
* no auth middleware
* no header manipulation
* no upstream balancing logic

---

## 12. Systemd Model

* systemd **user units only**
* one unit per service
* managed via CLI
* restart policies handled via systemd

Outpost does not implement its own process supervisor.

---

## 13. MCP Interface

Outpost exposes a **stdio MCP server**.

### Usage model

* CLI starts MCP server on demand
* Agents connect via stdio transport
* Used by coding agents (e.g. Claude Code)

### MCP tools

* list_services
* get_service_status
* start_service
* stop_service
* restart_service
* update_service
* apply_config
* validate_config
* show_routes
* show_exposure
* tail_logs

MCP is a thin wrapper over CLI internals.

---

## 14. State Model

Outpost maintains minimal state in:

```
~/.outpost/state.json
```

Stores:

* last applied config hash
* per-service deployed SHA
* timestamps of last successful apply

No history, no analytics, no event store.

---

## 15. CLI Model

### Required commands

* `outpost init`
* `outpost apply`
* `outpost update <service>`
* `outpost status`
* `outpost logs`
* `outpost ps`
* `outpost routes`
* `outpost exposure`
* `outpost start|stop|restart <service>`
* `outpost up`
* `outpost down`

---

## 16. Lifecycle Semantics

### up

* full system bring-up
* equivalent to apply + start everything

### down

* stop all services
* preserve config and state

---

## 17. Security Model

Security is structural, not policy-driven:

* services only bind localhost
* NGINX is single ingress point
* only exposed hosts are reachable via tunnel
* no secrets management layer in v1

---

## 18. Non-Goals (v1)

Explicitly excluded:

* replicas or clustering
* load balancing or upstream pools
* middleware/policy system
* multi-provider tunnels
* rollback system
* containers
* cross-host orchestration
* advanced health routing or runtime traffic control

---

## 19. Success Criteria

Outpost v1 is successful if a user can:

1. install via curl script
2. run `outpost init`
3. define a YAML file with services + routes
4. deploy git-sourced services
5. expose them via Cloudflare Tunnel
6. update a service via git commit advancement
7. observe logs and status via CLI or MCP

without ever writing systemd or NGINX config manually.

---

## 20. Final System Definition

Outpost v1 is:

> a deterministic deployment engine that turns a YAML file into git-sourced systemd services behind a user-level NGINX, exposed via Cloudflare Tunnel, operated through CLI and MCP.
