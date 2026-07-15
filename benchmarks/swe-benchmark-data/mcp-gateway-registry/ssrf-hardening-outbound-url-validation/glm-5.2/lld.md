# Low-Level Design: SSRF Hardening - Outbound URL Validation

*Created: 2026-07-15*
*Author: Claude (glm-5.2)*
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
A security audit found that the registry performs server-side fetches of registrant-supplied URLs on two paths that have no SSRF guard:

1. **Agent-card reachability** - `registry/utils/agent_validator.py::_check_endpoint_reachability` issues `httpx.get(f"{url}/.well-known/agent-card.json")` against an A2A agent URL supplied by the registrant. The only existing check, `_validate_agent_url`, validates scheme and format but does NOT resolve the host or block private/internal IPs.
2. **Server health checks** - `registry/health/service.py::_check_server_endpoint_transport_aware` (and the helpers it calls) fetch `proxy_pass_url` and derived endpoint URLs stored on each registered MCP server. These URLs originate from server registration (registrant- or operator-supplied) and are fetched on a recurring schedule with `follow_redirects=True` and no IP validation.

A working SSRF guard already exists - `_is_safe_url()` / `_is_private_ip()` / `_trusted_domains()` in `registry/services/skill_service.py` (used 8 times for skill fetches, with post-redirect re-validation) - but it is private to the skill service and not reused.

### Goals
- Promote the existing SSRF guard into a single shared, well-tested utility under `registry/utils/`.
- Apply the shared guard to the agent-card reachability fetch and the server health-check fetches, including post-redirect re-validation.
- Add one configuration knob (`OUTBOUND_URL_ALLOWLIST`) so operators can extend the trusted-host allowlist across all outbound fetch paths.
- Preserve the existing `github_extra_hosts` behaviour for skill fetches exactly (backwards compatibility).
- Keep the change stdlib-only: no new runtime dependencies.

### Non-Goals
- Central egress proxy, network policy, or SOCKS/HTTP-proxy rewrite.
- Changing how/where server URLs are stored.
- Adding SSRF validation to operator-configured trusted endpoints (`registry/auth/routes.py` OAuth callbacks, `registry/utils/okta_manager.py`, `entra_manager.py`, `auth0_manager.py`, `keycloak_manager.py`). These are operator-configured, not registrant-supplied. Noted as future work.
- Connection-time DNS pinning (the guard validates pre-fetch and post-redirect only, matching the existing proven pattern).

## Codebase Analysis

### Key Files Reviewed

| File/Directory | Purpose | Relevance to This Change |
|----------------|---------|--------------------------|
| `registry/services/skill_service.py` | Skill service; owns the existing SSRF guard (`_is_safe_url` line 128, `_is_private_ip` line 94, `_trusted_domains` line 82, `_DEFAULT_TRUSTED_DOMAINS` line 71) | Source of the guard to promote; must be refactored to import the shared utility |
| `registry/utils/agent_validator.py` | A2A agent card validation; `_check_endpoint_reachability` (line 196) does an unguarded `httpx.get`; `_validate_agent_url` (line 41) is format-only | Vulnerable path 1; needs the shared guard before the fetch |
| `registry/health/service.py` | Recurring server health checks; `_check_server_endpoint_transport_aware` (line 674), `_initialize_mcp_session` (line 561), `_try_ping_without_auth` (line 627) fetch registrant-supplied URLs | Vulnerable path 2; needs the shared guard on `proxy_pass_url` and derived `endpoint` |
| `registry/core/endpoint_utils.py` | `get_endpoint_url_from_server_info` / `get_endpoint_url` resolve the actual endpoint URL from `proxy_pass_url` | Used by health service to derive `endpoint`; the derived URL must also be validated |
| `registry/services/server_service.py` | `register_server` (line 49) stores `proxy_pass_url` from the registration request | Confirms the URL is registrant-supplied and later fetched by the health checker |
| `registry/core/config.py` | `Settings` (Pydantic); `github_extra_hosts` Field at line 292 | Where the new `outbound_url_allowlist` setting is declared, following the same pattern |
| `registry/utils/url_utils.py` | Sibling utility module for skill URL translation | Confirms the `registry/utils/` convention for a new `url_safety.py` module |
| `tests/unit/services/test_skill_service_ssrf_allowlist.py` | Existing SSRF allowlist tests; patches `registry.services.skill_service.settings` and `registry.services.skill_service.socket.getaddrinfo` | Test convention to follow; patch paths must migrate to the new shared module |
| `.env.example`, `docker-compose.yml`, `terraform/aws-ecs/**`, `charts/registry/**` | Deployment surfaces | Every surface a new env var must touch (see Configuration Parameters) |

