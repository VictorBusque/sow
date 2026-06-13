# PRD: Outpost — a Linux micro-platform control plane

## Overview

This product is a lightweight micro-platform for running and exposing small services on systemd Linux hosts — Raspberry Pi-class devices, VPSes, homelab boxes, and developer desktops. The platform is designed around three proven components: NGINX for ingress, routing, load balancing, and edge policy; systemd for process supervision; and a tunnel provider such as Cloudflare Tunnel for public exposure without direct port forwarding.

The product itself is not a new proxy, init system, or tunnel. It is a thin control plane and CLI that takes a declarative service specification, validates it, generates backend configuration, applies changes safely, and exposes a structured management interface to human operators and coding agents via CLI and MCP.

## Problem

There is a gap between raw self-hosting primitives and a coherent, low-footprint platform for small Linux service stacks. Existing solutions either assume a heavier substrate (Kubernetes, Nomad, full self-hosted PaaSes like Coolify), depend on a container runtime that is extra weight when services are just Go binaries or Python apps, or leave the operator hand-authoring the unholy trinity of systemd units, NGINX config, and cloudflared YAML — all of which drift apart over time and are awkward for an agent to operate safely.

The user need is a single configuration-driven workflow that can define services, routes, exposure, and lifecycle policy once, then render the appropriate NGINX, systemd, and tunnel configuration. The resulting system should feel like a tiny PaaS for low-traffic personal infrastructure, developer tooling, lightweight APIs, bots, dashboards, and agent-facing services — without mandating a container runtime.

## Goals

### Product goals

- Provide one declarative source of truth for services, routes, exposure, and runtime policy.
- Run reliably on systemd Linux (Raspberry Pi, VPS, homelab, desktop) with minimal host assumptions and no container-runtime requirement.
- Reuse mature infrastructure components rather than rebuilding them: NGINX for ingress and load balancing, systemd for service supervision, and Cloudflare Tunnel or similar for exposure.
- Own the source-to-runtime lifecycle for git-based services: clone, build, update, and rollback.
- Offer an ergonomic CLI for operators and a structured MCP surface for coding agents.

### Technical goals

- Generate deterministic configs for NGINX, systemd units, and supported tunnel providers from a higher-level platform spec.
- Support safe validation and apply workflows, including graceful NGINX reloads, `systemctl daemon-reload`, and service-level lifecycle actions.
- Keep the source model logical by avoiding backend-native nouns such as `systemd_unit`, `docker_image`, or raw NGINX directives — the platform generates these under the hood.

## Non-goals

- Replacing Kubernetes, Nomad, or a full container orchestration platform.
- Providing a multi-tenant enterprise control plane, billing system, or developer portal.
- Building a new reverse proxy, new process supervisor, or new public tunnel network.
- Cross-OS portability beyond systemd Linux in v1. macOS (launchd) and Android/Termux (no systemd) are deferred; Windows is out of scope.

## Target users

The primary users are technical operators and developers who want to run small service stacks on Linux hosts with a consistent, config-driven workflow. This includes individual engineers, homelab users, self-hosters, AI developers exposing local services, and teams building personal infrastructure tooling or agent endpoints.

The first adopter profile is a highly technical developer who values config-driven infrastructure, lightweight stacks (no container runtime), and the ability to automate operations through a CLI or coding agent integration.

## Core product concept

The platform owns only the orchestration glue. It ingests a declarative YAML file, compiles it into runtime-specific configurations, applies the resulting plan safely, and exposes state and control via CLI and MCP.

The runtime stack is:

- **NGINX**: reverse proxy, routing, path and host dispatch, load balancing, TLS termination where applicable, rate limiting, and edge-level security controls.
- **systemd**: service supervision, process lifecycle control, automatic restarts on exit with backoff and rate-limiting, and service-level operational commands via `systemctl`. Structured logs flow to journald.
- **Tunnel provider**: default public exposure via Cloudflare Tunnel, with a pluggable abstraction for alternatives such as ngrok.
- **Platform controller**: validation, generation, apply, status, journald integration, source/update lifecycle, health policy, and agent-facing API surface.

