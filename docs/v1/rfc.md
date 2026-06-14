# RFC: Outpost — Linux Micro-Platform Control Plane (v1)

## 1. Summary

Outpost is a lightweight control plane for running git-sourced services on a single systemd Linux machine. It provides a CLI and an MCP interface that turns a declarative YAML file into running systemd user services exposed through a managed NGINX reverse proxy and Cloudflare Tunnel.

Outpost is not a scheduler, container platform, or proxy system. It is a deterministic deployment engine that owns the lifecycle of small services on one host.

v1 is intentionally minimal: one machine, one configuration, one execution loop.

---

## 2. Core Principle

Outpost implements a single loop:

> A YAML file defines services and routes → Outpost materializes systemd units and NGINX config → services run behind a tunnel.

Everything else is derived from this loop.

---

## 3. System Overview

Outpost consists of:

- a CLI binary (`outpost`)
- a user-level runtime managed under the current user’s home directory
- systemd user services
- a user-level NGINX instance
- Cloudflare Tunnel (`cloudflared`)

Outpost is host-wide, but user-space: it runs on a single Linux machine under the operator account, without requiring a custom root daemon.

---

## 4. Installation Model

### 4.1 `install.sh`

Installation is performed via:

```bash
curl -fsSL <repo>/install.sh | sh
````

The installer is responsible for:

- installing the `outpost` CLI binary into the PATH
- ensuring required dependencies exist:

  - git
  - systemd user support
  - nginx
  - cloudflared
- running `outpost init`

### Privilege boundary

`install.sh` may use `sudo` only for installing missing system packages, and only if:

- `sudo` is available, and
- the user agrees or the installer is explicitly configured to do so.

If privilege escalation is not available, the installer must fail fast with exact remediation steps rather than silently degrading.

### Key design rule

> `install.sh` makes the host capable. `outpost init` makes the platform operational.

---

## 5. Initialization (`outpost init`)

`outpost init` is an opinionated bootstrap that ensures the platform is ready to use.

It performs:

### 5.1 Environment validation

- verifies git is installed and working
- verifies the user can authenticate to git remotes
- verifies systemd user support is available
- verifies nginx is installed and runnable as a user-level service
- verifies cloudflared is installed and authenticated or can be authenticated

### 5.2 Bootstrap configuration

If missing, `init`:

- creates `~/.config/outpost/outpost.yaml`
- creates the Outpost runtime directory structure under `~/.local/share/outpost/`
- prepares the nginx include directory and runtime config paths
- prepares cloudflared config pointing to local NGINX

### 5.3 Service activation

- starts or enables the user-level NGINX instance
- starts or enables cloudflared
- ensures the platform is operational after init

### 5.4 MCP setup

- offers a stdio MCP command that coding agents can invoke
- prints integration guidance for tools such as Claude Code

---

## 6. Directory Layout

Outpost uses split ownership:

### User-owned config

```text
~/.config/outpost/
  outpost.yaml
```

This file is the editable configuration source of truth. The user owns it and may version-control it if desired.

### Outpost-owned runtime

```text
~/.local/share/outpost/
  repos/
    <service-name>/
  data/
    <service-name>/
  generated/
    nginx/
    cloudflared/
    systemd/
  state.json