### Existing Patterns Identified

1. **SSRF guard pattern**: scheme allowlist (`http`/`https`) -> hostname required -> trusted-domain short-circuit -> `socket.getaddrinfo` resolve -> reject if any resolved IP is private/loopback/link-local/reserved or the cloud-metadata IP -> fail-closed on any exception. Files: `registry/services/skill_service.py`. A future implementer should preserve this exact algorithm verbatim when moving it, only relocating it.
2. **Post-redirect re-validation**: the skill service re-runs `_is_safe_url(final_url)` after every redirect (e.g. lines 616, 707, 896, 1071). A future implementer must apply the same re-validation to the agent-card and health-check fetches, since both follow redirects (`httpx.get` defaults; health uses `follow_redirects=True`).
3. **Trusted-domain allowlist derivation**: `_trusted_domains()` is an `lru_cache(maxsize=1)` that merges `_DEFAULT_TRUSTED_DOMAINS` with `settings.github_extra_hosts` (comma-separated, trimmed, lowercased). Files: `skill_service.py`. The shared module should keep this exact derivation and add the new `outbound_url_allowlist` to the merged set.
4. **Settings Field pattern**: `github_extra_hosts: str = Field(default="", description=...)` at `config.py:292`. A future implementer should declare `outbound_url_allowlist` with the same shape.
5. **Health-check failure handling**: a failed health check sets a `HealthStatus` string detail (e.g. `UNHEALTHY_TIMEOUT`, `UNHEALTHY_CONNECTION_ERROR`, or `f"error: {type(e).__name__}"`) rather than raising. A future implementer should represent an SSRF rejection as a new `HealthStatus` value (e.g. `UNHEALTHY_SSRF_BLOCKED`) so the loop continues instead of crashing.
6. **Test patching convention**: tests patch `registry.services.skill_service.settings` and `registry.services.skill_service.socket.getaddrinfo`. After the move, tests must patch `registry.utils.url_safety.settings` and `registry.utils.url_safety.socket.getaddrinfo` instead, and the existing skill-service SSRF test must be updated (or migrated to a new `tests/unit/utils/test_url_safety.py`).

### Integration Points

| Component | Integration Type | Details |
|-----------|------------------|---------|
| `skill_service.py` | Refactored to use shared utility | Replace local `_is_safe_url`/`_is_private_ip`/`_trusted_domains`/`_DEFAULT_TRUSTED_DOMAINS` with imports from `registry.utils.url_safety`; keep call sites unchanged |
| `agent_validator.py::_check_endpoint_reachability` | Extends | Call `is_url_safe_to_fetch(url)` before `httpx.get`; re-validate `str(response.url)` after the fetch (post-redirect) |
| `health/service.py::_check_server_endpoint_transport_aware` | Extends | Validate `proxy_pass_url` before the first request; validate the resolved `endpoint` (from `get_endpoint_url_from_server_info`) before `client.post`; re-validate after redirects where `follow_redirects=True` |
| `registry/core/config.py::Settings` | Extends | Add `outbound_url_allowlist: str` Field |
| Deployment surfaces | Extends | `.env.example`, `docker-compose.yml`, Terraform `variables.tf`/`main.tf`/`terraform.tfvars.example`/`modules/mcp-gateway/{variables.tf,ecs-services.tf}`, Helm `charts/registry/reserved-env-names.txt` (+ values/deployment if rendered) |

### Constraints and Limitations Discovered
- The existing guard resolves DNS once, pre-fetch, and again post-redirect. It does NOT pin the resolved IP for the actual TCP connection (TOCTOU/DNS-rebinding window). This is a known, accepted limitation of the current guard; this change does not widen or narrow it. Documented in Alternatives.
- `httpx.get` (used in `_check_endpoint_reachability`) follows redirects by default and does not expose the final URL until after the call. Post-redirect validation there must inspect `response.url` and treat a redirected-to-unsafe URL as a failure (the body must not be trusted).
- The health service fetches many servers concurrently in batches of 10 (line 360). The guard adds one `getaddrinfo` per URL per check cycle; with caching considerations see Scaling.
- `charts/registry` does not currently render `GITHUB_EXTRA_HOSTS` into the registry deployment (the `mcpgw` chart does). The new `OUTBOUND_URL_ALLOWLIST` should be added to the registry chart's reserved-env-names and, if operators are expected to set it via Helm, wired through `values.yaml` + `deployment.yaml`.