## Platform architecture

### Declarative config

The source of truth is a platform YAML file describing services, listeners, routes, exposure, health semantics, and lifecycle policy. The schema must express logical platform concepts rather than backend-specific implementation details so that the same config compiles cleanly to NGINX, systemd, and tunnel artifacts.

Representative logical sections include:

- `services`: command, args, cwd, environment (inline `environment` map/list with `${VAR}` interpolation, plus `env_file` references, Docker Compose style), `listen`/replica behavior (see "Listeners and ports"), optional `source`/`build` (see "Source and updates"), restart semantics, optional replicas.
- `routes`: a list of virtual-host objects; each vhost has a `host` (or is the default/catch-all vhost) and a `paths` map. See "Route shape".
- `exposure`: public vs local-only behavior, tunnel provider choice, domains, and exposure rules.
- `health`: process-level and endpoint-level checks used by the platform controller.

(Platform state — applied config digest, apply history, status snapshots — is tracked separately in a JSON state sidecar, not in the source spec; see "Idempotency and state".)

### Example config

A representative spec exercising the decisions below — pinned and allocated listeners, replicas, Compose-style env, health, a policy, a wildcard vhost, a local-only host, and Cloudflare exposure:

```yaml
version: 1

services:
  web:                              # single replica, operator-pinned port
    command: ./bin/web serve
    cwd: ./apps/web
    listen: 127.0.0.1:8080
    args: ["--addr", "${ADDRESS}"]   # platform injects ADDRESS=127.0.0.1:8080
    restart: always
    environment:
      LOG_LEVEL: info
    env_file: ./secrets/web.env
    health:
      http: { path: /healthz }

  api:                              # git-managed; three replicas, platform allocates ports
    source:
      git: https://github.com/me/api.git
      ref: main                     # branch | tag | sha; omit → remote default branch
    build: pip install -r requirements.txt
    command: python -m api          # cwd defaults to the managed clone
    replicas: 3                     # listen: omitted → 3 allocated ports, each binds $PORT
    restart: always
    environment:
      DATABASE_URL: sqlite:///${DATA_DIR}/api.db   # $DATA_DIR persists across updates
    health:
      http: { path: /healthz, timeout: 2s }

routes:
  - host: app.example.com           # public vhost
    paths:
      /api:
        to: api                     # targets a 3-replica service → NGINX upstream auto-built
        policy:
          rate_limit: { rps: 100, burst: 50 }
          request_headers:
            add: { X-Forwarded-By: outpost }
          auth:
            basic: ./secrets/users.htpasswd
      /:
        to: web
  - host: "*.example.com"           # wildcard vhost
    paths:
      /: { to: web }
  - host: admin.local               # local/LAN only: in routes, absent from exposure
    paths:
      /: { to: web }

exposure:
  cloudflare:
    credentials_file: ~/.cloudflared/app.json
    hosts: [app.example.com]        # only this vhost is public via the tunnel
```

The `/api` route targets a 3-replica service, so the platform builds an NGINX upstream across all three allocated ports; load balancing is native NGINX. `admin.local` has a route but is absent from `exposure.hosts`, so NGINX serves it locally only and the tunnel never sees it.

### Internal pipeline

The controller should implement a staged pipeline:

1. Parse YAML into an internal model.
2. Validate schema and topology.
3. Generate target configs.
4. Produce a plan/diff.
5. Apply atomically.
6. Reload or restart affected components (`systemctl daemon-reload` + start/restart units; reload NGINX).
7. Report final status.

This keeps the platform's internals backend-agnostic while targeting NGINX, systemd, and one or more tunnel providers.

### Idempotency and state

