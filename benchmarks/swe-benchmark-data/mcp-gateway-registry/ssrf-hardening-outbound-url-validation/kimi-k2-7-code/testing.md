# Testing Plan: Harden outbound URL fetches against SSRF

*Created: 2026-07-15*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

## Overview

### Scope of Testing
Verify that the shared SSRF validator blocks private/internal IPs for agent-card reachability and MCP server health checks, while preserving backwards-compatible behavior for public endpoints and existing SKILL.md fetches.

### Prerequisites
- [ ] Repository checked out at tag `1.24.4`.
- [ ] Python environment with `uv` installed.
- [ ] MongoDB running if running integration tests (not required for unit tests).
- [ ] Helm unittest plugin installed if running Helm tests.
- [ ] Terraform installed if running Terraform validation.

### Shared Variables

```bash
export REPO_ROOT="$(git rev-parse --show-toplevel)"
export REPO="$REPO_ROOT/benchmarks/swe-benchmark-data/mcp-gateway-registry/repo"
```

## 1. Functional Tests

### 1.1 Shared SSRF utility tests

Run the new unit tests for `registry/utils/ssrf.py`:

```bash
cd "$REPO"
uv run pytest tests/unit/utils/test_ssrf.py -v
```

Expected results:
- `test_blocks_private_ipv4` passes (`http://192.168.1.1/foo` is unsafe).
- `test_blocks_loopback` passes (`http://127.0.0.1/foo` is unsafe).
- `test_blocks_link_local` passes (`http://169.254.1.1/foo` is unsafe).
- `test_blocks_cloud_metadata` passes (`http://169.254.169.254/latest/meta-data` is unsafe).
- `test_blocks_non_http_scheme` passes (`ftp://example.com/foo` is unsafe).
- `test_blocks_missing_hostname` passes (`http:///path` is unsafe).
- `test_allows_public_host` passes (`http://example.com/foo` is safe).
- `test_allowlist_bypasses_ip_check` passes (`http://internal.example.com` with `extra_hosts="internal.example.com"` is safe even if it resolves to `10.0.0.1`).
- `test_allowlist_is_case_insensitive` passes (`INTERNAL.EXAMPLE.COM` matches `internal.example.com`).
- `test_allowlist_ignores_whitespace_and_empty_entries` passes (`" internal.example.com , , "` parses correctly).

### 1.2 Agent-card validation SSRF tests

Run the updated agent validator tests:

```bash
cd "$REPO"
uv run pytest tests/unit/utils/test_agent_validator.py -v
```

Expected results:
- `test_agent_card_with_private_url_is_invalid` passes: registering an agent with `"url": "http://192.168.1.1/agent"` returns `is_valid=False` and an error containing `blocked by SSRF policy`.
- `test_agent_card_with_public_url_is_valid` passes: registering an agent with `"url": "http://example.com/agent"` returns `is_valid=True`.
- `test_agent_card_with_allowlisted_private_url_is_valid` passes: with `OUTBOUND_URL_ALLOWLIST=internal.example.com`, an agent with `"url": "http://internal.example.com/agent"` returns `is_valid=True`.
- `test_reachability_check_blocked_for_private_url` passes: with `verify_endpoint=True`, a public host that redirects to a private IP is blocked (if redirect re-validation is implemented for agent cards).

### 1.3 Health-check SSRF tests

Run the updated health service tests:

```bash
cd "$REPO"
uv run pytest tests/unit/health/test_health_service.py::TestSSRF -v
```

Expected results:
- `test_check_server_endpoint_blocks_private_proxy_url` passes: `_check_server_endpoint_transport_aware(client, "http://192.168.1.1", mock_server_info)` returns `(False, "unhealthy: URL blocked by SSRF policy")` and the mock client makes no requests.
- `test_check_server_endpoint_blocks_private_mcp_endpoint` passes: `server_info` with `"mcp_endpoint": "http://10.0.0.2/mcp"` is blocked even if `proxy_pass_url` is public.
- `test_check_server_endpoint_blocks_private_sse_endpoint` passes: `server_info` with `"sse_endpoint": "http://10.0.0.3/sse"` is blocked.
- `test_check_server_endpoint_allows_public_url` passes: a public URL proceeds to the normal health check.
- `test_perform_immediate_health_check_marks_private_url_unhealthy` passes: `perform_immediate_health_check("/test-server")` with a private `proxy_pass_url` results in `"unhealthy: URL blocked by SSRF policy"`.