## Architecture

### System Context Diagram

```
            registrant (API request)
                  |
                  v
   +-------------------------------+
   | registry API (server_routes, |  -- stores proxy_pass_url / agent url
   |   agent_routes, skill_routes) |
   +-------------------------------+
                  |
        +---------+---------+
        |                   |
        v                   v
 +-------------+    +-----------------+
 | health      |    | agent_validator |  -- _check_endpoint_reachability
 | service     |    +-----------------+
 | (cron)      |            |
 +-------------+            |
        |                   |
        |  proxy_pass_url   |  agent url
        |  + endpoint       |  + /.well-known/agent-card.json
        v                   v
 +--------------------------------------------------+
 | registry.utils.url_safety  (NEW shared guard)   |
 |   is_url_safe_to_fetch(url) -> bool             |
 |   - scheme allowlist (http/https)               |
 |   - trusted-domain allowlist                    |
 |     = _DEFAULT_TRUSTED_DOMAINS                  |
 |       U github_extra_hosts                      |
 |       U outbound_url_allowlist  (NEW)           |
 |   - DNS resolve + private/loopback/link-local/  |
 |     reserved/metadata IP block                  |
 |   - fail-closed                                 |
 +--------------------------------------------------+
        |  (only if safe)
        v
   httpx.AsyncClient / httpx.get  -> external target
```

### Sequence Diagram

```
Health check (per server, per cycle):

  health loop -> _check_single_service(service_path, server_info)
    proxy_pass_url = server_info["proxy_pass_url"]
    -> is_url_safe_to_fetch(proxy_pass_url)?
         NO -> new_status = UNHEALTHY_SSRF_BLOCKED; log warning; return
         YES -> _check_server_endpoint_transport_aware(client, proxy_pass_url, ...)
                  endpoint = get_endpoint_url_from_server_info(server_info)
                  -> is_url_safe_to_fetch(endpoint)?
                       NO -> return unhealthy (SSRF)
                       YES -> client.get/post(..., follow_redirects=True)
                                 -> on response: is_url_safe_to_fetch(str(response.url))?
                                      NO -> treat unhealthy (redirect to private IP)
                                      YES -> existing health logic

Agent-card reachability:

  validate_agent_card(card, check_reachability=True)
    -> _check_endpoint_reachability(card.url)
         well_known_url = f"{url}/.well-known/agent-card.json"
         -> is_url_safe_to_fetch(well_known_url)?
              NO -> return (False, "SSRF-blocked: ...")
              YES -> httpx.get(well_known_url)
                        -> is_url_safe_to_fetch(str(response.url))?
                             NO -> return (False, "redirected to unsafe host")
                             YES -> existing reachability logic
```

### Component Diagram

```
registry.utils.url_safety (NEW)
  ├── DEFAULT_TRUSTED_DOMAINS  (moved from skill_service)
  ├── trusted_domains()        (lru_cache; merges defaults + github_extra_hosts + outbound_url_allowlist)
  ├── is_private_ip(ip_str)    (moved)
  └── is_url_safe_to_fetch(url)-> bool   (renamed from _is_safe_url; public)

Consumers:
  - registry.services.skill_service        (re-imports; behaviour unchanged)
  - registry.utils.agent_validator         (NEW consumer)
  - registry.health.service                (NEW consumer)
```

## Data Models

### New Models
No new Pydantic domain models are required. The change operates on existing URL strings (`proxy_pass_url`, `agent_card.url`, derived `endpoint`).

### Model Changes
None. `AgentCard.url` and the server-info dict's `proxy_pass_url` are unchanged in shape; they are merely validated before fetch.

A new `HealthStatus` literal is added (see Implementation Details), represented as a string value consistent with the existing `HealthStatus` usage in `health/service.py`.

## API / CLI Design

