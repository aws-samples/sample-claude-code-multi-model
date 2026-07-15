# Testing Plan: Remove FAISS, standardize on DocumentDB native hybrid search

*Created: 2026-07-15*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

## Overview

### Scope of Testing
This plan verifies that removing FAISS and routing all search through `DocumentDBSearchRepository` preserves externally observable search behavior (semantic search, tag search, tag enumeration, and CRUD-driven indexing) while eliminating every FAISS reference from the codebase, dependencies, build, and infrastructure. It also verifies the dependency change (`faiss-cpu` removed, `numpy` promoted to explicit) does not break imports.

### Prerequisites
- [ ] A DocumentDB or MongoDB-compatible instance is reachable (the conftest default is `mongodb-ce` on `localhost:27017`; see `tests/conftest.py:79`).
- [ ] `STORAGE_BACKEND` is set to a MongoDB variant (`mongodb-ce`, `documentdb`, `mongodb`, or `mongodb-atlas`) so `DocumentDBSearchRepository` is exercised.
- [ ] The embedding model is available (or tests fall back to lexical-only search, which is also valid).
- [ ] `uv` is installed for dependency resolution checks.
- [ ] The registry service can boot (`uv run python -m registry.main` or the project's standard launcher).
- [ ] A valid auth token / Nginx-proxied auth context for endpoints that require `nginx_proxied_auth`.

### Shared Variables
```bash
export REGISTRY_URL="http://localhost"
export STORAGE_BACKEND="mongodb-ce"
export DOCUMENTDB_HOST="localhost"
export DOCUMENTDB_PORT="27017"
export DOCUMENTDB_DATABASE="mcp_registry"
export DOCUMENTDB_USE_TLS="false"
# Auth token - obtain from the project's token helper (see get_asor_token.py / start_token_refresher.sh)
export ACCESS_TOKEN="$(python get_asor_token.py 2>/dev/null || echo 'REPLACE_WITH_VALID_TOKEN')"
# For Form-data endpoints (/api/register, /api/toggle, /api/internal/remove) which require
# UI/session auth + CSRF, obtain a session cookie and CSRF token from a logged-in browser session.
export SESSION_COOKIE="REPLACE_WITH_SESSION_COOKIE"
export CSRF_TOKEN="REPLACE_WITH_CSRF_TOKEN"
# Internal endpoints (/api/internal/register, /api/internal/remove) use internal admin auth.
export INTERNAL_AUTH_TOKEN="REPLACE_WITH_INTERNAL_ADMIN_TOKEN"
```

## 1. Functional Tests

### 1.1 curl / HTTP Tests

#### 1.1.1 Semantic search returns results for an indexed server
**Command:**
```bash
curl -sS -X POST "$REGISTRY_URL/api/search/semantic" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "web search", "max_results": 3}'
```
**Expected:** HTTP 200. Response JSON contains a `servers` array; a server whose description mentions "web search" appears with `relevance_score` in `[0, 1]`, a `match_context`, and a `matching_tools` array.
**Assertion:**
```bash
echo "$RESP" | jq -e '.servers | length > 0' >/dev/null
echo "$RESP" | jq -e '.servers[0].relevance_score | type == "number" and . >= 0 and . <= 1' >/dev/null
```
**Negative case:** query for a term that matches nothing:
```bash
curl -sS -X POST "$REGISTRY_URL/api/search/semantic" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "zzzNoSuchCapability12345"}'
```
**Expected:** HTTP 200 with empty `servers`, `tools`, `agents`, `skills`, `virtual_servers` arrays (not a 500).

#### 1.1.2 Tag-only search returns entities matching ALL tags
**Command:**
```bash
curl -sS -X POST "$REGISTRY_URL/api/search/semantic" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"tags": ["search", "web"], "max_results": 5}'
```
**Expected:** HTTP 200. Every returned server has both `search` and `web` in its `tags` (case-insensitive).
**Assertion:**
```bash
echo "$RESP" | jq -e '.servers[] | (.tags | map(ascii_downcase)) | index("search") and index("web")' >/dev/null
```
**Negative case:** a tag that does not exist:
```bash
curl -sS -X POST "$REGISTRY_URL/api/search/semantic" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"tags": ["nonexistent-tag-xyz"]}'
```
**Expected:** HTTP 200 with empty result arrays.

#### 1.1.3 Tag enumeration returns the sorted unique tag set
**Command:**
```bash
curl -sS "$REGISTRY_URL/api/search/tags" -H "Authorization: Bearer $ACCESS_TOKEN"
```
**Expected:** HTTP 200. Response is a JSON object with a `tags` array (or a bare array, depending on the response model) of unique tags, sorted case-insensitively.
**Assertion:**
```bash
echo "$RESP" | jq -e '(.tags // .) | type == "array" and length > 0' >/dev/null
```

#### 1.1.4 Disabled servers are excluded by default
**Command:** register a server, disable it, then search for its distinctive term.

> Note: `/api/register` and `/api/toggle/{path}` are **Form-data** endpoints (fields `name`, `description`, `path`, `proxy_pass_url`, `tags`, and Form `enabled`) protected by UI permission + CSRF. Use `curl -F` (multipart form) and supply a CSRF token / session cookie as the UI does. For a pure backend check, prefer the pytest harness in `tests/integration/test_search_integration.py` which handles auth/fixtures; the curl below is illustrative.

```bash
# Register (Form data; name field, not server_name)
curl -sS -X POST "$REGISTRY_URL/api/register" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Cookie: $SESSION_COOKIE" -H "X-CSRF-Token: $CSRF_TOKEN" \
  -F "name=Disabled Test" \
  -F "path=/test-disabled-srv" \
  -F "description=uniquephrase-disabled-test-xyz" \
  -F "proxy_pass_url=http://example.invalid/test-disabled-srv" \
  -F "tags=disabled-test"
# Disable (Form 'enabled' field on the toggle route)
curl -sS -X POST "$REGISTRY_URL/api/toggle/test-disabled-srv" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Cookie: $SESSION_COOKIE" -H "X-CSRF-Token: $CSRF_TOKEN" \
  -F "enabled=false"
# Search
curl -sS -X POST "$REGISTRY_URL/api/search/semantic" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "uniquephrase-disabled-test-xyz"}'
```
**Expected:** HTTP 200; the disabled server does NOT appear in results (status filter excludes it).
**Negative/override case:** with `include_disabled`:
```bash
curl -sS -X POST "$REGISTRY_URL/api/search/semantic" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "uniquephrase-disabled-test-xyz", "include_disabled": true}'
```
**Expected:** the disabled server appears.

### 1.2 CLI Tests

#### 1.2.1 `cli/agent_mgmt.py` search command (agent discovery via search)
**Invocation:**
```bash
uv run python -m cli.agent_mgmt search "weather" --max-results 3
```
**Expected output:** a list of agents matching the query, or an empty result with a clean message. Exit code 0.
**Assertion:** no `faiss` references in output or logs; results come from `DocumentDBSearchRepository`.

#### 1.2.2 `cli/service_mgmt.sh` no longer calls `verify_faiss_metadata`
**Invocation:**
```bash
grep -n "verify_faiss_metadata" cli/service_mgmt.sh terraform/aws-ecs/scripts/service_mgmt.sh
```
**Expected output:** no matches (exit code 1 from grep). The service-management lifecycle (enable/disable/remove) still succeeds:
```bash
./cli/service_mgmt.sh enable <service_name>
./cli/service_mgmt.sh disable <service_name>
```
**Expected:** exit code 0 for both; the service's enabled state is reflected in subsequent search results.

## 2. Backwards Compatibility Tests

### 2.1 Pre-change search request shape is still accepted
The `SemanticSearchRequest` schema is unchanged. Verify a client using the old payload (e.g. `query` + `max_results`, or `tags` alone) still gets a 200:
```bash
curl -sS -X POST "$REGISTRY_URL/api/search/semantic" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "file operations", "max_results": 3}'
```
**Expected:** HTTP 200; response shape contains the same top-level keys (`servers`, `tools`, `agents`, `skills`, `virtual_servers`) and the same per-entry fields as before the change.

### 2.2 `STORAGE_BACKEND=file` still boots (search routes to DocumentDB)
**Setup:** set `STORAGE_BACKEND=file` and provide a DocumentDB endpoint, then boot:
```bash
STORAGE_BACKEND=file DOCUMENTDB_HOST=localhost DOCUMENTDB_PORT=27017 \
  DOCUMENTDB_DATABASE=mcp_registry DOCUMENTDB_USE_TLS=false \
  uv run python -m registry.main
```
**Expected:** the service starts; the log line `Initializing DocumentDB search service` appears (no `FAISS` references, no in-memory re-index block). A subsequent `POST /api/search/semantic` returns 200 with results sourced from DocumentDB.

**Backwards-compat note (per review):** if the maintainer adopted the recommended `SEARCH_BACKEND`/explicit-DocumentDB fail-fast behavior, then `STORAGE_BACKEND=file` with no `DOCUMENTDB_HOST` should fail at startup with a clear message rather than silently defaulting to `localhost:27017`. Verify whichever behavior the final LLD specifies:
```bash
STORAGE_BACKEND=file DOCUMENTDB_HOST="" uv run python -m registry.main
```
**Expected (fail-fast variant):** startup exits non-zero with a message naming DocumentDB as required for search. **Expected (graceful variant):** startup succeeds, search returns empty results with a logged warning.

### 2.3 Server/agent CRUD response shapes unchanged
Register, update, disable, and remove a server; register and remove an agent. Confirm each endpoint returns the same status code and body shape as before the change (indexing now happens via `get_search_repository()` but the HTTP contract is unchanged).

> Note: `/api/register` and `/api/internal/remove` are **Form-data** endpoints (`name`/`description`/`path`/`proxy_pass_url` for register; `service_path` for remove) protected by UI/internal auth + CSRF. Use `curl -F`. The authoritative check is the pytest harness; the curl is illustrative.
```bash
curl -sS -X POST "$REGISTRY_URL/api/register" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Cookie: $SESSION_COOKIE" -H "X-CSRF-Token: $CSRF_TOKEN" \
  -F "name=BC Test" -F "path=/bc-srv" -F "description=backwards compat server" \
  -F "proxy_pass_url=http://example.invalid/bc-srv"
curl -sS -X POST "$REGISTRY_URL/api/internal/remove" \
  -H "X-Internal-Auth: $INTERNAL_AUTH_TOKEN" \
  -F "service_path=/bc-srv"
```
**Expected:** 2xx for register; success for remove. The server appears in, then disappears from, search results.

### 2.4 Metrics field rename does not break existing metrics consumers
After the `faiss_search_time_ms` -> `search_time_ms` rename, confirm the metrics-service still records tool-discovery metrics. Query the metrics DB:
```bash
# After triggering a search (1.1.1), inspect the metrics-service SQLite store
sqlite3 /path/to/metrics.db "SELECT search_time_ms FROM metrics WHERE metric_type='tool_discovery' ORDER BY id DESC LIMIT 1;"
```
**Expected:** a non-null numeric value (the additive migration backfilled from `faiss_search_time_ms` if present). If the rename was deferred per the LLD's open question, the column is still `faiss_search_time_ms` and the query should use that name; either way the value is present.

## 3. UX Tests

### 3.1 Search results render correctly in the web UI
Open the registry web UI, perform a natural-language search and a tag search. Confirm:
- Results render with name, description, tags, and relevance ordering.
- Matching tools are displayed under each server (count may differ from FAISS due to the DocumentDB tool soft-cap; verify the list is non-empty when tools match).
- No error toasts or "search unavailable" banners appear.

### 3.2 CLI output and error messages contain no FAISS references
```bash
uv run python -m cli.agent_mgmt search "anything" 2>&1 | grep -i faiss
./cli/service_mgmt.sh status 2>&1 | grep -i faiss
```
**Expected:** no matches. Error messages (e.g. searching with no query) are clear and reference "search" generically, not FAISS.

### 3.3 Tool description for `intelligent_tool_finder` reads correctly
Inspect `registry/servers/mcpgw.json`:
```bash
grep -i "faiss" registry/servers/mcpgw.json
```
**Expected:** no matches. The `intelligent_tool_finder` description, args, and raises text reference "semantic/hybrid search" instead of FAISS. Confirm an AI agent consuming the tool still receives a valid schema (the JSON structure is unchanged - only prose changed).

## 4. Deployment Surface Tests

### 4.1 Docker wiring
**Command:**
```bash
grep -rin "faiss" Dockerfile docker-compose.yml docker-compose.prebuilt.yml docker-compose.podman.yml
```
**Expected:** no matches. Build the image and confirm it does not install `faiss-cpu`:
```bash
docker build -t mcp-gateway-registry:test .
docker run --rm mcp-gateway-registry:test python -c "import faiss" 2>&1 | grep -i "ModuleNotFoundError"
```
**Expected:** `ModuleNotFoundError: No module named 'faiss'` (FAISS is genuinely absent). Then confirm the app still imports:
```bash
docker run --rm mcp-gateway-registry:test python -c "import registry.embeddings.client; print('numpy ok')"
```
**Expected:** `numpy ok` (the explicit `numpy` dependency resolves).

### 4.2 Terraform / ECS wiring
**Command:**
```bash
grep -rin "faiss" terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf \
  terraform/aws-ecs/scripts/service_mgmt.sh terraform/aws-ecs/OPERATIONS.md
```
**Expected:** no matches. Validate the Terraform still plans:
```bash
cd terraform/aws-ecs && terraform init -backend=false && terraform validate
```
**Expected:** `Success! The configuration is valid.`

### 4.3 Helm / EKS wiring
**Not Applicable** - the repository uses Terraform for ECS deployment and Docker Compose for local/prebuilt deployments; no Helm/EKS chart was found that references FAISS. (If a Helm chart is added later, run `grep -rin faiss charts/`.)

### 4.4 Deploy and verify
Deploy to a staging environment with `STORAGE_BACKEND=mongodb-ce` (or `documentdb`). After deploy:
- [ ] `/api/search/tags` returns 200 with a non-empty tag list.
- [ ] `POST /api/search/semantic` returns 200 for a known-good query.
- [ ] Startup logs contain `Initializing DocumentDB search service` and **no** `FAISS` lines.
- [ ] Image size is smaller than the pre-change image (FAISS native lib removed); record the new size in `OPERATIONS.md`.

### 4.5 Rollback verification
Revert the commit on staging. Confirm:
- [ ] The service boots with FAISS re-enabled.
- [ ] The DocumentDB `mcp_embeddings_<dim>` collection is still populated (embeddings persisted across the revert window; no search data loss).
- [ ] Search returns results immediately on rollback (no full re-index delay beyond the normal FAISS boot re-index).

## 5. End-to-End API Tests

### 5.1 Register -> index -> search -> disable -> search -> remove -> search (server)

> Note: register/toggle/remove are **Form-data** endpoints with UI/internal auth + CSRF. Use `curl -F` and supply `$SESSION_COOKIE` / `$CSRF_TOKEN` / `$INTERNAL_AUTH_TOKEN` (see `tests/integration/test_search_integration.py` for the fixture-based equivalent, which is the authoritative path). The search calls are bearer-token JSON.

```bash
# 1. Register a server with distinctive text (Form data; field is 'name', tags comma-separated)
curl -sS -X POST "$REGISTRY_URL/api/register" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Cookie: $SESSION_COOKIE" -H "X-CSRF-Token: $CSRF_TOKEN" \
  -F "name=E2E Srv" -F "path=/e2e-srv" \
  -F "description=end to end e2e-unique-phrase-xyz" \
  -F "proxy_pass_url=http://example.invalid/e2e-srv" -F "tags=e2e"

# 2. Search for it by natural language
RESP=$(curl -sS -X POST "$REGISTRY_URL/api/search/semantic" -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "e2e-unique-phrase-xyz"}')
echo "$RESP" | jq -e '.servers[] | select(.path == "/e2e-srv")' >/dev/null   # MUST find it

# 3. Search for it by tag
RESP=$(curl -sS -X POST "$REGISTRY_URL/api/search/semantic" -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"tags": ["e2e"]}')
echo "$RESP" | jq -e '.servers[] | select(.path == "/e2e-srv")' >/dev/null   # MUST find it

# 4. Disable it (Form 'enabled' field on the toggle route)
curl -sS -X POST "$REGISTRY_URL/api/toggle/e2e-srv" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Cookie: $SESSION_COOKIE" -H "X-CSRF-Token: $CSRF_TOKEN" \
  -F "enabled=false"

# 5. Search again (default excludes disabled)
RESP=$(curl -sS -X POST "$REGISTRY_URL/api/search/semantic" -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "e2e-unique-phrase-xyz"}')
echo "$RESP" | jq -e '(.servers // []) | all(.path != "/e2e-srv")' >/dev/null  # MUST NOT find it

# 6. Remove it (internal endpoint; Form 'service_path'; internal auth header)
curl -sS -X POST "$REGISTRY_URL/api/internal/remove" \
  -H "X-Internal-Auth: $INTERNAL_AUTH_TOKEN" \
  -F "service_path=/e2e-srv"

# 7. Search again (must not find it, even with include_disabled)
RESP=$(curl -sS -X POST "$REGISTRY_URL/api/search/semantic" -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "e2e-unique-phrase-xyz", "include_disabled": true}')
echo "$RESP" | jq -e '(.servers // []) | all(.path != "/e2e-srv")' >/dev/null  # MUST NOT find it
```
**Expected:** every assertion passes. This proves the full CRUD->index->search lifecycle works end-to-end through `get_search_repository()`.

### 5.2 Register -> search -> remove (agent)

> Note: agent register is a JSON body (`AgentRegistrationRequest`: `name`, `url`, `description`, `path`, `tags`); agent delete is `DELETE /api/agents/{path:path}`. Both use bearer auth (`nginx_proxied_auth`). Confirmed against `registry/api/agent_routes.py:439` and `:1783`.

```bash
curl -sS -X POST "$REGISTRY_URL/api/agents/register" -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"path": "/e2e-agent", "name": "E2E Agent", "url": "http://example.invalid/e2e-agent", "description": "agent e2e-unique-agent-phrase", "tags": ["e2e-agent"]}'
RESP=$(curl -sS -X POST "$REGISTRY_URL/api/search/semantic" -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "e2e-unique-agent-phrase"}')
echo "$RESP" | jq -e '.agents[] | select(.path == "/e2e-agent")' >/dev/null
curl -sS -X DELETE "$REGISTRY_URL/api/agents/e2e-agent" -H "Authorization: Bearer $ACCESS_TOKEN"
```
**Expected:** the agent is found after registration and absent after a subsequent search (removal from the search index happens via `get_search_repository().remove_entity`).

### 5.3 Before/after query parity (manual, per review)
Because the default test suite runs `STORAGE_BACKEND=mongodb-ce` (conftest.py:79), there are no existing tests comparing FAISS output to DocumentDB output. Capture a before snapshot (on the pre-change `file`+FAISS build) and an after snapshot (on the post-change DocumentDB build) for 3-5 representative queries:
```bash
for Q in "web search" "file operations" "weather forecast" "database query" "image processing"; do
  echo "=== $Q ==="
  curl -sS -X POST "$REGISTRY_URL/api/search/semantic" -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
    -d "{\"query\": \"$Q\", \"max_results\": 3}" | jq '[.servers[] | {path, relevance_score}]'
done > "parity-$(date +%s).json"
```
**Expected:** the **set of matched paths** is equivalent before and after (order and numeric scores may differ - that is an acknowledged behavioral delta, not a failure).

## 6. Test Execution Checklist

- [ ] Section 1 (Functional) passes - semantic search, tag search, tag enumeration, disabled-exclusion.
- [ ] Section 2 (Backwards Compat) verified - old request shapes accepted; `STORAGE_BACKEND=file` boots (or fails fast per final design); CRUD shapes unchanged; metrics field present.
- [ ] Section 3 (UX) verified - web UI renders results; CLI output has no FAISS references; tool description updated.
- [ ] Section 4 (Deployment) verified - `grep -ri faiss` empty in Docker/Terraform; image builds without `faiss-cpu`; `numpy` imports; image size reduced; rollback restores search.
- [ ] Section 5 (E2E) verified - server and agent register->search->remove cycles pass; before/after parity captured.
- [ ] `grep -ri faiss` against the entire repository (excluding `.git/`) returns zero hits:
  ```bash
  grep -ri faiss . --exclude-dir=.git | wc -l   # MUST be 0
  ```
- [ ] `uv lock --check` passes (lockfile in sync with `pyproject.toml`):
  ```bash
  uv lock --check
  ```
- [ ] `faiss-cpu` is absent from the resolved environment:
  ```bash
  ! uv pip list 2>/dev/null | grep -i faiss-cpu
  ```
- [ ] `numpy` is present and importable:
  ```bash
  uv run python -c "import numpy; print(numpy.__version__)"
  ```
- [ ] Unit tests pass (with the FAISS suite deleted and remaining tests updated):
  ```bash
  uv run pytest tests/unit -q
  ```
- [ ] Integration tests pass (against `STORAGE_BACKEND=mongodb-ce`):
  ```bash
  uv run pytest tests/integration -q
  ```
- [ ] Full suite passes with no regressions:
  ```bash
  uv run pytest tests/ -q
  ```
