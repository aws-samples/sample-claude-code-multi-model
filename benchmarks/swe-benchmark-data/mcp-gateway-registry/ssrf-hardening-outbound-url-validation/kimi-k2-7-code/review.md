# Expert Review: Harden outbound URL fetches against SSRF

*Review date: 2026-07-15*
*Design under review: `lld.md`*

## Reviewer 1: Pixel (Frontend Engineer)

### Focus
UI/UX, API response shapes, error messages, CLI output.

### Strengths
- The design explicitly states that no UI changes are required, which matches the backend-only scope.
- Agent validation errors are returned through the existing `422` shape with a clear `errors` array, so frontend clients can render them without new parsing logic.
- The proposed error message tells users exactly what to do: ask the operator to add the host to `OUTBOUND_URL_ALLOWLIST`.

### Concerns
- The `warnings` array returned by `validate_agent_card` currently carries reachability failures as non-fatal warnings. If a reachability check is blocked by SSRF, the design says it should surface as a validation error. Make sure the route-level code distinguishes fatal SSRF errors from non-fatal "unreachable" warnings correctly.
- Health-check failures are invisible to frontend users unless they poll the health endpoint. This is acceptable but should be documented as a known limitation.

### New libraries / infra dependencies
None.

### Better alternatives considered
None from a frontend perspective; the existing error shape is sufficient.

### Recommendations
1. Ensure the agent route still returns `warnings` for plain reachability timeouts while returning `errors` for SSRF blocks.
2. Consider exposing a count of blocked outbound URLs in the admin health/status endpoint so operators can discover misconfigurations without reading logs.

### Questions for author
- Is there an admin UI or status page that should display the new allowlist value?

### Verdict
**APPROVED WITH CHANGES** - 1 non-blocking recommendation.

---

## Reviewer 2: Byte (Backend Engineer)

### Focus
API design, data models, business logic, performance, code reuse.

### Strengths
- Moving `_is_safe_url` to `registry/utils/ssrf.py` is the right separation of concerns.
- Keeping `github_extra_hosts` separate from `outbound_url_allowlist` avoids coupling skill auth semantics to agent/health checks.
- The redirect re-validation pattern from skill_service is preserved.
- The design validates every derived endpoint (`/mcp`, `/sse`, explicit overrides), not just the base URL.

### Concerns
- **Re-exporting `is_safe_url` as `_is_safe_url` in skill_service** is convenient for tests but creates two names for the same function. Future readers may be confused about which to patch. Prefer updating tests to patch `registry.utils.ssrf.is_safe_url` and removing the alias.
- **`_trusted_domains()` vs `_default_trusted_domains()` split** adds complexity for a function that just returns a frozenset constant. The cache is unnecessary for a literal; simplify to a module-level constant for defaults and merge extras at call time.
- **`socket.getaddrinfo` is blocking** and is called inside an async health-check path. The existing skill code already does this, but the health service runs many checks concurrently. If a hostname resolves slowly, it will block the event loop for all health checks in the batch. Consider `asyncio.to_thread(socket.getaddrinfo, ...)` in the new code.
- The `_is_safe_health_url` instance method on `HealthMonitoringService` is pure and does not use `self`. Make it a module-level function or static method.
- `_parse_extra_hosts` returns a `frozenset`; `_trusted_domains` returns `frozenset[str] | frozenset[str]`. Type signatures are correct but could be simplified.

### New libraries / infra dependencies
None.

### Better alternatives considered
- Alternative: keep `_is_safe_url` private to skill_service and import it elsewhere. Rejected because it couples unrelated modules.
- Alternative: reuse `github_extra_hosts`. Rejected due to semantic confusion.

### Recommendations
1. Remove the `_is_safe_url` alias in skill_service and update tests to patch the shared utility.
2. Simplify `_default_trusted_domains()` to a module constant; remove the unnecessary cache.
3. Wrap `socket.getaddrinfo` in `asyncio.to_thread` for the health service path (and ideally skill_service too, but out of scope).
4. Make `_is_safe_health_url` a static method or free function.
5. Add unit tests for `_parse_extra_hosts` edge cases (spaces, duplicates, empty entries, mixed case).

### Questions for author
- How will existing tests that clear `_trusted_domains.cache_clear()` be updated once the cache is removed or moved?
- Should the shared utility expose `is_safe_url` and `is_safe_url_async` variants, or should callers always wrap `getaddrinfo` themselves?

### Verdict
**APPROVED WITH CHANGES** - 4 medium recommendations, 1 minor.

