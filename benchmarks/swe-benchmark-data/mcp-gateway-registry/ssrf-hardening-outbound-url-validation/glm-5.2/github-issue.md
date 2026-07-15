# GitHub Issue: SSRF Hardening - Outbound URL Validation

## Title
Promote the existing `_is_safe_url()` guard into a shared utility and apply it to agent-card fetch and server health-check paths

## Labels
- security
- enhancement
- hardening

## Description

### Problem Statement
A recent security audit found that the registry fetches user-supplied URLs without a Server-Side Request Forgery (SSRF) guard on several outbound fetch paths. The registry already contains a working SSRF guard - `_is_safe_url()` in `registry/services/skill_service.py` - that validates scheme, blocks private/loopback/link-local/resolved IPs, blocks cloud metadata endpoints, and honours a trusted-host allowlist. However, that guard is private to the skill service and is not reused by the other outbound fetch paths that consume registrant-supplied URLs.

The two unguarded paths called out by the audit are:

1. **Agent-card endpoint reachability check** - `registry/utils/agent_validator.py` (`_check_endpoint_reachability`) issues `httpx.get(f"{url}/.well-known/agent-card.json")` against a URL supplied by the registrant registering an A2A agent, with no SSRF validation.
2. **Server health checks** - `registry/health/service.py` constructs `httpx.AsyncClient` requests against `proxy_pass_url` and the derived endpoint URLs stored on each registered MCP server. These URLs originate from server registration (registrant- or operator-supplied) and are fetched by the health checker on a recurring schedule with no SSRF validation.

Because these fetches are performed server-side by the registry, a malicious or compromised registrant can submit a URL that resolves to an internal address (for example `http://169.254.169.254/...`, `http://127.0.0.1:...`, or a DNS name that resolves to an RFC 1918 address) and induce the registry to probe or exfiltrate from internal services, including the cloud metadata endpoint.

### Proposed Solution
Promote the existing `_is_safe_url()` / `_is_private_ip()` / `_trusted_domains()` logic out of `skill_service.py` into a shared, reusable utility module under `registry/utils/` (for example `registry/utils/url_safety.py`). Keep the skill service calling the same logic via the shared module so behaviour for skill fetches is unchanged. Then apply the shared guard to:

- the agent-card reachability check (`_check_endpoint_reachability`), and
- the server health-check fetch paths in `registry/health/service.py` (the `proxy_pass_url` and derived endpoint URLs).

Introduce a single configuration knob for the outbound-fetch trusted-host allowlist so operators can extend the built-in allowlist (currently `github.com`, `gitlab.com`, `raw.githubusercontent.com`, `bitbucket.org` plus `settings.github_extra_hosts`) for their own trusted internal hosts. The guard must be applied before any outbound request is issued and must re-validate the final URL after any redirect so that an open-redirect or a DNS rebinding cannot land the fetch on a private IP.

The change must be backwards-compatible: existing registrations with valid public URLs must continue to pass health checks and agent-card validation unchanged. Registrations whose URLs already resolved to private/internal addresses (the vulnerable case the audit is closing) will be rejected at validation/fetch time, which is the intended behaviour.

### User Stories
- As a platform operator running the gateway on ECS, I want every outbound URL the registry fetches on my behalf to be validated against SSRF rules, so that a malicious registrant cannot make the registry probe internal services or the cloud metadata endpoint.
- As a downstream team registering an MCP server or A2A agent, I want clear, logged feedback when my registered URL is rejected by SSRF validation, so that I can correct it without guessing.
- As a security engineer, I want one shared, well-tested SSRF guard used by every outbound fetch path, so that the rule set cannot drift between call sites and new fetch paths default to safe.
- As a platform operator, I want a configuration knob to extend the trusted-host allowlist, so that I can permit my organisation's private GHES or internal artifact hosts without disabling the guard.

### Acceptance Criteria
- [ ] A shared SSRF validation utility exists under `registry/utils/` exposing the scheme check, IP-resolution check, cloud-metadata block, and trusted-host allowlist.
- [ ] `registry/services/skill_service.py` is refactored to import the shared utility instead of defining its own copy; skill-fetch behaviour is unchanged.
- [ ] `_check_endpoint_reachability` in `registry/utils/agent_validator.py` validates the agent URL (and the final post-redirect URL) with the shared guard before issuing `httpx.get`; unsafe URLs are rejected with a logged, actionable message.
- [ ] The server health-check paths in `registry/health/service.py` validate `proxy_pass_url` and any derived endpoint URL with the shared guard before each outbound request; unsafe URLs are treated as unhealthy with a clear status detail rather than crashing the health loop.
- [ ] A single configuration setting (for example `outbound_url_allowlist` / `OUTBOUND_URL_ALLOWLIST`) extends the trusted-host allowlist across all outbound fetch paths; the existing `github_extra_hosts` behaviour for skill fetches is preserved.
- [ ] The new setting is wired through every deployment surface: `.env.example`, `docker-compose.yml`, Terraform/ECS variables, and the Helm chart values plus reserved-env-name lists, and is documented in the relevant operations docs.
- [ ] Redirect re-validation is applied on every fetch path that follows redirects, so a redirect to a private/internal IP is blocked.
- [ ] Unit tests cover the shared utility (private IPs, loopback, link-local, cloud metadata, scheme restrictions, allowlist, DNS resolution failure) following existing test conventions.
- [ ] Integration tests cover the agent-card and health-check paths rejecting SSRF payloads (for example `http://127.0.0.1`, `http://169.254.169.254`, a host resolving to an RFC 1918 address).
- [ ] Existing registrations with valid public URLs continue to pass validation and health checks (backwards compatibility).
- [ ] No new runtime dependencies are introduced unless explicitly justified.

### Out of Scope
- Rewriting the HTTP client layer or introducing a central egress proxy / network policy. This change is application-layer URL validation only.
- Changing how or where registered server URLs are stored.
- Adding SSRF validation to operator-configured, non-registrant outbound calls (for example the OAuth provider token endpoints in `registry/auth/routes.py` and the identity-provider managers in `registry/utils/okta_manager.py` / `entra_manager.py`); these are operator-configured trusted endpoints and are not in the audit scope. (They may be noted as future work.)
- DNS pinning / connection-time IP re-check beyond the pre-fetch and post-redirect re-validation already performed by the existing guard.

### Dependencies
- None. The existing guard already uses only stdlib (`ipaddress`, `socket`, `urllib.parse`) and `httpx`, both already present.

### Related Issues
- None known at filing time.
