# Outpost `outpost.yaml` — Config Schema Reference (v1)

## Purpose and authority

This is the canonical field-level schema for `~/.config/outpost/outpost.yaml`. It is the contract the Pydantic models in `models/` implement: every key, type, default, and validation rule lives here.

It is **derived from** `prd.md` and `rfc.md`. If this reference and those specs ever disagree, the specs govern — raise the discrepancy rather than silently picking one. Where the specs left a detail open (e.g. `restart` default, the TCP-check shape), this document pins it; changing such a value is a doc change, not a silent implementation choice.

The config is parsed into an **immutable** Pydantic v2 model; every mutation returns a new instance. All file writes go through temp-file + atomic rename. The deployed SHA lives **in the config** (`source.sha`), not in `state.json`.

## Top-level structure

```yaml
version: <int>
port_range: <str>          # optional
services:  <map>           # required
routes:    <list>          # optional
exposure:  <map>           # optional
```

### Top-level fields

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `version` | int | yes | — | Schema version. Currently `1`. |
| `port_range` | str | no | `"18000-18999"` | Inclusive range `"lo-hi"` for allocated (listen-less) ports. Excludes the NGINX port `41999` and all declared `listen` ports. |
| `services` | map&lt;name, Service&gt; | yes | — | Map of service name → spec. Names are the unit/clone key. |
| `routes` | list&lt;Route&gt; | no | `[]` | Virtual-host routing rules. A service with no route is still supervised and reachable on its localhost listener. |
| `exposure` | Exposure | no | — | Cloudflare Tunnel public exposure. |

## `services.<name>`

Each service is one git repo run as one process behind one bind address.

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `source` | Source | yes | — | Git source. Required on every service; v1 accepts only git. |
| `command` | str | yes | — | Executable to run (may include inline args). Runs in `WorkingDirectory`. |
| `args` | list&lt;str&gt; | no | `[]` | Extra args appended to `command`. Supports `${VAR}` interpolation. |
| `build` | str | no | — | Shell command run in the clone after checkout, before start. Runs only when the clone is freshly made or `sha` changed since last apply (idempotent). |
| `listen` | str | no | allocated | `host:port` or a unix socket path. Omit to allocate from `port_range`. |
| `restart` | str | no | `"on-failure"` | Maps to systemd `Restart=`. Accepted: `no`, `on-success`, `on-failure`, `always`, `on-abnormal`, `on-abort`, `on-watchdog`. |
| `environment` | map&lt;str,str&gt; \| list&lt;str&gt; | no | — | Inline env. Map (`KEY: value`) or list (`KEY=value`). Supports `${VAR}` interpolation. |
| `env_file` | str \| list&lt;str&gt; | no | — | Path(s) to env files loaded verbatim. Suited to secrets. |
| `health` | Health | no | — | Startup health gate. See below. |

The controller sets sane `RestartSec=`, `StartLimitBurst=`, `StartLimitIntervalSec=` defaults so a crash-looping service is throttled, not hammering.

### `services.<name>.source`

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `git` | str | yes | — | Remote URL, HTTPS or SSH. |
| `ref` | str | no | remote default branch | Branch/tag tracked by `update` — the **update target**. |
| `sha` | str | no | empty | Exact deployed commit — the **deploy target** `apply` checks out. Empty before first deploy; seeded once by `apply`, advanced only by `update`. |
| `path` | str | no | repo root | Subdirectory for monorepos. Sets `WorkingDirectory=` to `<clone>/<path>`; `command`/`build` run there. |

Clones are keyed **per service name** under `~/.local/share/outpost/repos/<name>`, so two services from the same repo get independent clones. Local edits in a managed clone are overwritten on update by design.

### `services.<name>.health`

A startup check that gates the apply. Exactly one of `http` / `tcp` may be set.

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `http` | map | no | — | `{ path: /healthz }`. HTTP GET against the service's local listener; 2xx/3xx passes. |
| `tcp` | bool | no | — | `true` = TCP connect check against the local listener. |
| `timeout` | int (seconds) | no | `30` | Max time `apply` waits for the check to pass before treating it as failure. |