---

## Reviewer 3: Circuit (SRE/DevOps Engineer)

### Focus
Deployment, monitoring, scaling, infrastructure, config surface.

### Strengths
- The design includes a complete deployment surface checklist covering `.env.example`, Docker Compose, Terraform, and Helm.
- No new secrets are introduced; the new setting is a plain allowlist.
- Blocking bad URLs reduces outbound connection volume, which is beneficial at scale.
- The default allowlist is conservative and uses well-known public hosts.

### Concerns
- **Helm registry chart currently does not render `GITHUB_EXTRA_HOSTS`**, so adding `OUTBOUND_URL_ALLOWLIST` introduces a new pattern to a chart that has not carried GitHub settings before. Ensure the template change is tested with `helm unittest` and that `reserved-env-names.txt` is updated so `extraEnv` validation rejects collisions.
- **Terraform variable count is already large.** Adding another variable is fine, but ensure it is grouped near `github_extra_hosts` and documented consistently.
- **Docker Compose `extra_env/` preflight validator** reads `charts/*/reserved-env-names.txt`. If `OUTBOUND_URL_ALLOWLIST` is added to the registry chart's reserved list, the preflight script will reject users who try to override it via `extra_env`. This is correct because the chart manages it, but it must be communicated.
- **ECS task definition ordering**: the new env entry should be placed near `GITHUB_EXTRA_HOSTS` to keep related settings together.

### New libraries / infra dependencies
None.

### Better alternatives considered
None.

### Recommendations
1. Run `helm unittest charts/registry charts/mcp-gateway-registry-stack` after adding the new value and reserved name.
2. Add a commented example to `.env.example` showing multiple hosts.
3. Update the unified parameter reference document if one exists in `docs/`.
4. Add an alert rule (or at least a log filter example) for `SSRF protection: Blocked` so operators can detect probing attempts.

### Questions for author
- Is `OUTBOUND_URL_ALLOWLIST` intended to be set per-environment or globally across all deployments?
- Should the chart also expose `outboundUrlAllowlistExistingSecret` for operators who store allowlists in existing secrets?

### Verdict
**APPROVED WITH CHANGES** - 2 medium recommendations, 2 minor.

---

## Reviewer 4: Cipher (Security Engineer)

### Focus
AuthN/AuthZ, input validation, OWASP, SSRF, data protection.

### Strengths
- The design directly addresses the audit finding by reusing and centralizing the existing control.
- It checks derived endpoints, not just the base URL, which prevents explicit `mcp_endpoint`/`sse_endpoint` bypasses.
- It preserves redirect re-validation, mitigating open-redirect-to-SSRF in the agent-card reachability path.
- The allowlist is additive and defaults to empty, so the change is deny-by-default for private IPs.

### Concerns
- **DNS rebinding is not addressed.** The design notes this as a limitation, but it should be called out explicitly in the issue acceptance criteria and LLD constraints.
- **`github_extra_hosts` bypasses IP checks entirely.** If an operator adds a host to `github_extra_hosts` for skill auth purposes, that host is also trusted for skill SSRF. This is existing behavior and not changed by this design, but it widens the skill trust surface beyond what some operators expect. Consider documenting that `github_extra_hosts` is a combined auth+SSRF trust decision.
- **The new `outbound_url_allowlist` also bypasses IP checks entirely.** If an operator allows `internal.example.com`, an attacker who can influence DNS or compromise that host can still SSRF through it. This is inherent to host-based allowlisting but should be documented.
- **No rejection of URLs with embedded credentials** such as `http://user:pass@host`. While not an SSRF vector directly, embedded creds can leak in logs and should be rejected.
- **Health-check blocking does not alert operators.** A server silently marked unhealthy due to SSRF policy looks like a down server. The distinction should be visible in health status and logs.
- **The `mcp_client_service` tool-fetch path is only protected indirectly.** If `server_routes.py` calls `get_tools_from_server_with_server_info` directly, that path bypasses the new guard. The LLD lists this as an open question; it should be resolved.

### New libraries / infra dependencies
None.

### Better alternatives considered
- Alternative: validate URLs at registration time and store a "safe" bit in the DB, then skip validation in background health checks. Rejected because DNS/IP mappings can change, and the existing design validates at fetch time.
- Alternative: use a URL parser that rejects uncommon schemes and path tricks. The current `urlparse` check is adequate but could be hardened.