No new HTTP endpoints or CLI commands are introduced. The change is internal validation applied before existing outbound fetches. Externally observable effects:

- **Agent registration with `check_reachability=True`**: an agent whose URL resolves to a private/internal IP now produces a validation warning `Agent endpoint unreachable: SSRF-blocked: <reason>` instead of a (possibly successful) internal probe. Registration itself is not blocked (reachability is a warning, per existing `validate_agent_card` semantics at line 316-318), but the unsafe endpoint is not fetched.
- **Server health checks**: a server whose `proxy_pass_url` (or derived endpoint, or redirect target) resolves to a private/internal IP is marked `UNHEALTHY_SSRF_BLOCKED` instead of being probed.

**Error Cases:**
- Unsafe URL (private IP / loopback / link-local / reserved / cloud-metadata / non-http scheme): fetch is skipped; health status set to `UNHEALTHY_SSRF_BLOCKED`; agent reachability returns `(False, "SSRF-blocked: ...")`. Logged at WARNING.

## Configuration Parameters

### New Environment Variables

| Variable Name | Type | Default | Required | Description |
|---------------|------|---------|----------|-------------|
| `OUTBOUND_URL_ALLOWLIST` | str (comma-separated hostnames) | `""` | No | Extra hostnames trusted for all outbound URL fetches (agent-card reachability, server health checks, skill fetches). Hosts here bypass the private-IP check. Merged with `GITHUB_EXTRA_HOSTS` and the built-in defaults. Keep the list tight. |

### Settings / Config Class Updates
In `registry/core/config.py`, near the existing `github_extra_hosts` Field (line 292):

```python
outbound_url_allowlist: str = Field(
    default="",
    description=(
        "Comma-separated extra hostnames trusted for all outbound URL fetches "
        "(agent-card reachability, server health checks, SKILL.md fetches). "
        "Hosts here bypass the SSRF private-IP check, e.g. for an internal artifact "
        "host on a private network. Merged with GITHUB_EXTRA_HOSTS and the built-in "
        "defaults (github.com, gitlab.com, raw.githubusercontent.com, bitbucket.org). "
        "Keep the list tight."
    ),
)
```

### Deployment Surface Checklist
A future implementer must add `OUTBOUND_URL_ALLOWLIST` to every surface below (mirroring how `GITHUB_EXTRA_HOSTS` is wired):

- [ ] `registry/core/config.py` - new Field (above)
- [ ] `.env.example` - add `# OUTBOUND_URL_ALLOWLIST=internal-artifacts.mycompany.com` (near line 623 where `GITHUB_EXTRA_HOSTS` is documented)
- [ ] `docker-compose.yml` - add `- OUTBOUND_URL_ALLOWLIST=${OUTBOUND_URL_ALLOWLIST:-}` (near line 195)
- [ ] `terraform/aws-ecs/variables.tf` - new `variable "outbound_url_allowlist"` (near line 1327)
- [ ] `terraform/aws-ecs/main.tf` - pass `outbound_url_allowlist = var.outbound_url_allowlist` (near line 298)
- [ ] `terraform/aws-ecs/terraform.tfvars.example` - commented example (near line 936)
- [ ] `terraform/aws-ecs/modules/mcp-gateway/variables.tf` - new variable (near line 1280)
- [ ] `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` - new env var `name = "OUTBOUND_URL_ALLOWLIST"`, `value = var.outbound_url_allowlist` (near line 1266)
- [ ] `charts/registry/reserved-env-names.txt` - add `OUTBOUND_URL_ALLOWLIST` (so it cannot be overridden via `extraEnv`)
- [ ] `charts/registry/values.yaml` + `charts/registry/templates/deployment.yaml` - render the env var if Helm operators are expected to set it (match the existing env-var rendering pattern; note `GITHUB_EXTRA_HOSTS` is currently rendered in the `mcpgw` chart, so decide whether the registry chart should render it too)
- [ ] Operations docs under `docs/operations/` - document the new knob and its security implications

## New Dependencies

This change uses only existing dependencies. No new packages are added.

| Package | Version | Purpose |
|---------|---------|---------|
| (none) | - | `ipaddress`, `socket`, `urllib.parse` are stdlib; `httpx` is already a dependency |

## Implementation Details

### Step-by-Step Plan (for a future implementer)