If no `health` is defined, `apply` succeeds once systemd reports the unit active. The check always probes the **local listener** (`127.0.0.1:<port>` or the unix socket) directly — never through NGINX. There are no passive/active probes or live-upstream pruning in v1.

## Platform-injected environment

For every service, the controller injects (via the unit's `Environment=`):

| Var | Value |
| --- | --- |
| `PORT` | The port (TCP only). |
| `ADDRESS` | `host:port` for TCP; the socket path for a unix `listen`. |
| `DATA_DIR` | Persistent per-service dir, default `~/.local/share/outpost/data/<name>`. Never touched by updates. |

Precedence (highest → lowest):

1. Platform-injected (`PORT`, `ADDRESS`, `DATA_DIR`)
2. Inline `environment`
3. Later `env_file` entries
4. Earlier `env_file` entries

Declaring `listen` **and** setting `PORT`/`ADDRESS` in `environment` is a validation error. `status` and `logs` never echo environment contents; generated units reference env files via `EnvironmentFile=` rather than copying secrets into broadly-readable files where avoidable.

### Service references

Beyond bare `${VAR}` (which resolves against this service's own merged env), any
`environment` value or `args` entry can reference **another service's platform
facts** via the dotted form `${<name>.<FIELD>}`:

| Reference | Resolves to |
| --- | --- |
| `${api.ADDRESS}` | `api` service's address (`host:port` or socket path) |
| `${worker.PORT}` | `worker` service's TCP port (absent for unix-socket services) |
| `${db.DATA_DIR}` | `db` service's persistent data directory |

Only the three platform facts (`ADDRESS`, `PORT`, `DATA_DIR`) are exposed
cross-service. One service's operator-defined `environment` is never reachable
from another — that channel may carry secrets, and copying them across generated
unit files would violate the security rule above. A reference to an unknown
service, an unexposed field, or a field the target lacks (e.g. `PORT` on a
unix-socket service) fails fast rather than emitting a literal.

A reference with no dot — `${ADDRESS}`, `${PORT}`, `${DATA_DIR}`, or any
operator-defined key — is shorthand for `<self>.<FIELD>` and resolves against
this service's own merged environment (its inline env plus its platform facts).

## `routes`

A list of virtual-host objects. Path keys are prefix matches, longest-prefix-wins (so `/api` beats `/`).

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `host` | str | no | catch-all | Literal or wildcard (`*.example.com`). Omit for the default/catch-all vhost. |
| `paths` | map&lt;prefix, {to}&gt; | yes | — | Path prefix → `{ to: <service> }`. |

Duplicate `host` entries are a validation error. A `to` referencing an unknown service is a topology error.

## `exposure`

Public exposure is Cloudflare Tunnel only in v1.

```yaml
exposure:
  cloudflare:
    credentials_file: <str>     # required
    hosts: [<str>, ...]         # required
```

`hosts` is the subset of routed vhosts exposed publicly via the tunnel. A vhost in `routes` but absent from `hosts` is served locally/LAN only. The traffic path is `cloudflared → NGINX (127.0.0.1:41999) → service`; cloudflared terminates TLS and owns the public certificate — Outpost issues, stores, or renews no certificates.

## Validation rules (collected)

These all fail `apply`/`validate` fast with a clear error before any system mutation:

- `version` must be `1`.
- `services` is non-empty; every service has a `source` with a `git` URL.
- `command` is required on every service.
- Declaring `listen` **and** setting `PORT`/`ADDRESS` in `environment` is rejected.
- A `listen` value colliding with the NGINX port `41999`, another `listen`, or an already-allocated port is rejected.
- A `listen` using the unix-socket form is allowed; two services may not claim the same socket path.
- `port_range` (custom or default) excludes `41999` and all declared `listen` ports; if exhausted, `apply` fails fast rather than reusing a port.
- `health`, if present, has exactly one of `http`/`tcp`; `health.timeout` must be positive.
- `restart`, if present, is one of the accepted systemd values.
- `routes` has no duplicate `host`; every `paths.*.to` references a defined service.
- `exposure.hosts` (if present) must each match a `host` (or wildcard) declared in `routes`.

## Full example

See [`examples/full-stack.yaml`](examples/full-stack.yaml) for an exhaustive, realistic config exercising every field with inline annotations.