```

Rules:

- `repos/`, `data/`, `generated/`, and `state.json` are managed by Outpost
- the user should not edit runtime files directly
- the CLI and MCP are the supported mutation interfaces for Outpost-managed state

---

## 7. System Model

Outpost defines two primitives:

### 7.1 Service

A service is:

- one git repository
- one commit SHA
- one command
- one optional build step
- one port
- environment variables

Each service maps to exactly one systemd user unit.

### 7.2 Route

A route maps:

- host + path → service

Routes compile into NGINX configuration.

---

## 8. Configuration Model

The source of truth is:

```text
~/.config/outpost/outpost.yaml
```

This file is:

- the canonical configuration for the platform
- user-editable
- optionally version-controlled by the user
- mutated by Outpost commands when needed

Outpost CLI is the authoritative writer for deployment-related fields such as `source.sha`. All writes go through temp-file + atomic rename to prevent torn files. v1 assumes a single operator issuing serialized commands. The MCP server serializes tool calls within one stdio connection, but there is no cross-process lock, so concurrent CLI + MCP sessions (or multiple MCP clients) can still race on `outpost.yaml`/`state.json`. An `fcntl` advisory lock is a candidate hardening but is deferred — concurrent edits may lose updates.

---

## 9. Execution Model

### 9.1 `apply`

`outpost apply` performs a full reconciliation:

1. parse YAML
2. validate schema
3. for each service:

   - clone repo if missing
   - if `sha` is empty, resolve `ref`→sha once and write it back (seed)
   - checkout pinned `sha`
   - run the build step, if defined, only when the clone is freshly made or `sha` changed since the last apply (idempotent; unchanged pinned commits are not rebuilt)
4. generate systemd user units
5. generate NGINX config
6. validate NGINX config (`nginx -t`)
7. atomically swap in the new runtime config (keeping last-known-good)
8. reload systemd user daemon
9. start/restart affected services
10. run startup health check against each service's local listener, if defined
11. on success: reload NGINX (activating new routing) and ensure cloudflared is running; on failure: restore last-known-good config and reload NGINX back to it. cloudflared is a `systemd --user` unit, so a start failure surfaces as a `subprocess` error and fails the apply (rollback) just like any other command
12. commit apply (update spec digest + timestamps in `state.json`)

### Failure model

- if config validation fails, rollback to the last known good runtime set
- if startup health fails, rollback to the last known good runtime set
- NGINX is reloaded only after the health check passes, so live traffic never reaches a service that failed its gate
- otherwise the apply is committed

The result is all-or-nothing for v1.

### 9.2 `update`

`outpost update <service>`:

1. fetch the latest commit from `source.ref`
2. write the resulting SHA into `source.sha` in the config
3. run `apply`

If any step fails, the running system remains unchanged.

---

## 10. Exposure Model

### Cloudflare Tunnel only

Outpost assumes one exposure mechanism in v1:

- `cloudflared` connects to local NGINX
- NGINX handles routing

Flow:

```text
Internet → Cloudflare Tunnel → NGINX (localhost) → Service
```

Outpost renders the cloudflared config from the exposure section of the YAML. It does not manage public TLS certificates or expose multiple tunnel providers in v1. cloudflared runs as a platform-managed `systemd --user` unit: `init` enables it and `apply` ensures it is started; supervision is delegated to systemd.

---

## 11. NGINX Model

- single user-level NGINX instance
- listens on a fixed local port, default `127.0.0.1:41999`
- includes generated config from the Outpost runtime directory
- acts only as a reverse proxy and route dispatcher

v1 does not include:

- rate limiting
- auth middleware
- header rewriting
- upstream balancing logic
- reusable policy chains

---

## 12. Systemd Model

- systemd **user units only**
- one unit per service
- managed via CLI
- restart behavior delegated to systemd

Outpost does not implement its own process supervisor.

---

## 13. Health Model

Health in v1 is a **startup check only**.

- if a service defines `health`, `apply` waits for the check to pass before considering the deploy successful
- `apply` polls until the check passes or `health.timeout` (default 30 seconds) elapses; timing out counts as a failure
- if no `health` is defined, `apply` succeeds once systemd reports the unit active
- if the check never passes, `apply` fails and the previous working state is restored

There are no passive or active traffic health checks, no route quarantine state, and no live upstream pruning in v1.

Cloudflared edge registration is **not** part of the health gate. `apply` ensures the cloudflared unit is started; a unit **start** failure is a rollback trigger (subprocess error), but the tunnel's asynchronous registration of a host route with the upstream edge is not observable via `systemctl is-active` and is not a v1 rollback trigger — such failures are typically external (credentials, network, edge state), so rollback cannot remediate them and would only extend the outage. systemd supervises cloudflared; the public route recovers on cloudflared's reconnect timeline. A dead-route *detection* (not rollback) belongs with the deferred passive/active connectivity checks.

---

## 14. State Model

Outpost maintains minimal state in:

```text
~/.local/share/outpost/state.json
```

This file stores only what cannot be reliably derived from config or runtime files:

- the last applied config hash
- allocated port assignments for services that omit `listen`
- timestamps of recent successful applies

It does **not** store the deployed SHA. The deployed SHA lives in `source.sha` inside `outpost.yaml` and is the source of truth for versioned deployment state.

No history store, no analytics, no event database, no SQLite in v1.

---

## 15. Listener and Port Model

Each service has exactly one bind address.

The address is resolved in one of two ways:

- **Declared**: set `listen:` to `host:port` or a unix socket path
- **Allocated**: omit `listen:` and Outpost assigns a stable loopback port from a configured range (default `18000-18999`, set via a top-level `port_range:`). Allocation is first-fit (lowest free port), persisted in `state.json`, and reused unless the service definition changes. The range excludes the NGINX port `41999` and all declared `listen` ports; collisions between a `listen` port and `41999`, another `listen` port, or an allocated port are a validation error. If the range is exhausted, `apply` fails fast.

In both cases, Outpost injects the bind address and persistent data path into the service environment as:

- `PORT`
- `ADDRESS`
- `DATA_DIR`

Platform-injected variables take precedence over user-defined environment values. Declaring `listen` and also setting `PORT` or `ADDRESS` is a validation error.

Allocated ports are stable across restarts and re-applies unless the service definition changes.

---

## 16. Environment and Secrets

Services receive environment variables in Docker Compose style:

- `services.<name>.environment`

  - inline map or list
  - supports `${VAR}` interpolation
  - resolved at generate time from the host environment plus platform-injected values
- `services.<name>.env_file`

  - one or more env files loaded verbatim

**Cross-service references.** Beyond bare `${VAR}` (self-scoped), values may
reference another service's platform facts via `${<name>.<FIELD>}`, where
`FIELD` is `ADDRESS`, `PORT`, or `DATA_DIR`. Only platform facts are exposed
cross-service — operator-defined environment is never reachable across services.
Unknown services, unexposed fields, and fields the target lacks fail fast.

Precedence:

1. platform-injected variables
2. inline `environment`
3. later `env_file` entries
4. earlier `env_file` entries

v1 ships no built-in secret store. The platform passes env values through to the generated unit files without interpreting them.

Safety rules:

- `status` and `logs` never echo environment contents
- generated unit files should reference env files rather than copy secrets into broadly-readable runtime files where avoidable

---

## 17. Source and Updates

Every service is git-sourced.

A `source:` block is required on every service and accepts only git.

Fields:

- `source.git`

  - remote URL, HTTPS or SSH
- `source.ref`

  - branch or tag tracked by `update`
  - omit to track the remote default branch
- `source.sha`

  - exact commit currently deployed
  - written by Outpost
  - may be empty before first deploy
- `source.path`

  - optional subdirectory within the repo for monorepos
- `build`

  - optional command run after checkout and before start

Rules:

- `apply` reconciles to the pinned `sha`; the only time `apply` writes `sha` is the one-time seed of an empty field (resolving `ref`→sha on first deploy)
- `apply` never advances an already-set `sha` within a ref
- `update` is the only command that advances an existing `sha`
- Outpost owns the managed clone under `~/.local/share/outpost/repos/<service>`; clones are keyed by service name, so two services from the same repo (e.g. a monorepo with different `source.path`) get independent clones
- local edits inside the managed clone are overwritten on update

Because the config is also written by `update`, the user interacts primarily through the CLI or MCP rather than hand-editing deployment fields.

Private repositories use the operator’s existing git credentials. Outpost runs git as the operator user and does not provide a credential manager in v1.

The persistent service data directory is separate from the managed clone:

- `~/.local/share/outpost/data/<service>/`

Mutable application state such as databases, uploads, caches, and user-generated files must live under `DATA_DIR`. Updates may replace the repo contents, but they must not touch service data.

---

## 18. Service Lifecycle

Required lifecycle operations:

- start one service
- stop one service
- restart one service
- check status for one service
- start all services
- stop all services
- restart all services
- tail logs for one service

These map to `systemctl --user` and `journalctl --user`.

---

## 19. CLI Model

The CLI is the primary operator interface and the canonical engine used by higher-level integrations.

### Required v1 command surface

- `outpost init`
- `outpost apply`
- `outpost update <service> [--ref <ref>]`
- `outpost validate`
- `outpost status`
- `outpost logs <service> [--lines N]`
- `outpost ps`
- `outpost routes`
- `outpost exposure`
- `outpost start <service>`
- `outpost stop <service>`
- `outpost restart <service>`
- `outpost up`
- `outpost down`

### Semantics

- `init` bootstraps the host and platform
- `apply` reconciles config into runtime state
- `update` advances a service to a new commit and deploys it
- `up` is `apply` plus `start` on **all** services (including unchanged-but-stopped ones)
- `down` stops all services but keeps config, runtime state, clones, and data in place

---

## 20. MCP and Coding-Agent Integration

Outpost exposes a stdio MCP server.

### Usage model

- the CLI provides a command that starts the MCP server over stdio
- coding agents connect to it directly
- this is intended for tools such as Claude Code

### MCP v1 tools

- `list_services`
- `get_service_status`
- `start_service`
- `stop_service`
- `restart_service`
- `update_service`
- `apply_config`
- `validate_config`
- `show_routes`
- `show_exposure`
- `tail_logs`

`tail_logs` returns a bounded tail (default 200 lines) to keep an agent's context window manageable; the bound is adjustable via a count argument.

The MCP server is a thin adapter over the same internal library used by the CLI.

---

## 21. Security Posture

Security in v1 is structural, not policy-driven:

- services bind to localhost
- NGINX is the only local ingress point
- Cloudflare Tunnel is the only public exposure mechanism
- only hosts listed in exposure are public

v1 does not include:

- upstream TLS/mTLS
- built-in auth middleware
- rate limiting
- secret management
- multi-tenant isolation

---

## 22. Runtime Targets

### Primary target

- systemd Linux only
- Raspberry Pi
- VPS
- homelab box
- developer desktop

### Out of scope for v1

- Android / Termux
- macOS
- Windows

---

## 23. Non-Goals for v1

Explicitly excluded:

- replicas
- load balancing
- template units
- policy / middleware system
- rollback as a first-class command
- multi-provider tunnel support
- containers
- cross-host orchestration
- advanced health-driven routing
- passive health checks
- dynamic traffic quarantine
- built-in secret store

---

## 24. Success Criteria

Outpost v1 is successful if a user can:

1. install via `curl ... | sh`
2. run `outpost init`
3. define a YAML file with services and routes
4. deploy git-sourced services
5. expose selected services through Cloudflare Tunnel
6. update a service by advancing its git ref
7. inspect status and logs through the CLI or MCP
8. do all of this without hand-writing systemd, NGINX, or cloudflared configuration

---

## 25. Final System Definition

Outpost v1 is:

> a deterministic deployment engine that turns a YAML file into git-sourced systemd user services behind a user-level NGINX, exposed through Cloudflare Tunnel, operated through CLI and MCP.

The product’s job is to make a small Linux service stack feel boring, predictable, and safe to operate.