#### Step 1: Create the shared utility module
**File:** `registry/utils/url_safety.py` (new file)

Move the three functions and the constant out of `skill_service.py` verbatim, renaming `_is_safe_url` to a public `is_url_safe_to_fetch` (keep a thin private alias if desired for the skill-service migration). Extend `trusted_domains()` to also merge `settings.outbound_url_allowlist`.

```python
"""Shared SSRF guard for all outbound URL fetches.

Promoted from registry.services.skill_service so agent-card reachability
and server health checks reuse the same scheme/IP/allowlist validation.
Algorithm is unchanged; only relocated and made reusable.
"""

import ipaddress
import logging
import socket
from functools import lru_cache
from urllib.parse import urlparse

from ..core.config import settings

logger = logging.getLogger(__name__)

# Built-in trusted domains that skip IP validation (SSRF protection allowlist).
DEFAULT_TRUSTED_DOMAINS: frozenset = frozenset(
    {
        "github.com",
        "gitlab.com",
        "raw.githubusercontent.com",
        "bitbucket.org",
    }
)


@lru_cache(maxsize=1)
def trusted_domains() -> frozenset[str]:
    """Return the SSRF allowlist: defaults + github_extra_hosts + outbound_url_allowlist.

    Reads settings.github_extra_hosts (GHES hosts that also receive GitHub auth
    headers) and settings.outbound_url_allowlist (operator-trusted outbound hosts
    such as an internal artifact server). Cached because settings are immutable
    per-process.
    """
    hosts: set[str] = set(DEFAULT_TRUSTED_DOMAINS)
    for raw in (settings.github_extra_hosts, settings.outbound_url_allowlist):
        if raw:
            hosts.update(h.strip().lower() for h in raw.split(",") if h.strip())
    return frozenset(hosts)


def is_private_ip(
    ip_str: str,
) -> bool:
    """Return True if the IP is private/loopback/link-local/reserved or the metadata endpoint."""
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
        if ip_str == "169.254.169.254":
            return True
        return False
    except ValueError:
        return True


def is_url_safe_to_fetch(
    url: str,
) -> bool:
    """Check if a URL is safe to fetch (SSRF protection).

    Validates that a URL:
    1. Uses http or https scheme
    2. Has a hostname
    3. Does not resolve to a private/loopback/link-local/reserved IP
    4. Does not target the cloud metadata endpoint

    Trusted domains (the built-in defaults plus settings.github_extra_hosts and
    settings.outbound_url_allowlist) skip the IP check.
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
        if hostname_lower in trusted_domains():
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
            if is_private_ip(ip_address):
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

#### Step 2: Refactor skill_service to import the shared utility
**File:** `registry/services/skill_service.py`
**Lines:** 67-192 (the constant + three functions)

Remove `_DEFAULT_TRUSTED_DOMAINS`, `_trusted_domains`, `_is_private_ip`, `_is_safe_url` from this file and import them from the new module. Preserve backwards-compatible aliases so existing internal call sites (lines 595, 616, 681, 707, 866, 896, 1042, 1071) keep working:

```python
from ..utils.url_safety import (
    DEFAULT_TRUSTED_DOMAINS as _DEFAULT_TRUSTED_DOMAINS,
    is_private_ip as _is_private_ip,
    is_url_safe_to_fetch as _is_safe_url,
    trusted_domains as _trusted_domains,
)
```

This keeps the 8 skill-service call sites and the existing test patch paths... NOTE: the existing test `tests/unit/services/test_skill_service_ssrf_allowlist.py` patches `registry.services.skill_service.settings` and `registry.services.skill_service.socket`. After the move, those attributes no longer live in `skill_service`, so the test MUST be updated to patch `registry.utils.url_safety.settings` and `registry.utils.url_safety.socket` (see Step 6).

#### Step 3: Add the new Settings field
**File:** `registry/core/config.py`
**Lines:** near 299 (after `github_extra_hosts`)

Add the `outbound_url_allowlist` Field as shown in Configuration Parameters.

#### Step 4: Harden the agent-card reachability check
**File:** `registry/utils/agent_validator.py`
**Lines:** 196-230 (`_check_endpoint_reachability`)

Validate before the fetch and re-validate the final (post-redirect) URL:

```python
def _check_endpoint_reachability(
    url: str,
) -> tuple[bool, str | None]:
    """Check if agent endpoint is reachable (SSRF-guarded)."""
    try:
        from ..utils.url_safety import is_url_safe_to_fetch

        well_known_url = f"{url}/.well-known/agent-card.json"

        if not is_url_safe_to_fetch(well_known_url):
            logger.warning(f"SSRF protection: blocked agent endpoint reachability check for {url}")
            return (False, "Endpoint URL blocked by SSRF protection")

        response = httpx.get(well_known_url, timeout=5.0)

        # Re-validate the final URL after any redirect.
        if str(response.url) != well_known_url and not is_url_safe_to_fetch(str(response.url)):
            logger.warning(
                f"SSRF protection: agent endpoint {url} redirected to unsafe URL {response.url}"
            )
            return (False, "Endpoint redirected to an unsafe URL")

        if response.status_code == 200:
            return (True, None)
        return (False, f"Endpoint returned status {response.status_code}")

    except httpx.TimeoutException:
        logger.warning(f"Endpoint timeout for {url}")
        return (False, "Endpoint request timed out")
    except Exception as e:
        logger.warning(f"Could not reach endpoint {url}: {e}")
        return (False, str(e))
