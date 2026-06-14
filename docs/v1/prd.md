# PRD: Outpost — a Linux micro-platform control plane

## Overview

Outpost is a lightweight control plane for running and exposing small **git-sourced services** on a single systemd Linux host — a Raspberry Pi, a VPS, a homelab box, or a developer desktop. It is not a new proxy, init system, or tunnel. It is a thin CLI (with an MCP surface for coding agents) that takes one declarative YAML file and turns it into running systemd units plus NGINX reverse-proxy config.

v1 is deliberately small. It implements exactly one loop: **a YAML file defines services and routes, and Outpost turns that into running processes behind NGINX on one Linux machine.** Everything that would turn it into a mini-orchestrator is deferred.

## Problem

There is a gap between raw self-hosting primitives and a coherent, low-footprint way to run small service stacks on Linux. Existing solutions either assume a heavier substrate (Kubernetes, Nomad, full self-hosted PaaSes like Coolify), depend on a container runtime that is extra weight when a service is just a Go binary or a Python app, or leave the operator hand-authoring the unholy trinity of systemd units, NGINX config, and cloudflared YAML — which drift apart over time and are awkward for an agent to operate safely.

The user need is a single configuration-driven workflow that defines services and routes once, then renders the appropriate systemd and NGINX configuration. The result should feel like a tiny, boring PaaS for low-traffic personal infrastructure, developer tooling, lightweight APIs, bots, dashboards, and agent-facing services — without mandating a container runtime.

## Goals

- Provide one declarative source of truth for services and routes.
- Run on systemd Linux (Raspberry Pi, VPS, homelab, desktop) with minimal host assumptions and no container-runtime requirement.
- Reuse mature components rather than rebuilding them: NGINX for ingress, systemd for supervision, Cloudflare Tunnel for exposure.
- Own the git lifecycle for every service: clone, build, update.
- Offer an ergonomic CLI for operators and a structured MCP surface for coding agents.

### The four primitives

v1 has exactly four concepts:

- **Service** — one git repo, one command, one optional build step, one port, and environment variables.
- **Route** — a `host`/`path` mapping to a service.
- **apply** — render config, check out the pinned commit, build if needed, write the systemd and NGINX files, reload, and start.
- **update** — advance the tracked git ref to a new commit, then apply it.

## Non-goals

For v1, the following are explicitly out of scope (reasonable later additions, but they add state machines or platform complexity that make the first version harder to ship):

- **Replicas / load balancing / template units.** One service = one instance, one port.
- **Policy and middleware blocks** — rate limiting, header rewriting, auth middleware, reusable chains.
- **A tunnel-provider abstraction or multi-provider exposure.** Cloudflare Tunnel only.
- **Rollback** as a first-class command or rollback-history state. Reverting to a known-good commit is a manual `update`/hand-edit of `sha` + `apply`; failed applies auto-revert to the last-known-good set, but there is no history to roll back *through*.
- **Advanced, health-driven routing** — passive/active health checks, maintenance/quarantine states, degraded applies.
- **Containers**, multi-host/clustering, and a built-in secret store.
- Cross-OS portability beyond systemd Linux. macOS (launchd) and Android/Termux (no systemd) are deferred; Windows is out of scope.

## Target users

Technical operators and developers who want to run small service stacks on a Linux host with a consistent, config-driven workflow: individual engineers, homelab users, self-hosters, AI developers exposing local services, and teams building personal infrastructure tooling or agent endpoints.

Outpost is a **deployment** platform for git-sourced services, not a rapid local-development runner: every code change must be committed and pushed before it reaches the running service, so hot-reload inner-loop iteration is intentionally out of scope (see "Source and updates").

## Core product concept

Outpost owns only the orchestration glue. It ingests a declarative YAML file, compiles it into systemd units and NGINX config, applies the result safely, and exposes state and control via CLI and MCP. The runtime stack is:

- **NGINX**: reverse proxy and host/path routing to services.
- **systemd (user units)**: process supervision and lifecycle.
- **Cloudflare Tunnel**: the only public-exposure path.
- **Platform controller**: validation, generation, apply, status, journald integration, and the git lifecycle.

## Platform architecture

### Declarative config

