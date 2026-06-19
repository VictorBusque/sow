"""Platform-wide constants shared across the engine, models, and sysdeps.

Centralised so the NGINX port and default port range never drift between the
model validators, the port allocator, and the rendered templates.
"""

from __future__ import annotations

# The user-level NGINX listens here (loopback). Distinct from any service
# listener; excluded from allocated port ranges and rejected as a declared
# `listen` port. See prd.md §1 "NGINX privilege bridge" and rfc.md §11.
NGINX_PORT: int = 41999

# First-fit allocation range for services that omit `listen`.
DEFAULT_PORT_RANGE: str = "18000-18999"

# The cloudflared user-unit name (mirrors the NGINX unit convention in
# sysdeps/nginx.py). `init` (Phase 9) creates/enables the unit under this name;
# `apply` ensures it is started when an exposure is defined. Kept here (not a
# sysdeps/cloudflared.py module) because apply needs only the string and creating
# a module for one constant would be over-building.
CLOUDFLARED_UNIT: str = "outpost-cloudflared"
