# Testing Plan: SSRF Hardening - Outbound URL Validation

*Created: 2026-07-15*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

## Overview

### Scope of Testing
Verify that the shared SSRF guard (`registry/utils/url_safety.py`) blocks private/loopback/link-local/reserved/cloud-metadata URLs and non-http(s) schemes, that the agent-card reachability check (`registry/utils/agent_validator.py::_check_endpoint_reachability`) and the server health checks (`registry/health/service.py`) use it, that the new `OUTBOUND_URL_ALLOWLIST` extends the trusted set, and that existing valid public-URL registrations and skill fetches are unaffected (backwards compatibility).

### Prerequisites
- [ ] Registry running locally (Docker Compose: `./build_and_run.sh`, or `uv run python -m registry.main`), reachable at `http://localhost:8000`
- [ ] MongoDB running for integration tests (`docker ps | grep mongo`)
- [ ] An access token with register permission (e.g. `.oauth-tokens/ingress.json`)
- [ ] A controllable DNS / local host that can resolve a chosen hostname to `127.0.0.1` or `10.0.0.5` (for redirect/rebind tests, use a local HTTP server that 302-redirects to `http://127.0.0.1/`)

### Shared Variables
```bash
export REGISTRY_URL="http://localhost:8000"
export ACCESS_TOKEN=$(jq -r '.access_token' .oauth-tokens/ingress.json)
curl_auth=(-H "Authorization: Bearer ${ACCESS_TOKEN}" -H "Content-Type: application/json")
```

---

## 1. Functional Tests

### 1.1 curl / HTTP Tests

These exercise the agent-card reachability and server health-check paths end to end through the API.

#### 1.1.1 Agent registration: SSRF URL is not fetched (reachability warning)
Register an A2A agent whose `url` points at the loopback metadata endpoint. Reachability is a warning, so registration succeeds but the endpoint is NOT probed.

```bash
curl -s -X POST "${REGISTRY_URL}/agents/register" \
  "${curl_auth[@]}" \
  -d '{
    "name": "ssrf-probe-agent",
    "description": "should not be fetched",
    "url": "http://169.254.169.254/latest/meta-data/",
    "protocol_version": "1.0",
    "visibility": "public"
  }' | jq .
```

**Expected:** HTTP 200/201 with a validation result that includes a warning like `Agent endpoint unreachable: ... SSRF ...` (or the registration succeeds with a reachability warning). **Assertion:** the registry logs contain `SSRF protection: blocked agent endpoint reachability check`. **Negative control:** the metadata endpoint was NOT actually requested (no outbound call to 169.254.169.254 - verify via a local packet capture or by pointing the URL at a local listener that records hits and confirming zero hits).

#### 1.1.2 Server registration with a private-IP URL is stored but health-checked as SSRF-blocked
```bash
curl -s -X POST "${REGISTRY_URL}/register" \
  "${curl_auth[@]}" \
  -d '{
    "name": "ssrf-probe-server",
    "url": "http://10.0.0.5/mcp",
    "deployment": "remote"
  }' | jq .
```

**Expected:** registration is accepted (the URL is stored; validation of scheme/format passes). After the next health-check cycle, `GET /servers` shows this server with status `unhealthy-ssrf-blocked`.

```bash
# Trigger / wait for a health cycle, then:
curl -s "${REGISTRY_URL}/servers" "${curl_auth[@]}" | jq '.[] | select(.name=="ssrf-probe-server") | .status'
```