The source of truth is a platform YAML file describing services, routes, and exposure. The schema expresses logical concepts and compiles to systemd units and NGINX config.

- `services`: required `source` (git; see "Source and updates") with optional `build`, `command`, optional `args`, `listen` (see "Listeners and ports"), `restart`, `environment` / `env_file` (Docker Compose style), and optional `health`.
- `routes`: a list of virtual-host objects; each vhost has a `host` (literal or wildcard) and a `paths` map of path prefix → service. See "Route shape".
- `exposure`: Cloudflare Tunnel config and the list of hosts exposed publicly.

(Platform state — the applied spec digest, allocated port assignments, and apply timestamps — is tracked in a JSON state sidecar, not in the spec; see "Idempotency and state". Live per-service status is never persisted; it is computed from `systemctl --user` / journald at query time.)

### Example config

```yaml
version: 1

# port_range: 18000-18999        # optional; default range for allocated (listen-less) ports

services:
  web:                              # declared (operator-pinned) port
    source:
      git: https://github.com/me/web.git
      ref: main
      sha: 9f2c1a4                  # deployed commit; written by outpost
    command: ./bin/web serve        # runs in <clone>/<source.path> (repo root if omitted)
    listen: 127.0.0.1:8080
    args: ["--addr", "${ADDRESS}"]  # platform injects ADDRESS=127.0.0.1:8080
    restart: always
    environment:
      LOG_LEVEL: info
    env_file: ./secrets/web.env
    health:
      http: { path: /healthz }
      timeout: 30                 # seconds the startup check may take (default 30)

  api:                              # allocated port; binds $PORT
    source:
      git: https://github.com/me/api.git
      ref: main                     # the update target
      sha: b7e0d33                  # the deploy target; apply checks this out exactly
    build: pip install -r requirements.txt
    command: python -m api
    environment:
      DATABASE_URL: postgres://api@127.0.0.1:5432/api
    health:
      http: { path: /healthz }

routes:
  - host: app.example.com           # public vhost
    paths:
      /api: { to: api }
      /:    { to: web }
  - host: admin.local               # local/LAN only: in routes, absent from exposure
    paths:
      /: { to: web }

exposure:
  cloudflare:
    credentials_file: ~/.cloudflared/app.json
    hosts: [app.example.com]        # only this vhost is public via the tunnel
```

`admin.local` has a route but is absent from `exposure.hosts`, so NGINX serves it locally only and the tunnel never sees it.

### Internal pipeline

`outpost apply` implements a staged pipeline:

1. Parse YAML into an internal model.
2. Validate schema and topology.
3. **Materialize sources**: for each service, ensure the clone at `~/.local/share/outpost/repos/<service>` is checked out at exactly the **pinned `sha`** — clone and run `build:` if missing; check out (and rebuild) if `sha` changed; if `sha` is empty (first deploy), resolve the current `ref`→sha, write it back into the config (a one-time seed), and build. Clones are keyed per service, so two services from the same repo get independent clones. `apply` never advances an already-set `sha` within a ref — pulling the latest is `update`'s job.
4. Generate target configs (systemd units, NGINX server blocks, cloudflared config).
5. Test the NGINX config (`nginx -t`).
6. Apply atomically: swap in the staged set, `systemctl --user daemon-reload`, and start/restart the affected services. (NGINX is **not** reloaded yet.)
7. Run the startup health check (if defined), probing the service's **local listener** (`127.0.0.1:<port>` or its unix socket) directly — not through NGINX. On success, reload NGINX so the new config goes live and the route becomes active, then commit the apply. On failure, restore the previous working state (old units + old NGINX config, reloaded) and report the apply failed. Routing new traffic through NGINX only after the health check passes closes the broken-service-serves-live-traffic window.

### Idempotency and state

Operational artifacts (systemd unit files, NGINX config, cloudflared config) are real files. In addition the controller keeps a tiny JSON state sidecar in the runtime directory (`~/.local/share/outpost/state.json`) holding exactly three things: the digest of the last successfully applied spec, the allocated port assignments for services that omit `listen`, and timestamps of recent successful applies. **Nothing else** — no apply history, no per-service status, no operational metadata, no event log. Per-service status is computed live from `systemctl --user` / journald at query time, never persisted. If a question cannot be answered by the files plus those three values, it is out of scope for v1. The **current deployed SHA lives in the config** (`source.sha`), which is the source of truth for what is deployed.