### 1.4 Skill service backwards-compatibility tests

Run the existing skill SSRF tests after migration:

```bash
cd "$REPO"
uv run pytest tests/unit/services/test_skill_service_ssrf_allowlist.py -v
uv run pytest tests/unit/test_skill_service_github_auth.py -v
uv run pytest tests/unit/api/test_skill_inline_content.py -v
```

Expected results:
- All tests pass.
- `github_extra_hosts` still bypasses private-IP checks for skill fetches.

## 2. Backwards Compatibility Tests

### 2.1 Existing registrations keep working

Create or use existing fixtures that register agents/servers with public URLs:

```bash
cd "$REPO"
uv run pytest tests/unit/api/test_agent_routes.py -v
uv run pytest tests/unit/api/test_server_routes.py -v
```

Expected results:
- No new SSRF-related failures.
- Existing tests that use `http://localhost` for health checks may need to be updated to use `http://127.0.0.1.nip.io` or to mock `is_safe_url`, because `localhost` resolves to `127.0.0.1` and will now be blocked.

### 2.2 Default behavior without allowlist

Unset `OUTBOUND_URL_ALLOWLIST` and `GITHUB_EXTRA_HOSTS`:

```bash
cd "$REPO"
unset OUTBOUND_URL_ALLOWLIST
unset GITHUB_EXTRA_HOSTS
uv run pytest tests/unit/utils/test_ssrf.py tests/unit/services/test_skill_service_ssrf_allowlist.py -v
```

Expected results:
- Skill fetches to `github.com`, `gitlab.com`, `raw.githubusercontent.com`, `bitbucket.org` are allowed.
- Agent/health checks to public IPs are allowed.
- Agent/health checks to private IPs are blocked.

### 2.3 GitHub Enterprise skill fetching still works

```bash
cd "$REPO"
OUTBOUND_URL_ALLOWLIST="" GITHUB_EXTRA_HOSTS="github.mycompany.com" \
  uv run pytest tests/unit/services/test_skill_service_ssrf_allowlist.py -v
```

Expected results:
- `github.mycompany.com` is trusted for skill fetches.
- `github.mycompany.com` is **not** trusted for agent/health checks unless also added to `OUTBOUND_URL_ALLOWLIST`.

## 3. UX Tests

### 3.1 Error message clarity

Simulate a blocked agent registration:

```bash
cd "$REPO"
# Requires running registry; alternatively run unit test that asserts message text.
uv run python - <<'PY'
import asyncio
from registry.schemas.agent_models import AgentCard
from registry.utils.agent_validator import validate_agent_card
from registry.core.config import settings

settings.outbound_url_allowlist = ""
card = AgentCard(
    protocol_version="1.0",
    name="test",
    description="test",
    url="http://192.168.1.1/agent",
    path="/test",
    version="1",
)
result = validate_agent_card(card)
assert not result.is_valid
assert any("blocked by SSRF policy" in e for e in result.errors), result.errors
print("OK:", result.errors)
PY
```

Expected output: error message contains `blocked by SSRF policy` and guidance to use `OUTBOUND_URL_ALLOWLIST`.

### 3.2 Health status clarity

Verify that a blocked health check produces a distinguishable status:

```bash
cd "$REPO"
uv run pytest tests/unit/health/test_health_service.py::TestSSRF::test_status_contains_ssrf_prefix -v
```

Expected result: status string starts with `unhealthy: URL blocked by SSRF policy`.

## 4. Deployment Surface Tests

### 4.1 Docker Compose wiring

Verify the env var is passed through in all compose files:

```bash
cd "$REPO"
grep -n "OUTBOUND_URL_ALLOWLIST" docker-compose.yml docker-compose.prebuilt.yml docker-compose.podman.yml
```

Expected: each file contains `OUTBOUND_URL_ALLOWLIST=${OUTBOUND_URL_ALLOWLIST:-}` under the `registry` service.

### 4.2 Terraform / ECS wiring

Validate Terraform syntax:

```bash
cd "$REPO/terraform/aws-ecs"
terraform validate
```

Expected: no errors.

Verify the variable and env entry exist:

