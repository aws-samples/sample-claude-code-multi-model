# GitHub Issue: Harden outbound URL fetches against SSRF

## Title
Apply shared SSRF URL validation to agent-card and server health-check fetches

## Labels
- security
- enhancement
- refactor
- backend

## Description

### Problem Statement
A recent security audit found that the registry makes outbound HTTP requests to user-supplied URLs without consistent SSRF protection.

- `registry/utils/agent_validator.py` fetches `/.well-known/agent-card.json` during agent-card validation/reachability checks.
- `registry/health/service.py` probes `proxy_pass_url` endpoints and derived MCP/SSE URLs during periodic server health checks.
- Both paths currently skip the SSRF guard that already exists for SKILL.md fetches in `registry/services/skill_service.py` (`_is_safe_url`).

This allows a malicious registration to point the registry at internal IP addresses, link-local addresses, or cloud metadata endpoints (e.g. `169.254.169.254`), potentially leaking credentials or probing the internal network.

### Proposed Solution
Promote the existing `_is_safe_url()` helper and its allowlist machinery from `registry/services/skill_service.py` into a shared utility (`registry/utils/ssrf.py`), then call it before every outbound fetch performed by agent-card validation and the health-check service.

Add a new settings field for an operator-managed allowlist of safe hosts that should bypass private-IP resolution, mirroring the existing `github_extra_hosts` pattern used for SKILL.md fetches. This keeps the change backwards-compatible: existing registrations that point to public endpoints continue to work unchanged, and operators can explicitly allow internal tooling hosts when needed.

### User Stories
- As a gateway operator, I want outbound fetches initiated by user registrations to be validated against an SSRF allowlist so that the registry cannot be used to scan or attack internal infrastructure.
- As a downstream team registering an MCP server or A2A agent, I want clear error messages when my URL is rejected so that I can fix the registration or ask the operator to allowlist the host.
- As a security reviewer, I want all outbound HTTP paths in the registry to reuse a single validation utility so that the control surface is small and auditable.

### Acceptance Criteria
- [ ] A shared SSRF-safe URL validator exists in a location that is not private to skill fetching.
- [ ] `registry/services/skill_service.py` imports and uses the shared validator instead of its private copy (backwards-compatible behaviour is preserved).
- [ ] `registry/utils/agent_validator.py` validates `agent_card.url` (and the derived `/.well-known/agent-card.json` URL) with the shared validator before making HTTP requests; rejected URLs surface as validation warnings or errors with a clear message.
- [ ] `registry/health/service.py` validates `proxy_pass_url` and any derived endpoint URL before making HTTP requests; rejected URLs are logged and treated as unhealthy without performing the fetch.
- [ ] A new settings/environment variable allows operators to configure an additional SSRF allowlist; the existing `github_extra_hosts` semantics for trusted GHES hosts are preserved.
- [ ] All new and existing unit tests pass; new tests cover private-IP blocking, allowlist bypass, redirect safety, and backwards compatibility for existing registrations.
- [ ] Helm values, Terraform variables, and `.env.example` document the new setting.

### Out of Scope
- Changing how the gateway/mcpgw itself proxies inbound traffic to registered servers.
- Adding DNS rebinding protection or time-of-check/time-of-use defences beyond the current `_is_safe_url` implementation.
- Replacing the existing `github_extra_hosts` setting; it remains the primary mechanism for SKILL.md trust and may optionally feed into the shared allowlist.
- Validating the direct "fetch live tools" endpoint in `registry/api/server_routes.py` (uses the same user-supplied `proxy_pass_url` but is outside the agent-card/health-check scope of this issue; should be handled as a follow-up).
- UI changes (the change is backend/config only).

### Dependencies
- None external.

### Related Issues
- Internal security audit finding (no public issue number available).