- **Idempotent apply**: `outpost apply` compares the spec's digest to the stored applied digest. If they match and services are up, the apply is a no-op. `apply` and `update` both update the stored digest, so an `apply` after an `update` stays a no-op.
- **Atomicity**: applies are staged, validated, the current set backed up as last-known-good, then swapped. If the generated config is invalid or the startup health check fails, the apply reverts to the backup so the running system is left unchanged. v1 applies are all-or-nothing: a single service failing its startup gate reverts the entire apply (including services that passed), and only **one** last-known-good set is retained — there is no history to roll back *through* (see Non-goals). Because the atomic swap stops and restarts the changed services while NGINX still holds the old routing, there is a brief window where those upstreams return 502s until the health check passes and NGINX reloads; v1 accepts this short blip as the price of guaranteeing that live traffic never reaches a service that failed its gate.
- **No SQLite in v1**: a query engine is unnecessary for digest/status. SQLite is deferred to v2 if needs grow. The JSON sidecar is portable, readable, git-ignoreable, and written atomically via temp-file rename.

## Functional requirements

### 1. Service management

Each service is one git repo run as one process behind one port. Services are startable, stoppable, restartable, and inspectable individually or as part of the full stack through the CLI.

#### Listeners and ports

Each service has one bind address the platform always knows; it is used for the health check and the NGINX upstream. The address is resolved one of two ways:

- **Declared** — set `listen:` to `host:port` or a unix socket path. The operator must ensure the binary binds there.
- **Allocated** — omit `listen:` and the platform assigns a port on `127.0.0.1` from a configurable range (default `18000-18999`, set via a top-level `port_range:` field). Allocation is first-fit (lowest free port in the range). The range excludes the NGINX port `41999` and any operator-declared `listen` ports; `listen` values that collide with `41999`, with each other, or with an already-allocated port are a validation error. If the range is exhausted, `apply` fails fast with a clear error rather than silently reusing a port.