Operational artifacts (systemd unit files, NGINX config, tunnel config) are real files — that is what systemd, NGINX, and the tunnel agent read. In addition the controller keeps a small state sidecar as a single JSON file written next to the config (e.g. `.outpost/state.json`), holding only what files cannot cheaply answer: the digest of the last successfully applied spec, a bounded rolling history of recent applies, per-service last-known status timestamps, and (for git-managed services) the deployed commit SHA.

- **Idempotent apply**: `outpost apply` compares the desired spec's digest to the stored applied digest. If they match and services are up, the apply is a no-op.
- **Atomicity**: applies are staged, validated (`nginx -t`), the current set backed up as last-known-good, then swapped and reloaded; on health failure the apply rolls back to that backup. This is not a true cross-system transaction, but a safe swap-and-rollback over a known-good baseline.
- **No SQLite in v1**: a query engine is unnecessary for digest/history/status. SQLite (for queryable long history) is deferred to v2 if needs grow. The JSON sidecar is portable, readable, git-ignoreable, and written atomically via temp-file rename.

## Functional requirements

### 1. Service management

The platform must support defining and running multiple long-lived services, including Go binaries and Python applications. Each service must be startable, stoppable, restartable, and inspectable individually or as part of the full stack through the CLI.

#### Listeners and ports

Each service has a bind address the platform always knows; it is used for health checks and to build the NGINX upstream. The address is resolved one of two ways:

- **Declared** — set `listen:` to `host:port` or a unix socket path. The operator must ensure the binary binds there.
- **Allocated** — omit `listen:` and the platform assigns a port on `127.0.0.1` from a default range (configurable, e.g. `18000-18999`).