```bash
cd "$REPO"
grep -n "outbound_url_allowlist" terraform/aws-ecs/variables.tf terraform/aws-ecs/modules/mcp-gateway/variables.tf terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf terraform/aws-ecs/main.tf
```

Expected: variable declared in both `variables.tf`, passed in `main.tf`, rendered as `OUTBOUND_URL_ALLOWLIST` in `ecs-services.tf`.

### 4.3 Helm / EKS wiring

Run Helm unittests after adding the new value:

```bash
cd "$REPO"
helm dep update charts/mcp-gateway-registry-stack
helm unittest charts/registry charts/mcp-gateway-registry-stack
```

Expected: all existing tests pass and new tests (if added) for the env var pass.

Verify reserved name list:

```bash
cd "$REPO"
grep "OUTBOUND_URL_ALLOWLIST" charts/registry/reserved-env-names.txt
```

Expected: entry present.

### 4.4 Render Helm with allowlist

```bash
cd "$REPO"
helm template charts/registry \
  --set app.outboundUrlAllowlist="internal.example.com,api.corp.local" \
  | grep -A1 "OUTBOUND_URL_ALLOWLIST"
```

Expected: rendered Secret contains `OUTBOUND_URL_ALLOWLIST` with the supplied value.

### 4.5 Rollback verification

After deployment, unset `OUTBOUND_URL_ALLOWLIST` and verify behavior returns to deny-by-default for private IPs:

```bash
# In the deployment environment
unset OUTBOUND_URL_ALLOWLIST
# Restart registry
# Attempt to register agent with private URL -> expect 422
```

## 5. End-to-End API Tests

### 5.1 Full agent registration flow with SSRF block

Prerequisites: registry running locally with default config.

```bash
export REGISTRY_URL="http://localhost:8000"
export ACCESS_TOKEN="<valid token>"

# Attempt to register an agent pointing to a private IP
curl -s -X POST "$REGISTRY_URL/api/agents/register" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol_version": "1.0",
    "name": "ssrf-test",
    "description": "SSRF test agent",
    "url": "http://192.168.1.1/agent",
    "version": "1"
  }' | jq .
```

Expected: `422 Unprocessable Entity` with `errors` containing `blocked by SSRF policy`.

### 5.2 Allowlisted agent registration succeeds

```bash
# Set OUTBOUND_URL_ALLOWLIST=internal.example.com in registry env and restart
# Register an agent whose URL resolves to a private IP but matches the allowlist
curl -s -X POST "$REGISTRY_URL/api/agents/register" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol_version": "1.0",
    "name": "allowlisted-test",
    "description": "Allowlisted test agent",
    "url": "http://internal.example.com/agent",
    "version": "1"
  }' | jq .
```

Expected: `200 OK` or `201 Created`.

### 5.3 Health check blocks private proxy URL

Prerequisites: registry running, server registered with private `proxy_pass_url`.

```bash
# Register a server with a private proxy URL (this may be blocked at registration if validation is added to server routes)
# If server registration still allows private URLs, observe health status:
curl -s "$REGISTRY_URL/api/health/<server-path>" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | jq .
```

Expected: status is `unhealthy: URL blocked by SSRF policy`.

## 6. Test Execution Checklist

- [ ] Section 1.1 (shared utility unit tests) passes.
- [ ] Section 1.2 (agent validator unit tests) passes.
- [ ] Section 1.3 (health service unit tests) passes.
- [ ] Section 1.4 (skill service backwards-compatibility tests) passes.
- [ ] Section 2 (backwards compatibility) verified.
- [ ] Section 3 (UX/error message clarity) verified.
- [ ] Section 4.1 (Docker Compose) wiring verified.
- [ ] Section 4.2 (Terraform) validates and wires correctly.
- [ ] Section 4.3 (Helm) unittests pass.
- [ ] Section 4.4 (Helm render) shows env var.
- [ ] Section 5 (E2E) verified in a running environment.
- [ ] Unit tests added under `tests/unit/utils/test_ssrf.py`.
- [ ] Unit tests added/updated under `tests/unit/utils/test_agent_validator.py`.
- [ ] Unit tests added under `tests/unit/health/test_health_service.py`.
- [ ] `uv run pytest tests/unit/` passes with no regressions.