**Expected output:** `"unhealthy-ssrf-blocked"` (or the repo's exact `HealthStatus` representation of the new value). **Assertion:** no outbound connection to `10.0.0.5` was attempted.

#### 1.1.3 Non-http scheme rejected
```bash
curl -s -X POST "${REGISTRY_URL}/agents/register" \
  "${curl_auth[@]}" \
  -d '{
    "name": "file-scheme-agent",
    "description": "scheme block",
    "url": "file:///etc/passwd",
    "protocol_version": "1.0",
    "visibility": "public"
  }' | jq .
```

**Expected:** reachability warning `SSRF protection: Blocked URL with scheme 'file'`; no fetch attempted.

#### 1.1.4 Redirect to a private IP is blocked
Point the agent `url` at a local redirector (e.g. `http://localhost:9999/redirect`) that returns `302 Location: http://127.0.0.1/`. Register the agent with `check_reachability`.

**Expected:** reachability returns unreachable with a redirected-to-unsafe message; the registry logs `redirected to unsafe URL`. **Assertion:** the registry did not follow the redirect to 127.0.0.1 (or, if `follow_redirects` was disabled per the LLD revision, only the first hop occurred and was then rejected).

### 1.2 CLI Tests

**Not Applicable** - this change introduces no new CLI command and modifies no CLI flags. The `cli/` package is unaffected. (The existing CLI continues to call the same API endpoints; behaviour is covered by the HTTP tests above.)

---

## 2. Backwards Compatibility Tests

### 2.1 Valid public-URL server registration still passes health checks
Register a server pointing at a real public MCP endpoint (or the repo's own example server reachable on a public host).

```bash
curl -s -X POST "${REGISTRY_URL}/register" \
  "${curl_auth[@]}" \
  -d '{"name":" legit-public","url":"https://example.com/mcp","deployment":"remote"}' | jq .
# Wait one health cycle
curl -s "${REGISTRY_URL}/servers" "${curl_auth[@]}" | jq '.[] | select(.name=="legit-public") | .status'
```

**Expected:** status is `healthy` or a network-error status (NOT `unhealthy-ssrf-blocked`). Pre-change behaviour is preserved for public URLs.

### 2.2 Skill fetches still work after the guard is relocated
Register/refresh a SKILL.md from `github.com` (a default-trusted host). **Expected:** the skill is fetched successfully, identical to pre-change behaviour. The guard's algorithm is unchanged; only its location moved.

```bash
curl -s -X POST "${REGISTRY_URL}/skills/register" \
  "${curl_auth[@]}" \
  -d '{"skill_url":"https://github.com/<org>/<repo>/blob/main/SKILL.md"}' | jq .
```

**Expected:** success (matches pre-change result).

### 2.3 Existing `GITHUB_EXTRA_HOSTS` allowlist still honoured
Set `GITHUB_EXTRA_HOSTS=github.mycompany.com` (with a mock resolving to a private IP), restart the registry, and fetch a skill from that host. **Expected:** allowed (bypasses IP check), identical to pre-change. Confirms the merge did not regress the existing knob.

### 2.4 Existing registrations (pre-upgrade) still health-check correctly
On an upgraded instance with pre-existing registered servers that use public URLs, **Expected:** all previously-healthy servers remain healthy; none flip to `unhealthy-ssrf-blocked` (which would indicate a false positive).

### 2.5 Pre-change request shapes still accepted
The registration request schema is unchanged (no new required fields). A pre-existing client payload that registered successfully before the change must still register successfully. **Expected:** HTTP 200/201, identical response shape.

---

## 3. UX Tests

### 3.1 Operator-visible health status is clear
When a server is SSRF-blocked, `GET /servers` and the web UI (`/`) should show a readable status for that server, not a raw exception or a blank.

```bash
curl -s "${REGISTRY_URL}/servers" "${curl_auth[@]}" \
  | jq '.[] | select(.status | tostring | contains("ssrf"))'
```

**Expected:** a human-readable status/detail. **Assertion:** open the registry web UI and confirm the blocked server renders with an "unhealthy" indicator and (if the UI maps it) a security-blocked label rather than "unknown".

### 3.2 Block reason is logged and actionable
The registry log should contain a WARNING line naming the hostname and the reason (e.g. `Blocked URL resolving to private IP '10.0.0.5' for hostname '...'`). **Assertion:**
```bash
docker compose logs registry 2>&1 | grep "SSRF protection" | tail
```
**Expected:** lines are present, do not contain credentials, and reference the offending hostname. **Negative:** no full credentialed URL is logged (hostname + scheme only, per the security recommendation).

### 3.3 Agent reachability warning is surfaced to the registering user
The response to `POST /agents/register` for an SSRF-blocked agent should include the reachability warning text so the downstream team can correct the URL. **Expected:** response JSON contains a `warnings` array with an `Agent endpoint unreachable: ...` entry.

---

## 4. Deployment Surface Tests

### 4.1 Docker wiring
Confirm `OUTBOUND_URL_ALLOWLIST` is wired in `docker-compose.yml` and passed to the registry container.

```bash
grep -n "OUTBOUND_URL_ALLOWLIST" docker-compose.yml
OUTBOUND_URL_ALLOWLIST=internal-artifacts.mycompany.com docker compose up -d registry
docker compose exec registry printenv OUTBOUND_URL_ALLOWLIST
```

**Expected:** the value is present inside the container.

### 4.2 Terraform / ECS wiring
Validate the Terraform variable, the root passthrough, the module variable, and the rendered env var.

```bash
cd terraform/aws-ecs
grep -n "outbound_url_allowlist" variables.tf main.tf modules/mcp-gateway/variables.tf modules/mcp-gateway/ecs-services.tf terraform.tfvars.example
terraform init && terraform validate
terraform plan -var outbound_url_allowlist=internal-artifacts.mycompany.com -out /tmp/tfplan
```

**Expected:** `terraform validate` passes; `terraform plan` renders an `OUTBOUND_URL_ALLOWLIST` env var with value `internal-artifacts.mycompany.com` on the registry task. Confirm the variable has the same validation (if any) as `github_extra_hosts`.

### 4.3 Helm / EKS wiring
Render the chart and confirm the env var appears (or is intentionally omitted per the LLD decision), and that the reserved-name list rejects it as an `extraEnv` override.

```bash
helm dep update charts/mcp-gateway-registry-stack
helm template charts/registry -f charts/registry/values.yaml \
  --set app.outboundUrlAllowlist=internal-artifacts.mycompany.com \
  | grep -n "OUTBOUND_URL_ALLOWLIST"
grep -n "OUTBOUND_URL_ALLOWLIST" charts/registry/reserved-env-names.txt
```

**Expected:** the env var is rendered in the registry Deployment when set via values, and is present in `reserved-env-names.txt` (so it cannot be overridden via `extraEnv`). Then run the Helm unittest suite (per the repo's CLAUDE.md contract):

```bash
helm unittest charts/registry
helm unittest charts/mcp-gateway-registry-stack
```

**Expected:** all suites pass, including the updated positional assertions in `charts/registry/tests/extra_env_test.yaml` that account for the new `env[N].name` index. If `OUTBOUND_URL_ALLOWLIST` is rendered, the `NOTE:` comment on the index assertion must be updated to describe the new assumption.

### 4.4 Deploy and verify
Deploy to a staging ECS/EKS environment with `OUTBOUND_URL_ALLOWLIST` unset (default). **Expected:** registry starts normally; no SSRF blocks on legitimate servers. Set the allowlist to an internal host the registry must reach and restart the task/pod. **Expected:** that host is now reachable for outbound fetches.

### 4.5 Rollback verification
Roll back to the previous image (without the guard). **Expected:** existing registrations still work (the change is additive; no schema migration). Roll forward again and confirm the `lru_cache` allowlist change requires a process restart to take effect (documented in operations docs): changing `OUTBOUND_URL_ALLOWLIST` without restarting the task does NOT change behaviour until restart.

---

## 5. End-to-End API Tests

### 5.1 Register server -> health check marks SSRF -> allowlist recovers
1. Register a server whose `url` resolves to a private IP (mock DNS `internal.svc` -> `10.0.0.5`).
2. Wait one health cycle; assert status `unhealthy-ssrf-blocked`.
3. Add `internal.svc` to `OUTBOUND_URL_ALLOWLIST`, restart the registry.
4. Wait one health cycle; assert the server is now probed (healthy or network-error, but NOT ssrf-blocked).

**Covers:** the full registration -> storage -> health-check -> allowlist-bypass loop across the API and the guard.

### 5.2 Register agent -> reachability SSRF block -> allowlist recovers
1. `POST /agents/register` with `url` resolving to `127.0.0.1`; assert reachability warning, no fetch.
2. Add the host to `OUTBOUND_URL_ALLOWLIST`, restart, re-register; assert the endpoint is now probed.

### 5.3 Skill fetch -> guard relocation regression
1. Register a skill from `github.com`; assert success.
2. Register a skill from a private-IP host (not in the allowlist); assert block, matching pre-change behaviour.

**Covers:** the skill path still uses the (now shared) guard with no regression.

### 5.4 Redirect chain re-validation
1. Stand up a local redirector `http://localhost:9999/r` -> `http://localhost:9999/r2` -> `http://127.0.0.1/`.
2. Register a server/agent pointing at the redirector.
3. Assert the fetch is blocked at the redirect to `127.0.0.1` and the registry logs the redirected-to-unsafe URL.

---

## 6. Unit Tests

Add to `tests/unit/utils/test_url_safety.py` (new) following the conventions in the existing `tests/unit/services/test_skill_service_ssrf_allowlist.py` (patch `registry.utils.url_safety.settings` and `registry.utils.url_safety.socket.getaddrinfo`; clear the `trusted_domains` lru_cache between tests).

```python
class TestIsPrivateIp:
    def test_loopback_blocked(self) -> None: ...
    def test_private_rfc1918_blocked(self) -> None: ...        # 10.x, 172.16-31.x, 192.168.x
    def test_link_local_blocked(self) -> None: ...             # 169.254.x
    def test_cloud_metadata_ipv4_blocked(self) -> None: ...    # 169.254.169.254
    def test_cloud_metadata_ipv6_blocked(self) -> None: ...    # fd00:ec2::254 (if in scope)
    def test_public_ip_allowed(self) -> None: ...              # 8.8.8.8
    def test_invalid_ip_string_fail_closed(self) -> None: ...

class TestIsUrlSafeToFetch:
    def test_http_https_allowed(self) -> None: ...
    def test_file_scheme_blocked(self) -> None: ...
    def test_no_hostname_blocked(self) -> None: ...
    def test_private_ip_resolution_blocked(self) -> None: ...  # mock getaddrinfo -> 10.0.0.5
    def test_loopback_resolution_blocked(self) -> None: ...
    def test_metadata_resolution_blocked(self) -> None: ...
    def test_dns_resolution_failure_fail_closed(self) -> None: ...  # getaddrinfo raises gaierror
    def test_trusted_default_host_skips_ip_check(self) -> None: ... # github.com
    def test_outbound_url_allowlist_host_skips_ip_check(self) -> None: ...
    def test_github_extra_hosts_host_skips_ip_check(self) -> None: ...  # regression
    def test_allowlist_merge_whitespace_case(self) -> None: ...

class TestTrustedDomainsMerge:
    def test_defaults_plus_github_extra_hosts_plus_allowlist(self) -> None: ...
    def test_cache_returns_same_frozenset(self) -> None: ...
```

Integration-level unit tests (mock httpx) for the two call sites:

```python
class TestAgentReachabilitySsrf:
    def test_private_ip_url_not_fetched(self, monkeypatch) -> None: ...  # assert httpx.get not called
    def test_redirect_to_private_blocked(self) -> None: ...
    def test_valid_public_url_probed(self) -> None: ...

class TestHealthCheckSsrf:
    def test_proxy_pass_url_private_marks_ssr_blocked(self) -> None: ...
    def test_derived_endpoint_private_blocked(self) -> None: ...
    def test_redirect_to_private_blocked(self) -> None: ...
    def test_valid_public_url_healthy(self) -> None: ...
```

Migrate the existing `tests/unit/services/test_skill_service_ssrf_allowlist.py` patch targets to `registry.utils.url_safety` (or move the canonical tests to `tests/unit/utils/test_url_safety.py` and leave a thin re-export smoke test in the old file). Confirm the existing skill-service SSRF tests still pass after the move.

## 7. Test Execution Checklist
- [ ] Section 1 (Functional) passes
- [ ] Section 2 (Backwards Compat) verified or marked Not Applicable
- [ ] Section 3 (UX) verified or marked Not Applicable
- [ ] Section 4 (Deployment) verified or marked Not Applicable
- [ ] Section 5 (E2E) verified or marked Not Applicable
- [ ] Unit tests added under `tests/unit/utils/test_url_safety.py` (and migrated skill-service tests)
- [ ] Integration tests added under `tests/integration/` for the agent-card and health-check SSRF paths
- [ ] `uv run pytest tests/ -n 8` passes with no regressions
- [ ] `uv run bandit -r registry/utils/url_safety.py registry/utils/agent_validator.py registry/health/service.py` introduces no new findings
- [ ] Helm unittest suites pass: `helm unittest charts/registry charts/mcp-gateway-registry-stack`