In both cases the platform injects the address into the service environment (via the generated unit's `Environment=`) as `PORT`, `ADDRESS` (`host:port`), and (for replicas) `OUTPOST_REPLICA_INDEX` and `OUTPOST_REPLICAS`, so a binary can bind `${PORT}` without restating the port in its flags. Platform-injected vars take precedence over operator `environment`/`env_file`; declaring `listen` and also setting `PORT`/`ADDRESS` in environment is a validation error.

Rules:

- `replicas > 1` requires allocation (omit `listen:`). The platform generates a systemd template unit (e.g. `outpost-api@.service`) and instantiates `outpost-api@1`..`@N`, each with its own port discovered via the injected env. Explicit multi-port is not supported.
- Allocated ports are stable across restarts and re-applies (persisted in the state sidecar) unless the service definition changes, so NGINX upstreams do not churn.
- Default listener transport is TCP on loopback; unix sockets are opt-in via `listen: <path>`.

`restart:` maps to the generated unit's `Restart=` directive, and the controller sets sane `RestartSec=` / `StartLimitBurst` / `StartLimitIntervalSec=` defaults so a crash-looping service is throttled rather than hammering.

#### Privilege model

By default the platform targets **systemd user units** (`systemctl --user`), so services run as the operator without root. The controller expects `loginctl enable-linger` so user services survive logout and start at boot. System units (root, `systemctl`) are supported as an opt-in for operators who want services managed at the system level.

Required lifecycle operations (mapped to `systemctl`):

- Start one service.
- Stop one service.
- Restart one service.
- Check status for one service.
- Start all services.
- Stop all services.
- Restart all services.
- Tail service logs (`journalctl -u <service>`).
- Signal or reload advanced services where supported.

### 2. Routing and ingress

The platform must generate NGINX config that supports host-based and path-based routing across multiple sub-apps and microservices. It must support multiple backends per logical service so NGINX can balance requests across replicas using native upstream capabilities such as round robin, weighted balancing, least connections, and affinity-related patterns where appropriate.

Required routing features:

- Virtual hosts.
- Path prefix routing.
- Reverse proxying to service listeners.
- Upstream grouping for replicas.
- Graceful reload after validated config changes.

#### Route shape

`routes` is a **list of virtual-host objects**. Each entry has a `host` (a literal or wildcard like `*.example.com`); omit `host` to mark the default/catch-all vhost. Within a vhost, `paths` is a map of **path prefix to target spec**. Routing match rules in v1:

- Path keys are **prefix** matches, **longest-prefix-wins** (so `/api` beats `/`).
- Exact-path and regex matching are deferred to v2.
- Duplicate `host` entries across the list are a validation error.
- Wildcards and the catch-all vhost are awkward as YAML map keys, so a list is used instead of a host-keyed map.

Example:

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

#### Policies

A path's target spec may carry an inline `policy:` block. In v1 policies are **inline-only and logical** — they express intent, never raw NGINX directives, so the platform can compile them to backend config without leaking backend nouns into the source model. Named/reusable middleware chains are deferred to v2.

v1 policy set:

- `rate_limit`: `{ rps, burst }`.
- `request_headers` / `response_headers`: `{ add, set, remove }` (each a map).
- `auth`: `{ basic: <htpasswd file path> }`.

In v1, policies attach at the **path level only**; there is no vhost- or global-level policy block. Example:

```yaml
routes:
  - host: app.example.com
    paths:
      /api:
        to: api
        policy:
          rate_limit: { rps: 100, burst: 50 }
          request_headers:
            add: { X-Forwarded-By: outpost }
          auth:
            basic: ./secrets/users.htpasswd
      /:
        to: web
```

### 3. Exposure

The platform must support secure public exposure through a file-configured tunnel provider, with Cloudflare Tunnel as the default backend in v1. The exposure model must support multiple services and hostnames behind a single tunnel configuration via ordered ingress rules.

In v1 all public exposure routes through NGINX: the traffic path is `tunnel -> nginx -> service`. The tunnel terminates at NGINX, which provides the single TLS termination point, security boundary, and policy layer. Direct tunnel-to-service exposure is deferred to a later version. A service with no route is still supervised by systemd and reachable on its localhost listener, but that is an unexposed service, not public exposure.

TLS is tunnel-managed only in v1. The tunnel provider owns the public certificate; NGINX listens on plain HTTP locally because the encrypted boundary is the tunnel. The platform does not issue, store, or renew certificates, and does not manage host trust stores. Local HTTPS for LAN-direct access (e.g. mkcert/self-signed) is deferred to v2 and is coupled to any future LAN/direct mode. As a consequence, upstream TLS/mTLS from NGINX to services is also out of scope for v1 — services listen on plain HTTP or raw TCP on localhost.

Requirements:

- Expose one or more services under one domain set or tunnel.
- Support hostname-based mapping to internal NGINX endpoints.
- Keep the public exposure abstraction generic enough to add ngrok later.

### 4. Security controls

The platform must allow NGINX to function as the outer security layer for sub-apps and APIs through centralized gateway policies. This includes support for headers, rate limiting, upstream TLS/mTLS where needed, access restrictions, and optional integration with a public auth layer such as Cloudflare Access.

This product will not provide a full enterprise API management suite, but it should allow users to secure small internal services behind one controlled entry point.

### 5. Health and supervision

systemd provides process supervision and restart-on-exit with backoff and rate-limiting (`Restart=`, `StartLimitBurst`/`StartLimitIntervalSec`). The platform controller adds higher-level health semantics beyond simple process existence, because systemd alone does not express readiness/liveness probes or tie them to routing.

Requirements:

- Track process state from systemd (`systemctl`/unit state).
- Optionally check HTTP/TCP health endpoints.
- Delay route activation until a service is healthy.
- Surface unhealthy states in CLI and MCP status outputs.

Health behavior in v1 is scoped to **status reporting and apply-time readiness gating**, not ongoing traffic shaping:

- Health is always reported in `status` and MCP outputs.
- During `outpost apply`, a route to a service is only enabled if the service passes its startup/readiness check within a configured timeout. If it never becomes healthy, NGINX is still reloaded but the route is left in a disabled/maintenance state and the apply is reported as degraded, rather than pointing at a dead backend.
- systemd continues to restart crashed processes (throttled by rate limits), so the "process died" case is handled by supervision.
- Continuously pulling an unhealthy-but-alive service out of the live NGINX upstream (passive health checks or active probe-driven reloads) is deferred to v2, since it implies a background component and reload churn that a v1 control plane should not own.

### 6. Logging and status

The platform must provide a unified operator-facing status view that merges service state, route state, and exposure state. It should surface per-service logs from journald (`journalctl -u <service>`) and optionally tail NGINX logs, so operators and agents can inspect problems without understanding low-level file layout.

Required commands:

- `status`
- `logs`
- `routes`
- `exposure`
- `ps` or equivalent service list/status view.

### 7. Environment and secrets

Services receive environment variables in Docker Compose style, supporting both inline values and file references:

- `services.<name>.environment`: an inline map (`KEY: value`) or list (`KEY=value`), with `${VAR}` interpolation resolved at generate time from the host environment plus the platform-injected variables (`PORT`, `ADDRESS`, `DATA_DIR`, `OUTPOST_REPLICA_INDEX`, `OUTPOST_REPLICAS`); platform-injected vars take precedence. Suited to non-secret configuration.
- `services.<name>.env_file`: one or more paths to env files loaded verbatim. Suited to secrets and larger env sets; the operator owns these files (typically gitignored or decrypted out of band via `age`/`sops`).
- Precedence matches Compose: inline `environment` overrides `env_file`; later `env_file` entries override earlier ones.

v1 ships no built-in secret store — no encryption at rest, generation, or rotation. The platform reads env sources and passes them through to the service via the generated unit's `Environment=` / `EnvironmentFile=` without interpreting values. Safety constraints: `status` and `logs` never echo environment contents, and `outpost generate` must reference the env source (e.g. via `EnvironmentFile=`) in rendered unit files rather than copying secret values into broadly-readable generated files where avoidable.

### 8. Source and updates

A service becomes **managed** by declaring a `source:` block; `source:` accepts only git. Outpost then owns that service's files and lifecycle end-to-end — clone, build, update, rollback. A service without `source:` is operator-provided: outpost supervises it but never touches its files, and `outpost update` does not apply to it.

Fields:

- `source.git`: the remote URL (HTTPS or SSH).
- `source.ref`: a branch, tag, or commit SHA to deploy. Omit to track the remote default branch.
- `source.path`: a subdirectory within the repo, for monorepos (default: repo root).
- `build`: an optional command run in the clone after every pull, before start (e.g. `make build`, `pip install -r requirements.txt`). Outpost does **not** detect languages or manage toolchains — the host provides them. No `build:` means clone-and-run (scripts or committed/prebuilt artifacts).

The managed clone lives under `.outpost/repos/<service>` (root configurable); outpost owns it and local edits are overwritten on update, by design. Because the clone is ephemeral, the platform guarantees a separate persistent directory per service and injects its path as `DATA_DIR` (default `.outpost/data/<service>`, also available for `${...}` interpolation). All mutable state — databases, uploads, caches — must live under `$DATA_DIR`, which is never touched by updates. This `DATA_DIR` is provided for every service, managed or not.

`outpost update <service> [--ref <ref>]` fetches, checks out the ref (or the remote default branch), runs `build:` if present, then swaps and restarts once the new instance is healthy — the old instance keeps serving throughout. On fetch, build, or health failure the running service is left untouched and the update is reported failed; there is no partial state. The actual deployed SHA is recorded in the state sidecar, and `outpost rollback <service>` checks out the previous SHA and rebuilds.

Private repositories use the operator's existing git/SSH credentials — outpost runs `git` as the operator user; there is no credential manager in v1. Auto-update (polling or webhooks) is deferred to v2: v1 updates are explicit, keeping the control plane synchronous with no background daemon.

## CLI requirements

The CLI is the primary operator interface and the canonical engine used by higher-level integrations. It must be stable, scriptable, and deterministic.

### Required v1 command surface

- `outpost validate`
- `outpost generate`
- `outpost apply`
- `outpost status`
- `outpost logs`
- `outpost start <service>`
- `outpost stop <service>`
- `outpost restart <service>`
- `outpost update <service> [--ref <ref>]`
- `outpost rollback <service>`
- `outpost up`
- `outpost down`
- `outpost ps`
- `outpost routes`
- `outpost exposure`

These commands should map internally to render/apply logic plus `systemctl` operations (`--user` by default), `git` for source updates, and `journalctl` for logs, alongside tunnel-provider state inspection.

### Apply semantics

`outpost apply` must:

1. Parse and validate the platform spec.
2. Generate all target configs to a staging area.
3. Test the NGINX config before activation.
4. Write configs atomically (swap last-known-good for staged set).
5. `systemctl daemon-reload`, then start or reload affected services.
6. Gracefully reload NGINX.
7. Verify health and report final state.

Rollback behavior is defined in "Idempotency and state": on an invalid config test or post-apply health failure, restore the last-known-good backup set and reload; if health checks never pass within the timeout, the apply is reported as degraded.

## MCP and coding-agent integration

The product must expose a structured, minimal MCP server so coding agents can inspect and operate the platform through typed tools instead of raw shell access. MCP is appropriate because it standardizes tool discovery and invocation for hosts and agent clients.

### MCP v1 tools

- `list_services`
- `get_service_status`
- `start_service`
- `stop_service`
- `restart_service`
- `update_service`
- `rollback_service`
- `apply_config`
- `validate_config`
- `show_routes`
- `show_exposure`
- `tail_logs`

The CLI should remain the core implementation, with the MCP server acting as an adapter over the same internal library.

## Runtime targets

### Primary target

**systemd Linux** — Raspberry Pi, VPSes, homelab boxes, and developer desktops. NGINX, systemd, and tunnel agents are all native and first-class here, and systemd's restart policies, journald logging, and template units map cleanly onto the platform's service model. User units (no root, with `enable-linger`) are the default; system units are supported.

### Explicitly deferred

- **Android/Termux**: out of scope for v1. Termux has no systemd; the previous runit-based path relied on termux-services, which is no longer in the architecture.
- **macOS**: out of scope for v1. macOS uses launchd, a separate supervisor; supporting it would mean a second backend. A launchd backend is conceivable later but is not promised.
- **Windows**: out of scope for v1.

## Adoption strategy for exposure backend

Cloudflare Tunnel should be the default v1 exposure backend because it is widely adopted in self-hosting workflows, supports exposing multiple services from a single configuration, and removes the need for direct port forwarding. The platform should keep the exposure abstraction generic enough to support ngrok later as a second provider for developer-centric or temporary workflows.

The platform should not make tunnel-provider syntax the source model. Instead, the source model should describe logical exposure intent and compile it into provider-native configuration.

## Open questions

(None remaining for v1 — all original questions have been resolved into the sections above. Deferred items are noted inline as v2 candidates.)

## Success criteria

The product will be successful in v1 if a technical user can take one declarative config file and, on a systemd Linux host (Raspberry Pi, VPS, or desktop), reliably:

- start a small multi-service stack,
- route requests through NGINX,
- load balance replicas where configured,
- expose selected endpoints through a tunnel,
- deploy services from git, and update or roll them back,
- inspect service health and logs,
- operate everything through a CLI and MCP surface,
- and do so without manually authoring raw NGINX config, systemd units, or tunnel configs.

## v1 summary

The differentiator is not the infrastructure primitives — NGINX, systemd, and a tunnel provider are all proven and unglamorous. It is the clean logical abstraction over them, the safe apply-and-rollback workflow, and a consistent operational model that works the same across any systemd Linux box, drivable by both an operator and a coding agent.