```

Note: reachability is a warning in `validate_agent_card` (lines 313-318), so an SSRF block surfaces as an "unreachable" warning without aborting registration. This preserves backwards compatibility for valid public-URL registrations.

#### Step 5: Harden the server health-check fetches
**File:** `registry/health/service.py`
**Lines:** 429-484 (`_check_single_service`) and 674-800 (`_check_server_endpoint_transport_aware`)

Add a module-level `HealthStatus` value (follow the existing `HealthStatus` convention in this file):

```python
UNHEALTHY_SSRF_BLOCKED = "unhealthy-ssrf-blocked"
```

In `_check_single_service`, validate `proxy_pass_url` before delegating:

```python
from ..utils.url_safety import is_url_safe_to_fetch

proxy_pass_url = server_info.get("proxy_pass_url")
if not proxy_pass_url:
    ...  # existing behaviour
elif not is_url_safe_to_fetch(proxy_pass_url):
    new_status = HealthStatus.UNHEALTHY_SSRF_BLOCKED
    self.server_health_status[service_path] = new_status
    self.server_last_check_time[service_path] = datetime.now(UTC)
    logger.warning(
        f"SSRF protection: blocked health check for {service_path} "
        f"(proxy_pass_url resolves to a private/unsafe target)"
    )
    return previous_status != new_status
```

In `_check_server_endpoint_transport_aware`, validate the derived `endpoint` before any `client.get`/`client.post`, and re-validate `str(response.url)` after any `follow_redirects=True` call (lines 710, 736). For `_initialize_mcp_session` (line 592) and `_try_ping_without_auth` (line 650), validate `endpoint` at the entry of each helper (or at the single call site that computes it) and return an unhealthy result on block. A helper keeps this DRY:

```python
def _is_endpoint_safe(self, endpoint: str) -> bool:
    if not endpoint:
        return False
    return is_url_safe_to_fetch(endpoint)
```

Where redirects are followed, add after the response:

```python
if not is_url_safe_to_fetch(str(response.url)):
    logger.warning(f"SSRF protection: health check for {proxy_pass_url} redirected to unsafe {response.url}")
    return False, HealthStatus.UNHEALTHY_SSRF_BLOCKED