### Recommendations
1. Add explicit acceptance criterion: "DNS rebinding protection is not in scope; document the limitation."
2. Reject URLs with embedded credentials in `is_safe_url` as a defence-in-depth measure.
3. Resolve the open question about `server_routes.py` direct tool fetch - either add validation there or explicitly exclude it from scope in the issue.
4. Add a dedicated health status string prefix (e.g. `unhealthy: SSRF policy blocked URL`) so operators can distinguish policy blocks from server failures.
5. Document the allowlist trust model: allowlisted hosts bypass IP checks; operators must trust the host and its DNS.

### Questions for author
- Has the team considered requiring `https` scheme only for agent/health URLs in production? The current design allows `http://`.
- Should failed SSRF checks be emitted as a structured security event for SIEM ingestion?

### Verdict
**APPROVED WITH CHANGES** - 1 blocker (resolve `server_routes.py` scope), 3 medium recommendations.

---

## Reviewer 5: Sage (SMTS / Overall)

### Focus
Architecture, code quality, maintainability, alignment with project conventions.

### Strengths
- The design is minimal, focused, and follows the codebase's preference for simple, entry-level-developer-friendly code.
- It leverages an existing control rather than inventing a new one, which reduces risk.
- The configuration surface is explicit and complete.
- Backwards compatibility is preserved for existing registrations and skill GHES setups.

### Concerns
- **The shared utility name `security.py` is broad.** The module currently contains only URL validation. Consider `registry/utils/url_security.py` or `registry/utils/ssrf.py` for a more precise name. If more security helpers are added later, `security.py` becomes justified.
- **The design introduces a new settings field but also keeps `github_extra_hosts` as a separate concept.** This is correct, but the relationship between the two should be documented in `docs/` or `.env.example` comments.
- **Test migration from `skill_service._is_safe_url` to shared utility** is non-trivial because several tests patch the private function. Ensure the test update plan is explicit in `testing.md`.
- **The estimated 700 LOC is on the high side for a "medium" scope.** The actual code change is small; most lines are tests and deployment wiring. This is acceptable but should be communicated to the implementer.

### New libraries / infra dependencies
None.

### Better alternatives considered
None at the architecture level.

### Recommendations
1. Keep the shared utility name `registry/utils/ssrf.py` for precision.
2. Add a cross-reference in `.env.example` between `GITHUB_EXTRA_HOSTS` and `OUTBOUND_URL_ALLOWLIST` explaining when to use each.
3. Include a test-migration checklist in `testing.md`.
4. Confirm that the new module follows the project logging format (`%(asctime)s,p%(process)s,...`).

### Questions for author
- Is there a central `docs/security/ssrf.md` or similar where this control should be documented for operators?

### Verdict
**APPROVED WITH CHANGES** - 2 medium recommendations, 2 minor.

---

## Review Summary

| Reviewer | Verdict | Blockers | Key Recommendations |
|----------|---------|----------|---------------------|
| Pixel (Frontend) | APPROVED WITH CHANGES | 0 | Keep SSRF blocks as errors, reachability issues as warnings; consider admin visibility. |
| Byte (Backend) | APPROVED WITH CHANGES | 0 | Remove `_is_safe_url` alias; simplify default-domain cache; wrap `getaddrinfo` in thread for async callers; make helper static. |
| Circuit (SRE) | APPROVED WITH CHANGES | 0 | Run helm unittest; add alert example; update docs; keep ECS env ordering tidy. |
| Cipher (Security) | APPROVED WITH CHANGES | 1 | Resolve `server_routes.py` direct tool-fetch scope; reject embedded creds; document DNS rebinding limitation. |
| Sage (SMTS) | APPROVED WITH CHANGES | 0 | Rename module to `ssrf.py`; cross-reference settings in `.env.example`; add test-migration checklist. |

## Consolidated Next Steps

1. **Resolve the blocker:** Decide whether `registry/api/server_routes.py::fetch_live_tools` should also validate `proxy_pass_url`. Update the issue and LLD accordingly.
2. Use `registry/utils/ssrf.py` as the shared utility name (already reflected in the LLD).
3. **Simplify** the default trusted domains to a module constant and remove the re-export alias in skill_service.
4. **Harden `is_safe_url`** to reject URLs with embedded credentials.
5. **Run Helm unittest** and update `reserved-env-names.txt` after adding the new value.
6. **Update `testing.md`** with a test-migration checklist for the existing skill_service patches.
7. **Document** the DNS rebinding limitation and the trust model for allowlisted hosts.