In both cases the platform injects the address into the service environment (via the generated unit's `Environment=`) as `PORT` and `ADDRESS` (`host:port`), so a binary can bind `${PORT}` without restating the port in its flags. Platform-injected vars take precedence over operator `environment`/`env_file`; declaring `listen` and also setting `PORT`/`ADDRESS` in environment is a validation error. Default transport is TCP on loopback; unix sockets are opt-in via `listen: <path>`. Allocated ports are persisted in the state sidecar and reused unless the service definition changes.

`restart:` maps to the generated unit's `Restart=` directive, and the controller sets sane `RestartSec=` / `StartLimitBurst` / `StartLimitIntervalSec=` defaults so a crash-looping service is throttled rather than hammering.

#### Privilege model

The platform targets **systemd user units only** (`systemctl --user`); services run as the operator without root. The controller expects `loginctl enable-linger` so user services survive logout and start at boot. System units (root) are not supported in v1.

##### NGINX privilege bridge

NGINX is managed as a **systemd user unit** too — an instance the operator runs (no root). Because the tunnel terminates TLS, NGINX only needs to listen on a distinctive high local port (default `127.0.0.1:41999`, distinct from any service listener) or a unix socket, so no privileged bind is required. The controller writes all **generated server blocks** under the Outpost-owned runtime directory (`~/.local/share/outpost/generated/nginx/`), matching the layout in RFC §6. The user NGINX's main `nginx.conf` — which carries a single `include` line pointing at those generated server blocks — is created once by `init` under the user-owned config directory (`~/.config/outpost/nginx/`). Cloudflared's ingress points at this NGINX (`http://127.0.0.1:41999`).

This keeps the no-root model intact: Outpost only ever writes inside its own directory and reloads via `systemctl --user reload`. There is no sudo and no sudoers rule in v1. A documented **one-time setup step** installs the `include` line and starts the user NGINX unit — analogous to `enable-linger`, a host prerequisite, not an ongoing privileged operation.

Required lifecycle operations (mapped to `systemctl --user`): start/stop/restart one service, check status for one service, start/stop/restart all services, and tail service logs (`journalctl --user -u <service>`).

### 2. Routing

Outpost generates NGINX config for host-based and path-based routing to services.

`routes` is a list of virtual-host objects. Each entry has a `host` (a literal or wildcard like `*.example.com`); omit `host` to mark the default/catch-all vhost. Within a vhost, `paths` is a map of **path prefix to target service** (`{ to: <service> }`). Path keys are prefix matches, longest-prefix-wins (so `/api` beats `/`). Exact-path and regex matching are deferred to v2. Duplicate `host` entries are a validation error.

```yaml
routes:
  - host: app.example.com
    paths:
      /api: { to: api }
      /:    { to: web }
  - host: "*.example.com"      # wildcard
    paths:
      /: { to: web }
  -                            # default/catch-all vhost (host omitted)
    paths:
      /: { to: web }
```

v1 has **no in-band traffic policy**: no rate limiting, header rewriting, or auth middleware, and no reusable middleware chains. Policies/middleware are a later addition.

### 3. Exposure

Public exposure is **Cloudflare Tunnel only** in v1; there is no provider abstraction. All public exposure routes through NGINX: the traffic path is `cloudflared -> nginx (127.0.0.1:41999) -> service`. The tunnel terminates TLS and owns the public certificate; NGINX listens on plain HTTP locally because the encrypted boundary is the tunnel. The platform does not issue, store, or renew certificates. A service with no route is still supervised by systemd and reachable on its localhost listener, but that is an unexposed service, not public exposure.

Outpost renders a cloudflared config from `exposure.cloudflare` (`credentials_file` plus the `hosts` to expose). cloudflared runs as a **platform-managed `systemd --user` unit**: `outpost init` enables it and `apply` ensures it is started, but its process supervision is delegated to systemd (just like any service) — Outpost does not implement its own supervisor for the tunnel. cloudflared points at the local NGINX (`http://127.0.0.1:41999`), and Outpost only renders the host list beyond that mapping. Direct tunnel-to-service exposure and additional providers (e.g. ngrok) are deferred.

### 4. Security posture

With no policy engine in v1, security comes from the architecture: services bind loopback, NGINX is the single local entry point, and only the hosts listed in `exposure.cloudflare.hosts` are public via the tunnel (optionally fronted by the operator's own Cloudflare Access, which is outside Outpost). Upstream TLS/mTLS from NGINX to services is out of scope — services listen on plain HTTP or raw TCP on localhost.

### 5. Health

Health in v1 is a **simple startup check only**, used to gate an apply — not ongoing traffic shaping.

- If a service defines `health` (`http: { path }` or a TCP check), `apply` probes it on the service's **local listener** and waits for it to pass within a timeout (`health.timeout`, default 30 seconds) before reloading NGINX — so the route only goes active once the check passes.
- If no `health` is defined, `apply` succeeds once systemd reports the unit active.
- If the check never passes, the apply **fails** and the previous working state is restored (see "Atomicity").

There are no passive/active health probes, no pulling of unhealthy services out of the live upstream, and no degraded/maintenance states in v1 — systemd handles crash-restart; apply handles the startup gate.

**The gate covers services, not the Cloudflare Tunnel.** `apply` only verifies cloudflared is *started* (its systemd unit active), and that enters the rollback trigger set solely as a **start** failure (a subprocess error). Cloudflared **edge registration** — the tunnel actually accepting and serving a hostname route at the upstream edge — is asynchronous, unobservable via `systemctl is-active`, and deliberately **not** a v1 rollback trigger: a registration failure is typically caused by credentials/network/edge state rather than by the config Outpost just applied, so reverting to the last-known-good set would not recover the route and would only tear down a locally-healthy apply. systemd keeps cloudflared alive and it re-registers on its own reconnect timeline; Outpost surfaces the unit's state at query time via `status`/MCP like any other runtime component. Detecting a dead public route (without rolling back on it) is a candidate for the deferred passive/active connectivity checks, not for v1's startup gate.

### 6. Logging and status

Outpost provides a unified status view: which services are running (and their systemd unit state), which routes are configured, and which hosts are exposed. "Route state" means only whether a route is configured, not a hidden routing state machine. It surfaces per-service logs from journald (`journalctl --user -u <service>`), so operators and agents can inspect problems without understanding low-level file layout. Log output is **bounded**: `logs <service>` (and the MCP `tail_logs` tool) emit a tail, default the last 200 lines, adjustable with `--lines N`, so an agent's context window is never flooded by raw journald output.

Required commands: `status`, `logs <service>`, `routes`, `exposure`, `ps`.

### 7. Environment and secrets

Services receive environment variables in Docker Compose style:

- `services.<name>.environment`: an inline map (`KEY: value`) or list (`KEY=value`), with `${VAR}` interpolation resolved at generate time from the host environment plus platform-injected variables (`PORT`, `ADDRESS`, `DATA_DIR`); platform-injected vars take precedence. Suited to non-secret configuration.
- `services.<name>.env_file`: one or more paths to env files loaded verbatim. Suited to secrets; the operator owns these files (typically gitignored or decrypted out of band via `age`/`sops`).
- Precedence matches Compose: inline `environment` overrides `env_file`; later `env_file` entries override earlier ones.

**Cross-service references.** Any `environment` value or `args` entry can also
reference another service's platform facts via the dotted form
`${<name>.<FIELD>}`, where `FIELD` is one of `ADDRESS`, `PORT`, or `DATA_DIR`.
For example, `${api.ADDRESS}` resolves to whatever address the `api` service was
given (declared or allocated), and `${db.DATA_DIR}` gives the data directory of
`db`. Only the three platform facts are exposed cross-service — one service's
operator-defined `environment` is never reachable from another (that channel may
carry secrets). A reference to an unknown service, an unexposed field, or a
field the target lacks fails fast.

v1 ships no built-in secret store. The platform reads env sources and passes them through via the generated unit's `Environment=` / `EnvironmentFile=` without interpreting values. Safety constraints: `status` and `logs` never echo environment contents, and generated unit files reference the env source (e.g. via `EnvironmentFile=`) rather than copying secret values into broadly-readable generated files where avoidable.

### 8. Source and updates

**Every service is git-sourced** (typically a GitHub repository). A `source:` block is required on every service and accepts only git; there is no operator-provided / no-source service class in v1. Outpost owns every service's files and lifecycle — clone, build, update. As above, Outpost is a deployment platform, not a local-dev runner: every code change must be committed and pushed before it reaches the running service.

Fields:

- `source.git`: the remote URL (HTTPS or SSH).
- `source.ref`: the branch or tag this service tracks — the **update target** (what `outpost update` advances to). Omit to track the remote default branch.
- `source.sha`: the exact commit currently deployed — the **deploy target** (what `outpost apply` checks out). It may be empty before first deploy, in which case `apply` resolves `ref`→sha once and seeds it; thereafter `apply` never writes `sha`, and `update` is the only command that advances it. Editing `ref` alone does **not** redeploy; advancing to a new commit goes through `update`, which owns `sha`.
- `source.path`: a subdirectory within the repo, for monorepos (default: repo root). The generated unit's `WorkingDirectory=` is set to `<clone>/<source.path>`, and `command`/`build` run there.
- `build`: an optional command run in the clone after checkout, before start (e.g. `make build`, `pip install -r requirements.txt`). Outpost does **not** detect languages or manage toolchains — the host provides them. No `build:` means clone-and-run (scripts or committed/prebuilt artifacts).

The config is the source of truth for what is deployed: reading a service's `source.git`/`ref`/`sha` gives the repo, the tracked ref, and the exact running commit. Because `sha` lives in the config, `apply` is a pure, local reconcile to that exact commit and never pulls on its own.

The managed clone lives under `~/.local/share/outpost/repos/<service>`; clones are keyed by service name, so two services pointing at the same repo (e.g. a monorepo with different `source.path`) get independent clones — no checkout conflicts, at the cost of extra disk. Outpost owns each clone and local edits are overwritten on update, by design. Because the clone is ephemeral, the platform injects a separate persistent directory per service as `DATA_DIR` (default `~/.local/share/outpost/data/<service>`, available for `${...}` interpolation). All mutable state — databases, uploads, caches — must live under `$DATA_DIR`, which is never touched by updates.

- **`outpost apply`** reconciles to whatever `source.sha` says: clone if missing, check out the sha, build if the clone changed, then render and activate configs. The only time `apply` writes `source.sha` is the one-time seed of an empty field (resolving `ref`→sha on first deploy); it never advances an already-set sha and never pulls within a ref.
- **`outpost update <service> [--ref <ref>]`** is the **only** command that advances an existing `source.sha`: it fetches, resolves the current `ref` (or `--ref`, which also writes `ref`) to a new sha, writes that sha into `source.sha`, then runs `apply` (which now just reconciles to the value `update` just wrote). On fetch/build/health failure the running service is left untouched and the update is reported failed.

Because the config is also written by `update`, the operator interacts primarily through the CLI/MCP rather than hand-editing YAML; if the config is version-controlled, each update appears as a commit (a free audit log — commit afterward). Private repositories use the operator's existing git/SSH credentials — Outpost runs `git` as the operator user; there is no credential manager in v1. Git runs non-interactively, so the credentials (SSH keys, an HTTPS credential helper, or a cached token) must already be resolvable for the operator account, or clone/fetch fails fast rather than blocking on a prompt. Auto-update (polling or webhooks) is deferred to v2.

## CLI requirements

The CLI is the primary operator interface and the canonical engine used by higher-level integrations. It must be stable, scriptable, and deterministic.

### Required v1 command surface

- `outpost init`
- `outpost validate`
- `outpost apply`
- `outpost status`
- `outpost logs <service> [--lines N]`
- `outpost ps`
- `outpost routes`
- `outpost exposure`
- `outpost start <service>`
- `outpost stop <service>`
- `outpost restart <service>`
- `outpost update <service> [--ref <ref>]`
- `outpost up`
- `outpost down`

These map internally to render/apply logic plus `systemctl --user`, `git` for source updates, and `journalctl --user` for logs.

### Apply semantics

`outpost apply` must: parse and validate the spec; materialize sources to the pinned sha (seeding an empty `sha` once); generate configs to a staging area; test the NGINX config; write configs atomically (swap last-known-good for staged set); `daemon-reload` and start/restart affected services; run the startup health check against each service's local listener; **then**, on success, reload NGINX to activate routing and report final state; on failure, revert to the last-known-good backup and reload NGINX back to it. Reloading NGINX only after the health check passes ensures live traffic never reaches a service that failed its gate.

### Stack lifecycle

`up` and `down` are the stack-level lifecycle pair:

- **`outpost up`** = `apply` + start **all** services. `apply` only starts/restarts services whose definition changed; `up` additionally runs `systemctl --user start` on every service, so unchanged-but-stopped services come up too. It is the one-shot "make everything run" command and the first command run on a fresh host. Idempotent.
- **`outpost down`** = stop all services only. Leaves the spec, generated units, NGINX blocks, clones, and data in place, so a subsequent `up` brings the stack straight back. Full teardown/uninstall is out of scope for v1.

## MCP and coding-agent integration

Outpost exposes a minimal MCP server so coding agents can inspect and operate the platform through typed tools instead of raw shell access. The CLI remains the core implementation; the MCP server is an adapter over the same internal library.

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

## Runtime targets

**systemd Linux** is the only v1 target — Raspberry Pi, VPSes, homelab boxes, and developer desktops, using systemd user units (no root, with `enable-linger`). Android/Termux (no systemd), macOS (launchd), and Windows are out of scope for v1.

## Open questions

(None remaining for v1 — deferred items are noted inline as later additions.)

## Success criteria

v1 is successful if a technical user can take one declarative config file and, on a single systemd Linux host, reliably: clone git services, build them, run each behind one port, route requests to them through NGINX, expose selected hosts through Cloudflare Tunnel, update a service by advancing its git ref, inspect status and logs, and do all of it through a CLI and MCP surface — without hand-authoring systemd units, NGINX config, or cloudflared YAML.

## v1 summary

v1 ships exactly one loop: a YAML file defines git-sourced services and host/path routes, and Outpost renders that into running systemd user units behind a user NGINX, exposed through Cloudflare Tunnel. Every feature in v1 must directly help a technical user deploy a small git-based service stack on one Linux machine with as little ceremony as possible. Replicas, policies, multi-provider exposure, rollback, and advanced health-driven routing are all reasonable later additions — but not in v1.