```

#### Step 6: Update existing tests and add new ones
**File:** `tests/unit/services/test_skill_service_ssrf_allowlist.py` (modify) and `tests/unit/utils/test_url_safety.py` (new)

Migrate the existing test's patch targets from `registry.services.skill_service.settings` / `.socket` to `registry.utils.url_safety.settings` / `.socket`, and from `skill_service._trusted_domains` / `_is_safe_url` to `url_safety.trusted_domains` / `is_url_safe_to_fetch`. Add new tests for the `outbound_url_allowlist` merge and for the agent-card and health-check call sites (see Testing Strategy).

### Error Handling
- The guard fails closed: any exception during parse/resolve returns `False` (safe = do not fetch). This matches the existing behaviour.
- Health checks treat a blocked URL as an unhealthy status string, not an exception, so the batch loop (`asyncio.gather(..., return_exceptions=True)` at line 383) is unaffected.
- Agent reachability treats a blocked URL as `(False, reason)`, matching the existing tuple contract.

### Logging
- `WARNING` for every block: scheme, no-hostname, private-IP resolution, redirect-to-unsafe, metadata endpoint. Include the hostname and (for redirects) the final URL. Never log credentials or full request headers.
- `DEBUG` for trusted-domain short-circuit.
- `INFO` for the first-check / transition logs is unchanged.

## Observability

### Tracing / Metrics / Logging Points
- Logs (existing `logger` in each module) at WARNING on every SSRF block, with hostname and reason. This gives operators a signal to investigate misconfigured or malicious registrations.
- Consider a counter metric (optional, follow the existing metrics convention in `registry/metrics/`): `ssrf_blocks_total{path="health|agent_card|skill"}`. Not required for the security fix but recommended for production visibility.
- The `UNHEALTHY_SSRF_BLOCKED` health status is already surfaced through the existing health-status broadcast and UI, so operators see blocked servers as unhealthy with a clear reason.

## Scaling Considerations
- **Current load**: health checks run on a configured interval over all enabled services in batches of 10 (line 360). Each check now adds one `getaddrinfo` per unique hostname per cycle.
- **Caching**: `getaddrinfo` results are not cached by the guard (matching the existing skill-service behaviour, which re-resolves to catch DNS changes). For fleets with hundreds of servers this is acceptable (the batch delay and short timeouts already bound the load). If profiling later shows DNS as a bottleneck, an LRU cache on `(hostname, port)` with a short TTL can be added to `url_safety` without changing the public API.
- **Bottlenecks**: none introduced beyond the DNS lookup, which is bounded by the OS resolver and the existing `health_check_timeout_seconds`.
- **Horizontal scaling**: the guard is stateless per process; the `lru_cache` on `trusted_domains()` is process-local and safe.

## File Changes

### New Files

| File Path | Description |
|-----------|-------------|
| `registry/utils/url_safety.py` | Shared SSRF guard (promoted from skill_service) |
| `tests/unit/utils/test_url_safety.py` | Unit tests for the shared guard (scheme, private IPs, allowlist merge, redirect re-validation, DNS failure) |

### Modified Files

| File Path | Lines | Change Description |
|-----------|-------|--------------------|
| `registry/services/skill_service.py` | ~67-192 | Remove local guard defs; import from `url_safety` with aliased names so call sites are unchanged |
| `registry/utils/agent_validator.py` | ~196-230 | Add pre-fetch + post-redirect SSRF validation in `_check_endpoint_reachability` |
| `registry/health/service.py` | ~429-484, 561-800 | Add `UNHEALTHY_SSRF_BLOCKED`; validate `proxy_pass_url`, derived `endpoint`, and post-redirect `response.url` |
| `registry/core/config.py` | ~299 | Add `outbound_url_allowlist` Field |
| `tests/unit/services/test_skill_service_ssrf_allowlist.py` | throughout | Migrate patch targets to `registry.utils.url_safety` |
| `.env.example` | ~623 | Add commented `OUTBOUND_URL_ALLOWLIST` example |
| `docker-compose.yml` | ~195 | Add `OUTBOUND_URL_ALLOWLIST` env passthrough |
| `terraform/aws-ecs/variables.tf` | ~1327 | Add `outbound_url_allowlist` variable |
| `terraform/aws-ecs/main.tf` | ~298 | Pass `outbound_url_allowlist = var.outbound_url_allowlist` |
| `terraform/aws-ecs/terraform.tfvars.example` | ~936 | Add commented example |
| `terraform/aws-ecs/modules/mcp-gateway/variables.tf` | ~1280 | Add variable |
| `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` | ~1266 | Add `OUTBOUND_URL_ALLOWLIST` env var |
| `charts/registry/reserved-env-names.txt` | end | Add `OUTBOUND_URL_ALLOWLIST` |
| `charts/registry/values.yaml` + `templates/deployment.yaml` | - | Render env var if Helm operators should set it |

### Estimated Lines of Code

| Category | Lines |
|----------|-------|
| New code (`url_safety.py` + integrations) | ~180 |
| New tests | ~220 |
| Modified code | ~60 |
| Deployment-surface edits | ~30 |
| **Total** | **~490** |

## Testing Strategy
The full plan lives in `./testing.md`. Summary: unit tests for the shared guard (migrated + extended), integration tests for the agent-card and health-check paths rejecting SSRF payloads, backwards-compat tests for valid public URLs, and deployment-surface wiring checks across Docker, Terraform, and Helm.

## Alternatives Considered

### Alternative 1: Per-call-site inline guards (no shared module)
**Description:** Copy the IP-check logic into `agent_validator.py` and `health/service.py` instead of promoting a shared utility.
**Pros / Cons:** No refactor of skill_service; but the rule set drifts between three copies, and the next fetch path added will be unguarded by default.
**Why Rejected:** The audit's root cause is precisely that the guard is not reused. Duplicating it recreates the drift risk. The user explicitly asked to promote `_is_safe_url()` into a shared utility.

### Alternative 2: Add a new env var per fetch path
**Description:** Separate allowlists for health-check hosts vs. agent-card hosts.
**Pros / Cons:** More granular; but operationally heavier and the trust decision is the same (operator trusts a host for outbound fetch).
**Why Rejected:** A single `OUTBOUND_URL_ALLOWLIST` (merged with the existing `GITHUB_EXTRA_HOSTS`) is simpler and matches the existing single-knob pattern. Granularity can be added later if needed.

### Alternative 3: Connection-time DNS pinning (pin resolved IP for the TCP connection)
**Description:** Resolve once, then force `httpx` to connect to that specific IP (via a custom transport / `extensions={"target": ip}`) so a DNS rebinding between resolution and connection cannot redirect to a private IP.
**Pros / Cons:** Closes the TOCTOU/DNS-rebinding window the pre-fetch-only check leaves open.
**Why Rejected:** Significantly more complex (custom httpx transport, SNI/Host header handling, TLS certificate hostname validation issues), and the existing skill-service guard - which this change promotes and standardises on - already accepts the pre-fetch + post-redirect model. This change does not weaken security relative to the existing, audited skill-fetch path; connection-time pinning is a worthwhile but separate hardening follow-up, noted as future work rather than bundled into a medium-scope change.

### Comparison Matrix

| Criteria | Chosen (shared util + allowlist) | Alt 1 (inline copies) | Alt 2 (per-path allowlist) | Alt 3 (DNS pinning) |
|----------|----------------------------------|-----------------------|----------------------------|---------------------|
| Complexity | Low | Low | Medium | High |
| Drift risk | None (one guard) | High | None | None |
| Operator simplicity | One knob | One knob per path | Multiple knobs | One knob |
| Closes TOCTOU window | No (matches existing) | No | No | Yes |
| Backwards compatible | Yes | Yes | Yes | Needs care |

## Rollout Plan
- Phase 1: Implementation (out of scope for this skill - a future implementer follows the steps above).
- Phase 2: Testing - unit + integration per `testing.md`; confirm the migrated skill-service SSRF test still passes; confirm valid public-URL registrations are unaffected.
- Phase 3: Deployment - ship the new env var (default empty, no behaviour change for operators who do not set it); document the knob; monitor the `UNHEALTHY_SSRF_BLOCKED` status and WARNING logs for false positives on legitimately-trusted internal hosts (operators add them to `OUTBOUND_URL_ALLOWLIST`).

## Open Questions
- Should the `charts/registry` Helm chart render `OUTBOUND_URL_ALLOWLIST` (and backfill `GITHUB_EXTRA_HOSTS`, which is currently only in `mcpgw`)? Recommend yes, for parity with Docker/Terraform. Confirm with the chart maintainers.
- Should reachability SSRF blocks remain warnings (current `validate_agent_card` semantics) or become hard errors that reject registration? Recommend keeping as warnings for backwards compatibility; the unsafe URL is simply not fetched.
- Should an `ssrf_blocks_total` metric be added now or in a follow-up? Recommend follow-up to keep this change focused.

## References
- Existing guard: `registry/services/skill_service.py:128` (`_is_safe_url`), `:94` (`_is_private_ip`), `:82` (`_trusted_domains`)
- Existing test: `tests/unit/services/test_skill_service_ssrf_allowlist.py`
- OWASP SSRF Prevention Cheat Sheet (scheme/IP allowlist, redirect re-validation, metadata-endpoint block)
- CLAUDE.md security guidelines (subprocess/SQL not applicable; input-validation and "never trust external data" apply)
