# Low-Level Design: Harden outbound URL fetches against SSRF

*Created: 2026-07-15*
*Author: Claude (kimi-k2-7-code)*
*Status: Draft*

## Table of Contents
1. [Overview](#overview)
2. [Codebase Analysis](#codebase-analysis)
3. [Architecture](#architecture)
4. [Data Models](#data-models)
5. [API / CLI Design](#api--cli-design)
6. [Configuration Parameters](#configuration-parameters)
7. [New Dependencies](#new-dependencies)
8. [Implementation Details](#implementation-details)
9. [Observability](#observability)
10. [Scaling Considerations](#scaling-considerations)
11. [File Changes](#file-changes)
12. [Testing Strategy](#testing-strategy)
13. [Alternatives Considered](#alternatives-considered)
14. [Rollout Plan](#rollout-plan)

## Overview

### Problem Statement
The registry fetches user-supplied URLs in two places without reusing the SSRF guard that already exists for SKILL.md fetches:

- `registry/utils/agent_validator.py::_check_endpoint_reachability` performs `httpx.get(<agent-url>/.well-known/agent-card.json)` during agent-card registration with `verify_endpoint=True`.
- `registry/health/service.py` probes `proxy_pass_url` and derived MCP/SSE endpoints during periodic health checks and immediate health checks.

Both paths can currently be pointed at `169.254.169.254`, `127.0.0.1`, or RFC1918 addresses, allowing SSRF against the host/network.

### Goals
- Promote `registry/services/skill_service.py::_is_safe_url()` into a shared, reusable utility.
- Apply the shared validator to agent-card reachability checks and server health-check fetches.
- Add an operator-configurable allowlist for internal hosts that legitimately need to be reached.
- Preserve existing SKILL.md behavior and all existing registrations that point to public endpoints.

### Non-Goals
- Replacing or removing `github_extra_hosts`; it remains the SKILL.md/GHES-specific trust knob.
- Adding DNS-rebinding or time-of-check/time-of-use defences beyond the current resolver-based check.
- Changing inbound proxy behavior in mcpgw/nginx.
- Validating URLs for IdP/OAuth calls, telemetry, webhooks, or federation (those are operator-configured, not user-supplied).
- Validating the direct "fetch live tools" endpoint in `registry/api/server_routes.py` (it uses the same user-supplied `proxy_pass_url` but is outside the agent-card/health-check scope; handled as a follow-up).

## Codebase Analysis

### Key Files Reviewed

| File/Directory | Purpose | Relevance to This Change |
|----------------|---------|--------------------------|
| `registry/services/skill_service.py` | SKILL.md validation/fetching | Contains `_is_safe_url()` (lines 128-192) and all current SSRF usage; logic will be moved out. |
| `registry/utils/agent_validator.py` | A2A agent-card validation | `_check_endpoint_reachability()` (lines 196-230) fetches `/.well-known/agent-card.json` without SSRF checks. Called from `registry/api/agent_routes.py` with `verify_endpoint=True` on register and `verify_endpoint=False` on update/patch. |
| `registry/health/service.py` | Periodic health checks | `_check_server_endpoint_transport_aware()` and `_perform_health_checks()` make outbound HTTP calls to `proxy_pass_url` and derived endpoints. `_update_tools_background()` calls MCP client only after a healthy check. |
| `registry/core/endpoint_utils.py` | MCP/SSE endpoint URL resolution | Used by health service to derive `/mcp` and `/sse` URLs from `proxy_pass_url`. |
| `registry/core/config.py` | Pydantic settings | Location for new `outbound_url_allowlist` setting. `github_extra_hosts` already defined at lines 292-299. |
| `registry/exceptions.py` | Domain exceptions | Existing `SkillContentSSRFError`/`SkillUrlValidationError`; no agent/health-specific SSRF exception yet. |
| `tests/unit/services/test_skill_service_ssrf_allowlist.py` | SSRF allowlist tests for skills | Pattern for mocking settings and clearing `_trusted_domains` lru_cache. |
| `tests/unit/health/test_health_service.py` | Health service tests | Mocks `httpx.AsyncClient` directly; good home for new SSRF health-check tests. |
| `tests/unit/api/test_agent_routes.py` | Agent route tests | Mocks `agent_validator.validate_agent_card`; new SSRF tests can live in validator tests instead. |

### Existing Patterns Identified

1. **SSRF validation pattern in skill fetching**
   - File: `registry/services/skill_service.py`
   - Pattern: call `_is_safe_url(url)` before the fetch, then call `_is_safe_url(final_url)` after redirects.
   - Future implementer should preserve redirect re-validation and move the helper to a shared module rather than duplicating it.

2. **Settings-driven allowlist**
   - File: `registry/services/skill_service.py` (lines 81-91), `registry/core/config.py` (lines 292-299)
   - Pattern: `_trusted_domains()` is an `@lru_cache(maxsize=1)` function that merges built-in defaults with `settings.github_extra_hosts`.
   - Future implementer should keep the cache for immutable process-wide settings, but make the helper accept an optional extra-hosts string so it can be reused for non-GitHub allowlists.

3. **Validation-result propagation for agents**
   - File: `registry/utils/agent_validator.py`, `registry/api/agent_routes.py`
   - Pattern: `validate_agent_card()` returns `ValidationResult(is_valid, errors, warnings)`. The route raises `HTTPException(422)` on `is_valid=False` and returns warnings in the response.
   - Future implementer should treat an unsafe agent URL as a validation error (not a warning) so registration is rejected.

4. **Health-check status propagation**
   - File: `registry/health/service.py`
   - Pattern: `_check_server_endpoint_transport_aware()` returns `(is_healthy: bool, status_detail: str)`. Unsafe URLs should return `(False, "unhealthy: URL blocked by SSRF policy")` and be logged.

5. **Deployment surface pattern**
   - `.env.example`, `docker-compose*.yml`, `terraform/aws-ecs/variables.tf`, `terraform/aws-ecs/modules/mcp-gateway/variables.tf`, `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` already carry `GITHUB_EXTRA_HOSTS`.
   - Helm registry chart currently does not render GitHub settings; new allowlist should be added to `charts/registry/values.yaml` and `charts/registry/templates/secret.yaml` if the team wants parity.

### Integration Points

| Component | Integration Type | Details |
|-----------|------------------|---------|
| `registry/services/skill_service.py` | Uses shared utility | Imports `is_safe_url` from new shared module; passes `settings.github_extra_hosts` as extra trusted hosts to preserve GHES behavior. |
| `registry/utils/agent_validator.py` | Uses shared utility | Calls `is_safe_url` for `agent_card.url` and the derived well-known URL; passes `settings.outbound_url_allowlist`. |
| `registry/health/service.py` | Uses shared utility | Calls `is_safe_url` for `proxy_pass_url` and each resolved endpoint; passes `settings.outbound_url_allowlist`. |
| `registry/core/config.py` | New setting | Adds `outbound_url_allowlist: str` setting. |
| Deployment configs | New env var | `OUTBOUND_URL_ALLOWLIST` plumbed through `.env.example`, Docker Compose, Terraform, and Helm. |

### Constraints and Limitations Discovered

- **Resolver-based SSRF is not DNS-rebinding-safe**: `_is_safe_url` resolves the hostname once and checks all returned IPs. A determined attacker with DNS TTL control can still bypass this in some environments. This design keeps the same limitation as the existing control.
- **Trusted-domains cache is process-global**: The `@lru_cache(maxsize=1)` cache must be keyed only on settings that are immutable per process. Adding a per-call allowlist means the cache can stay for the default allowlist, but per-call extra hosts must not be cached unless stable.
- **MCP client library does not expose raw httpx**: `_update_tools_background()` uses `mcp.client.streamable_http`/`sse_client`, so redirect re-validation inside the library is not possible. The design mitigates this by validating `proxy_pass_url` before the health check that eventually triggers tool fetching.
- **Helm registry chart does not currently render `GITHUB_EXTRA_HOSTS`**: Adding `OUTBOUND_URL_ALLOWLIST` to Helm requires touching `charts/registry/values.yaml`, `charts/registry/templates/secret.yaml`, and possibly `charts/registry/reserved-env-names.txt`.

## Architecture

### System Context Diagram

```
                                    +--------------------+
                                    |   Operator config  |
                                    | (OUTBOUND_URL_     |
                                    |  ALLOWLIST)        |
                                    +---------+----------+
                                              |
                                              v
+------------+     register/update     +------+-------+     verify/reachability    +------------------+
| API client | --------------------->  | agent_routes | ------------------------> | agent_validator  |
+------------+                         +--------------+                            +---------+--------+
                                                                                           |
                                                                                           v
+------------+     register/update     +--------------+     health probes     +----------+---------+
| Downstream | --------------------->  | server_routes|---------------------->| health/service     |
| teams      |                         +--------------+                     +----------+---------+
+------------+                                                                    |
                                                                                  v
+------------+     fetch SKILL.md     +--------------+                     +------+------+
| Skill repo | ---------------------> | skill_service|<--------------------| ssrf        |
+------------+                        +--------------+    is_safe_url()    | (shared)    |
                                                                           +-------------+
```

### Sequence Diagram: Agent-card registration with SSRF guard

```
Client -> agent_routes: POST /api/agents/register {url: http://10.0.0.1/agent}
agent_routes -> agent_validator: validate_agent_card(card, verify_endpoint=True)
agent_validator -> ssrf: is_safe_url(card.url, extra=settings.outbound_url_allowlist)
ssrf --> agent_validator: False
agent_validator --> agent_routes: ValidationResult(is_valid=False, errors=["Agent URL blocked by SSRF policy"])
agent_routes --> Client: 422 {message, errors}
```

### Sequence Diagram: Health check with SSRF guard

```
health_service -> health_service: _perform_health_checks()
health_service -> health_service: _check_single_service(client, path, server_info)
health_service -> ssrf: is_safe_url(proxy_pass_url, extra=settings.outbound_url_allowlist)
ssrf --> health_service: False
health_service -> health_service: server_health_status[path] = "unhealthy: URL blocked by SSRF policy"
health_service -> nginx_reload_scheduler: mark_dirty() (if status changed)
```

## Data Models

### New Models
No new Pydantic models are required. A new lightweight exception is recommended for clarity:

```python
# registry/exceptions.py
class UnsafeUrlError(RegistryError):
    """Raised when a URL fails the SSRF safety check."""

    def __init__(self, url: str, reason: str = "URL failed SSRF validation") -> None:
        self.url = url
        self.reason = reason
        super().__init__(reason)
```

### Model Changes
No schema changes. `AgentCard.url` and server `proxy_pass_url` remain plain strings.

## API / CLI Design

### New Endpoints / Commands
No new endpoints or CLI commands.

### Changed Behavior

**Agent registration (POST /api/agents/register)**
- If `agent_card.url` resolves to a private/loopback/link-local IP and the host is not in the allowlist, the request returns `422 Unprocessable Entity` with `{"message": "Agent card validation failed", "errors": ["Agent URL blocked by SSRF policy: <url>"], "warnings": []}`.
- If reachability is enabled (`verify_endpoint=True`) and the well-known URL is unsafe, the registration is still rejected with the same shape.

**Agent update/patch (PUT/PATCH /api/agents/{path})**
- Same validation as registration, but `verify_endpoint=False`, so only the `agent_card.url` itself is checked.

**Server health checks**
- If `proxy_pass_url` or any derived endpoint is unsafe, the service is marked `unhealthy: URL blocked by SSRF policy` and no outbound request is made.

## Configuration Parameters

### New Environment Variables

| Variable Name | Type | Default | Required | Description |
|---------------|------|---------|----------|-------------|
| `OUTBOUND_URL_ALLOWLIST` | string | `""` | No | Comma-separated hostnames that bypass the private-IP SSRF check for agent-card and health-check outbound fetches. Example: `internal-tools.example.com,api.corp.local`. |

### Settings / Config Class Updates

```python
# registry/core/config.py, in class Settings
outbound_url_allowlist: str = Field(
    default="",
    description=(
        "Comma-separated hostnames allowed for outbound fetches initiated by "
        "user registrations (agent-card reachability and MCP server health checks). "
        "Hosts here bypass the private-IP SSRF check. Keep the list tight."
    ),
)
```

### Deployment Surface Checklist

- [ ] `.env.example`: add commented `OUTBOUND_URL_ALLOWLIST=` block near `GITHUB_EXTRA_HOSTS`.
- [ ] `docker-compose.yml`: add `OUTBOUND_URL_ALLOWLIST=${OUTBOUND_URL_ALLOWLIST:-}` to registry service.
- [ ] `docker-compose.prebuilt.yml`: same.
- [ ] `docker-compose.podman.yml`: same.
- [ ] `terraform/aws-ecs/variables.tf`: add `variable "outbound_url_allowlist" { default = "" }`.
- [ ] `terraform/aws-ecs/modules/mcp-gateway/variables.tf`: add same variable.
- [ ] `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf`: add env entry for registry container.
- [ ] `terraform/aws-ecs/main.tf`: pass `outbound_url_allowlist = var.outbound_url_allowlist`.
- [ ] `charts/registry/values.yaml`: add `app.outboundUrlAllowlist: ""`.
- [ ] `charts/registry/templates/secret.yaml`: render `OUTBOUND_URL_ALLOWLIST` when non-empty.
- [ ] `charts/registry/reserved-env-names.txt`: add `OUTBOUND_URL_ALLOWLIST`.
- [ ] `charts/mcp-gateway-registry-stack/values.yaml`: add `registry.app.outboundUrlAllowlist` override if desired.

## New Dependencies

This change uses only existing dependencies (`httpx`, Pydantic settings).

## Implementation Details

### Step-by-Step Plan

#### Step 1: Create shared SSRF utility
**File:** `registry/utils/ssrf.py` (new file)
**Lines:** new file

```python
"""Shared SSRF protection utilities."""

import ipaddress
import logging
import socket
from functools import lru_cache
from urllib.parse import urlparse

from registry.core.config import settings

logger = logging.getLogger(__name__)

# Built-in trusted domains for outbound fetches. These are public code-hosting
# platforms; resolving them to private IPs is extremely unlikely.
_DEFAULT_TRUSTED_DOMAINS: frozenset[str] = frozenset(
    {
        "github.com",
        "gitlab.com",
        "raw.githubusercontent.com",
        "bitbucket.org",
    }
)


@lru_cache(maxsize=1)
def _default_trusted_domains() -> frozenset[str]:
    """Return the built-in allowlist.

    Kept as a cached function so tests can clear it if the default set ever
    becomes dynamic.
    """
    return _DEFAULT_TRUSTED_DOMAINS


def _parse_extra_hosts(extra_hosts: str | None) -> frozenset[str]:
    """Normalize a comma-separated hostname string into a set."""
    if not extra_hosts:
        return frozenset()
    return frozenset(h.strip().lower() for h in extra_hosts.split(",") if h.strip())


def _trusted_domains(extra_hosts: str | None = None) -> frozenset[str]:
    """Return merged allowlist: defaults plus caller-supplied extra hosts.

    Args:
        extra_hosts: Optional comma-separated hostnames to trust in addition
            to the built-in defaults. This argument is intentionally NOT cached
            so callers with different allowlists see correct results.
    """
    extras = _parse_extra_hosts(extra_hosts)
    return _default_trusted_domains() | extras


def _is_private_ip(ip_str: str) -> bool:
    """Return True if ip_str is private, loopback, link-local, or reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
        )
    except ValueError:
        # Unparseable IP is treated as unsafe.
        return True


def is_safe_url(
    url: str,
    extra_hosts: str | None = None,
) -> bool:
    """Check if a URL is safe to fetch (SSRF protection).

    Reuses the algorithm originally implemented in skill_service._is_safe_url:

    1. Scheme must be http or https.
    2. URL must have a hostname.
    3. Hostnames in the allowlist skip IP validation.
    4. All resolved IP addresses must be public/non-special.

    Args:
        url: URL to validate.
        extra_hosts: Comma-separated hostnames to add to the allowlist.

    Returns:
        True if the URL is safe to fetch, False otherwise.
    """
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            logger.warning(f"SSRF protection: Blocked URL with scheme '{parsed.scheme}'")
            return False

        hostname = parsed.hostname
        if not hostname:
            logger.warning("SSRF protection: URL has no hostname")
            return False

        hostname_lower = hostname.lower()
        if hostname_lower in _trusted_domains(extra_hosts):
            logger.debug(f"SSRF protection: Trusted domain '{hostname_lower}'")
            return True

        try:
            addr_info = socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as e:
            logger.warning(f"SSRF protection: Failed to resolve hostname '{hostname}': {e}")
            return False

        for family, socktype, proto, canonname, sockaddr in addr_info:
            ip_address = sockaddr[0]
            if _is_private_ip(ip_address):
                logger.warning(
                    f"SSRF protection: Blocked URL resolving to private IP "
                    f"'{ip_address}' for hostname '{hostname}'"
                )
                return False

        return True

    except Exception as e:
        logger.warning(f"SSRF protection: Error validating URL: {e}")
        return False
```

#### Step 2: Replace private helper in skill_service.py
**File:** `registry/services/skill_service.py`
**Lines:** 60-192 (remove), top of file (update imports)

Remove `_DEFAULT_TRUSTED_DOMAINS`, `_trusted_domains()`, `_is_private_ip()`, and `_is_safe_url()`.

Add import:

```python
from ..utils.ssrf import is_safe_url
```

Replace all existing calls to `_is_safe_url(url)` with:

```python
is_safe_url(url, extra_hosts=settings.github_extra_hosts)
```

This preserves the existing GHES bypass semantics exactly.

Update tests that patch `registry.services.skill_service._is_safe_url` to patch `registry.utils.ssrf.is_safe_url` instead. The existing private name is removed so tests must import from the new shared location.

#### Step 3: Add SSRF check to agent card validation
**File:** `registry/utils/agent_validator.py`
**Lines:** 196-230 and 233-290

Add import:

```python
from registry.utils.ssrf import is_safe_url
from registry.core.config import settings
```

Update `_validate_agent_card()` to reject unsafe agent URLs:

```python
def _validate_agent_card(
    agent_card: AgentCard,
) -> tuple[bool, list[str]]:
    errors: list[str] = []

    # ... existing name/description/path checks ...

    url_str = str(agent_card.url)
    if not _validate_agent_url(url_str):
        errors.append("Agent URL must be HTTP or HTTPS and properly formatted")
    elif not is_safe_url(url_str, extra_hosts=settings.outbound_url_allowlist):
        errors.append(
            f"Agent URL blocked by SSRF policy: {url_str}. "
            f"If this host is legitimate, ask the operator to add it to OUTBOUND_URL_ALLOWLIST."
        )

    # ... rest of validation ...
```

Update `_check_endpoint_reachability()`:

```python
def _check_endpoint_reachability(
    url: str,
) -> tuple[bool, str | None]:
    well_known_url = f"{url}/.well-known/agent-card.json"

    if not is_safe_url(well_known_url, extra_hosts=settings.outbound_url_allowlist):
        logger.warning(f"SSRF protection: Blocked agent-card reachability check for {well_known_url}")
        return (False, "Agent endpoint URL blocked by SSRF policy")

    try:
        response = httpx.get(well_known_url, timeout=5.0)
        # ... existing status handling ...
    except httpx.TimeoutException:
        # ... existing ...
```

#### Step 4: Add SSRF check to health service
**File:** `registry/health/service.py`
**Lines:** 410-485 and 674-958

Add import:

```python
from registry.utils.ssrf import is_safe_url
from registry.core.config import settings
```

Add a small helper:

```python
def _is_safe_health_url(self, url: str) -> bool:
    return is_safe_url(url, extra_hosts=settings.outbound_url_allowlist)
```

In `_check_server_endpoint_transport_aware()`:

```python
if not proxy_pass_url:
    return False, HealthStatus.UNHEALTHY_MISSING_PROXY_URL

if not self._is_safe_health_url(proxy_pass_url):
    logger.warning(f"SSRF protection: Blocked health check proxy URL {proxy_pass_url}")
    return False, "unhealthy: URL blocked by SSRF policy"

# ... existing transport detection ...

# Before each derived endpoint fetch, validate the resolved URL.
endpoint = get_endpoint_url_from_server_info(server_info, transport_type="streamable-http")
if not self._is_safe_health_url(endpoint):
    logger.warning(f"SSRF protection: Blocked health check endpoint {endpoint}")
    return False, "unhealthy: URL blocked by SSRF policy"
```

Apply the same check to `sse_endpoint` and the fallback default endpoint.

In `_check_single_service()`:

```python
proxy_pass_url = server_info.get("proxy_pass_url")
if not proxy_pass_url:
    # ... existing missing URL handling ...
```

No change required here because `_check_server_endpoint_transport_aware()` now validates it.

#### Step 5: Add exception (optional)
**File:** `registry/exceptions.py`
**Lines:** new class after `SkillContentSSRFError`

```python
class UnsafeUrlError(RegistryError):
    """Raised when a URL fails the SSRF safety check."""

    def __init__(self, url: str, reason: str = "URL failed SSRF validation") -> None:
        self.url = url
        self.reason = reason
        super().__init__(reason)
```

This is optional because the current helpers return `bool`. It is added only if future code wants to raise instead of returning False.

#### Step 6: Update configuration and deployment surfaces
**File:** `registry/core/config.py`
**Lines:** after `github_extra_hosts` (around line 299)

```python
outbound_url_allowlist: str = Field(
    default="",
    description=(
        "Comma-separated hostnames allowed for outbound fetches initiated by "
        "user registrations (agent-card reachability and MCP server health checks). "
        "Hosts here bypass the private-IP SSRF check. Keep the list tight."
    ),
)
```

Update deployment files per the checklist in [Configuration Parameters](#configuration-parameters).

### Error Handling

- **Agent validation**: Unsafe URL is added to `ValidationResult.errors`; the route returns `422`. This is consistent with other validation failures.
- **Health checks**: Unsafe URL returns `(False, "unhealthy: URL blocked by SSRF policy")` and logs at WARNING. The service is marked unhealthy; no exception propagates to the background loop.
- **Skill service**: Existing `SkillUrlValidationError` / `SkillContentSSRFError` behavior is unchanged because the helper signature stays compatible.

### Logging

- `WARNING` when a URL is blocked, including the URL (sanitized) and reason (scheme, missing hostname, private IP, resolution failure).
- `DEBUG` when a URL is trusted via allowlist.
- All log messages reuse the existing format from `skill_service.py` for consistency.

## Observability

### Tracing / Metrics / Logging Points

- Log line per blocked URL in `ssrf.is_safe_url`.
- Log line per blocked agent-card reachability check.
- Log line per blocked health-check URL.
- Optional metric (out of scope for initial implementation): counter `outbound_url_blocked_total{reason="private_ip|scheme|resolution|allowlist"}`.

## Scaling Considerations

- The `@lru_cache(maxsize=1)` default allowlist remains; per-call extra hosts are not cached.
- `socket.getaddrinfo` is a blocking syscall. It is already used synchronously in the existing skill check; this design keeps the same behavior. If health-check volume becomes very high, consider running `getaddrinfo` in `asyncio.to_thread`, but that is out of scope for this change.
- No additional DB or network load is introduced; in fact, blocked URLs skip outbound HTTP entirely.

## File Changes

### New Files

| File Path | Description |
|-----------|-------------|
| `registry/utils/ssrf.py` | Shared SSRF validator (`is_safe_url`, `_is_private_ip`, `_trusted_domains`). |

### Modified Files

| File Path | Lines | Change Description |
|-----------|-------|--------------------|
| `registry/services/skill_service.py` | ~60-192 | Remove private `_is_safe_url` family; import from shared utility; preserve behavior via `extra_hosts=settings.github_extra_hosts`. |
| `registry/utils/agent_validator.py` | ~41-70, 196-230, 259 | Add `is_safe_url` import; validate `agent_card.url` and well-known URL. |
| `registry/health/service.py` | ~674-958 | Validate `proxy_pass_url` and derived endpoints before fetching. |
| `registry/core/config.py` | ~299 | Add `outbound_url_allowlist` setting. |
| `registry/exceptions.py` | ~194 | Add `UnsafeUrlError` (optional). |
| `.env.example` | ~623 | Add `OUTBOUND_URL_ALLOWLIST` documentation. |
| `docker-compose.yml` | ~195 | Pass through `OUTBOUND_URL_ALLOWLIST`. |
| `docker-compose.prebuilt.yml` | ~102 | Pass through `OUTBOUND_URL_ALLOWLIST`. |
| `docker-compose.podman.yml` | ~92 | Pass through `OUTBOUND_URL_ALLOWLIST`. |
| `terraform/aws-ecs/variables.tf` | ~1327 | Add `outbound_url_allowlist` variable. |
| `terraform/aws-ecs/modules/mcp-gateway/variables.tf` | ~1280 | Add `outbound_url_allowlist` variable. |
| `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` | ~1266 | Add `OUTBOUND_URL_ALLOWLIST` env entry. |
| `terraform/aws-ecs/main.tf` | ~298 | Pass variable to module. |
| `charts/registry/values.yaml` | ~75 | Add `app.outboundUrlAllowlist`. |
| `charts/registry/templates/secret.yaml` | TBD | Render `OUTBOUND_URL_ALLOWLIST`. |
| `charts/registry/reserved-env-names.txt` | TBD | Add `OUTBOUND_URL_ALLOWLIST`. |
| `tests/unit/services/test_skill_service_ssrf_allowlist.py` | All | Update imports to point at shared utility; add tests for generic allowlist. |
| `tests/unit/utils/test_ssrf.py` | New | Unit tests for `is_safe_url`. |
| `tests/unit/utils/test_agent_validator.py` or similar | New/updated | Tests for agent-card URL SSRF blocking. |
| `tests/unit/health/test_health_service.py` | New | Tests for health-check SSRF blocking. |

### Estimated Lines of Code

| Category | Lines |
|----------|-------|
| New code | ~250 |
| New tests | ~300 |
| Modified code | ~150 |
| **Total** | **~700** |

## Testing Strategy

The full executable plan lives in `testing.md`. Summary:

- Unit-test the shared validator: private-IP blocking, allowlist bypass, scheme validation, missing hostname, cloud metadata IP.
- Unit-test agent validator: unsafe URL rejected as validation error; safe URL allowed; allowlisted unsafe URL allowed.
- Unit-test health service: unsafe `proxy_pass_url` returns SSRF unhealthy status; derived endpoint (`/mcp`, `/sse`) unsafe also blocked.
- Backwards-compatibility tests: existing skill_service tests still pass; existing registrations to public IPs still work.
- Deployment surface tests: render Helm/Terraform and assert `OUTBOUND_URL_ALLOWLIST` is present.

## Alternatives Considered

### Alternative 1: Reuse `github_extra_hosts` for all outbound fetches
**Description:** Have the shared validator always merge `settings.github_extra_hosts` into the allowlist, and apply it to agent/health checks.
**Pros / Cons:** One less setting; minimal config surface. / Confusing semantics - `github_extra_hosts` also controls GitHub auth header injection and is documented for SKILL.md only.
**Why Rejected:** The name is misleading for agent/health use cases. A separate `outbound_url_allowlist` is clearer and avoids accidentally widening auth header injection.

### Alternative 2: Leave `_is_safe_url` in skill_service.py and import it from there
**Description:** `agent_validator.py` and `health/service.py` import `_is_safe_url` from `registry.services.skill_service`.
**Pros / Cons:** Smaller diff. / Creates a confusing dependency where agent and health code import from a skill-specific module; violates separation of concerns.
**Why Rejected:** A shared utility under `registry/utils` is the conventional location for cross-cutting helpers.

### Alternative 3: Validate only `proxy_pass_url` and not derived endpoints
**Description:** Check only the base `proxy_pass_url` and trust endpoint derivation.
**Pros / Cons:** Simpler. / An attacker could register `http://evil.com` (public) with an explicit `mcp_endpoint` of `http://169.254.169.254/mcp` and bypass the check.
**Why Rejected:** Defence in depth requires checking every URL that will actually be fetched, including explicit `mcp_endpoint`/`sse_endpoint`.

### Comparison Matrix

| Criteria | Chosen (shared utility + new allowlist) | Alt 1 (reuse github_extra_hosts) | Alt 2 (import from skill_service) |
|----------|----------------------------------------|----------------------------------|-----------------------------------|
| Clarity | High | Medium | Low |
| Backwards compatibility | High | High | High |
| Separation of concerns | High | High | Low |
| Config surface | One new env var | None | None |
| Implementation risk | Low | Low | Very low |

## Rollout Plan

- Phase 1: Implementation (out of scope for this skill)
- Phase 2: Run unit tests for `ssrf`, `skill_service`, `agent_validator`, `health/service`
- Phase 3: Run Helm unittest and Terraform validation
- Phase 4: Deploy to staging; verify existing agents/servers remain healthy
- Phase 5: Production rollout with monitoring for blocked-URL log lines

## Open Questions

- Should the shared validator also reject URLs with username/password embedded in the authority (e.g. `http://user:pass@host`) as an additional hardening measure? The current helper does not.
- Should the direct "fetch live tools" endpoint in `server_routes.py` be included in a follow-up issue, or is the current agent-card/health-check scope sufficient for the audit finding?

## References

- Existing SSRF implementation: `registry/services/skill_service.py` lines 128-192
- Agent-card reachability: `registry/utils/agent_validator.py` lines 196-230
- Health-check outbound calls: `registry/health/service.py` lines 674-958
- Existing SSRF tests: `tests/unit/services/test_skill_service_ssrf_allowlist.py`
