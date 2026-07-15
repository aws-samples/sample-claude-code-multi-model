# Testing Plan: Migrate ECS Environment Variables to AWS Secrets Manager

*Created: 2026-07-15*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

## Overview

### Scope of Testing
This plan verifies that every sensitive ECS environment variable across the registry, auth-server, keycloak, metrics-service, and mcpgw services is backed by an AWS Secrets Manager secret, injected via the ECS `secrets` block, readable by the application through the new env-var-first fallback resolver, and that the migration is backward compatible (a plaintext env var still wins) and non-disruptive to existing deployments. It covers Terraform validation/planning, resolver unit tests, backwards-compatibility tests, deployment-surface wiring, and an end-to-end ECS deploy.

Per the skill constraints, no commands are executed against the cloned `repo/` during design. The commands below are for the future implementer to run.

### Prerequisites
- [ ] Local clone of `mcp-gateway-registry` at tag `1.24.4` with the implementation applied.
- [ ] Terraform >= 1.x installed.
- [ ] `uv` installed; project dependencies installed (`uv sync`).
- [ ] An AWS account with permissions to run `terraform plan` (read-only plan tests do not require apply).
- [ ] A representative `terraform.tfvars` with the sensitive variables populated (the values themselves do not matter for plan-diff assertions; only that they are non-empty).
- [ ] `boto3` available in the test environment (already a project dependency).

### Shared Variables

```bash
# Repo root of the cloned target (set by the implementer)
REPO_ROOT="$(git rev-parse --show-toplevel)"

# Terraform module path
TF_DIR="$REPO_ROOT/terraform/aws-ecs"

# A representative tfvars file for plan tests
TF_VARS_FILE="$TF_DIR/terraform.tfvars"

# Service names under test
SERVICES="registry auth-server keycloak metrics-service mcpgw"
```

---

## 1. Functional Tests

### 1.1 Terraform: secrets exist and are referenced

These are `terraform plan` / `terraform console` assertions, not apply tests. They verify the wiring described in the LLD.

**1.1.1 Every migrated secret resource is planned**

```bash
cd "$TF_DIR"
terraform init -backend=false
terraform plan -out=/tmp/migrate.tfplan -var-file="$TF_VARS_FILE"
terraform show -json /tmp/migrate.tfplan > /tmp/migrate-plan.json
```

Assertions (each must print a resource address; none may be empty):

```bash
# Each migrated secret must appear in the planned resource changes.
for secret in \
  aws_secretsmanager_secret.registry_api_token \
  aws_secretsmanager_secret.registry_api_keys \
  aws_secretsmanager_secret.federation_static_token \
  aws_secretsmanager_secret.federation_encryption_key \
  aws_secretsmanager_secret.auth0_management_api_token \
  aws_secretsmanager_secret.ans_api_key \
  aws_secretsmanager_secret.ans_api_secret \
  aws_secretsmanager_secret.github_pat \
  aws_secretsmanager_secret.github_app_private_key \
  aws_secretsmanager_secret.registration_webhook_auth_token \
  aws_secretsmanager_secret.registration_gate_auth_credential \
  aws_secretsmanager_secret.registration_gate_oauth2_client_secret ; do
    count=$(jq --arg s "$secret" '[.resource_changes[] | select(.address=="module.mcp-gateway."+$s)] | length' /tmp/migrate-plan.json)
    echo "$secret -> $count"
    test "$count" -ge 1
done
echo "PASS: all migrated secrets are planned"
```

**Expected:** every secret prints `-> 1` (or more if count-gated and enabled); the loop exits 0.

**1.1.2 Every migrated value is in the `secrets` block, not the `environment` block**

This is validated by rendering the task definition and asserting no migrated name appears in `environment` while each appears in `secrets`. Because the module uses `jsonencode`-free object form via the `terraform-aws-modules/ecs` module, inspect the rendered container definitions:

```bash
# Render the registry task definition container definitions to JSON.
terraform plan -out=/tmp/migrate.tfplan -var-file="$TF_VARS_FILE"
# Use terraform console to read the container_definitions object for the registry service.
# (Adjust the module path to match the root module wiring.)
terraform show -json /tmp/migrate.tfplan > /tmp/migrate-plan.json

# Extract planned container_definitions for the registry container and check placement.
python3 - <<'PY'
import json, sys
plan = json.load(open("/tmp/migrate-plan.json"))
migrated = [
    "REGISTRY_API_TOKEN","REGISTRY_API_KEYS","FEDERATION_STATIC_TOKEN",
    "FEDERATION_ENCRYPTION_KEY","AUTH0_MANAGEMENT_API_TOKEN","ANS_API_KEY",
    "ANS_API_SECRET","GITHUB_PAT","GITHUB_APP_PRIVATE_KEY",
    "REGISTRATION_WEBHOOK_AUTH_TOKEN","REGISTRATION_GATE_AUTH_CREDENTIAL",
    "REGISTRATION_GATE_OAUTH2_CLIENT_SECRET",
]
# Find the registry container_definitions in the planned changes (implementation-dependent path).
# The implementer locates the module.mcp-gateway module.ecs_service_registry container_definitions.
# This script is a template; adapt the JSON traversal to the actual rendered structure.
print("Implementer: traverse /tmp/migrate-plan.json to the registry container_definitions,")
print("then assert each migrated name is in 'secrets' and NOT in 'environment'.")
PY
```

Implementer note: the concrete traversal depends on how the root module wires `module.mcp-gateway`. The assertion logic is:

```python
for name in migrated:
    env_names  = [e["name"] for e in container["environment"]]
    sec_names  = [s["name"] for s in container["secrets"]]
    assert name not in env_names, f"{name} still in environment block"
    assert name in sec_names,    f"{name} missing from secrets block"
```

Repeat for the auth-server container (subset: `REGISTRY_API_TOKEN`, `REGISTRY_API_KEYS`, `FEDERATION_STATIC_TOKEN`, `FEDERATION_ENCRYPTION_KEY`, `AUTH0_MANAGEMENT_API_TOKEN`, `ANS_API_KEY`, `ANS_API_SECRET`).

**Expected:** all assertions pass; no migrated name remains in `environment`; every migrated name is in `secrets`.

**Negative case:** pick a non-sensitive env var that should stay in `environment` (e.g. `BIND_HOST`, `AWS_REGION`) and assert it is NOT in `secrets`:

```python
assert "AWS_REGION" in env_names and "AWS_REGION" not in sec_names
```

**1.1.3 IAM policy lists every new secret ARN**

```bash
# Render the ecs_secrets_access policy document and assert each new ARN is present.
python3 - <<'PY'
import json
# The policy is jsonencode({...}) in module.mcp-gateway.aws_iam_policy.ecs_secrets_access.
# Implementer: extract the planned policy JSON, then:
policy = {...}  # from the plan
allowed = policy["Statement"][0]["Resource"]
new_arns = [
    "registry_api_token","registry_api_keys","federation_static_token",
    "federation_encryption_key","ans_api_key","ans_api_secret","github_pat",
    "github_app_private_key","registration_webhook_auth_token",
    "registration_gate_auth_credential","registration_gate_oauth2_client_secret",
]
# Each new secret's ARN fragment must appear in the allowed list.
joined = " ".join(allowed)
for frag in new_arns:
    assert frag in joined or f"{frag}[0]" in joined, f"{frag} ARN missing from ecs_secrets_access"
print("PASS: all new secret ARNs in ecs_secrets_access")
PY
```

**Negative case:** assert mcpgw-only or unrelated ARNs are NOT over-granted beyond the existing posture (per Cipher's review, mcpgw should not gain access to the new app secrets it does not consume). If per-service splitting is deferred, document the accepted over-grant here.

### 1.2 CLI / Resolver Unit Tests

These are `pytest` tests added under `tests/unit/`. They do not require AWS.

**1.2.1 Resolver: env var present wins**

```python
# tests/unit/test_secrets_loader.py
import os
from unittest.mock import patch
from registry.core import secrets_loader

def test_plaintext_env_var_takes_precedence(monkeypatch):
    monkeypatch.setenv("REGISTRY_API_TOKEN", "plaintext-value")
    monkeypatch.setenv("REGISTRY_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:regtoken-xxx")
    with patch.object(secrets_loader, "_fetch_secret_value") as mock_fetch:
        result = secrets_loader.get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN")
    assert result == "plaintext-value"
    mock_fetch.assert_not_called()  # ARN path must not run when env var is present
```

**Expected:** `plaintext-value` returned; no boto3 call made.

**1.2.2 Resolver: ARN fallback when env var absent**

```python
def test_arn_fallback_when_env_var_absent(monkeypatch):
    monkeypatch.delenv("REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("REGISTRY_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:regtoken-xxx")
    secrets_loader._fetch_secret_value.cache_clear()
    with patch.object(secrets_loader, "_fetch_secret_value", return_value="secret-value") as mock_fetch:
        result = secrets_loader.get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN")
    assert result == "secret-value"
    mock_fetch.assert_called_once_with("arn:aws:secretsmanager:us-east-1:111:secret:regtoken-xxx")
```

**Expected:** `secret-value` returned; exactly one fetch by ARN.

**1.2.3 Resolver: both absent returns None**

```python
def test_both_absent_returns_none(monkeypatch):
    monkeypatch.delenv("REGISTRY_API_TOKEN", raising=False)
    monkeypatch.delenv("REGISTRY_API_TOKEN_SECRET_ARN", raising=False)
    assert secrets_loader.get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN") is None
```

**1.2.4 Resolver: fetch failure returns None, does not raise**

```python
def test_fetch_failure_returns_none(monkeypatch, caplog):
    monkeypatch.delenv("REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("REGISTRY_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:bad")
    secrets_loader._fetch_secret_value.cache_clear()
    import botocore.exceptions
    with patch("boto3.client") as mock_client:
        mock_client.return_value.get_secret_value.side_effect = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")
        result = secrets_loader.get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN")
    assert result is None  # must not raise
    assert any("Failed to fetch secret" in r.getMessage() for r in caplog.records)
```

**1.2.5 Resolver: results are cached (one fetch per ARN)**

```python
def test_cache_single_fetch_per_arn(monkeypatch):
    monkeypatch.delenv("REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("REGISTRY_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:regtoken-xxx")
    secrets_loader._fetch_secret_value.cache_clear()
    with patch.object(secrets_loader, "_fetch_secret_value", return_value="secret-value") as mock_fetch:
        secrets_loader.get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN")
        secrets_loader.get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN")
    assert mock_fetch.call_count == 1
```

**1.2.6 Resolver: master switch disables fetch**

```python
def test_resolver_disabled_skips_fetch(monkeypatch):
    monkeypatch.setenv("MCP_SECRETS_RESOLVER_ENABLED", "false")
    # Re-import to pick up the switch (or refactor to read it lazily).
    import importlib
    from registry.core import secrets_loader as sl
    importlib.reload(sl)
    monkeypatch.delenv("REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("REGISTRY_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:regtoken-xxx")
    with patch("boto3.client") as mock_client:
        assert sl.get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN") is None
        mock_client.assert_not_called()
```

**1.2.7 Duplication guardrail (Byte/Sage review)**

```python
def test_resolver_copies_are_byte_identical():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]  # adjust to repo root
    a = (root / "registry" / "core" / "secrets_loader.py").read_text()
    b = (root / "auth_server" / "secrets_loader.py").read_text()
    assert a == b, "registry and auth_server resolver copies have drifted"
```

Run:

```bash
uv run pytest tests/unit/test_secrets_loader.py -v
```

**Expected:** all 7 tests pass.

---

## 2. Backwards Compatibility Tests

These verify pre-change behavior is preserved during the migration window (Q6).

**2.1 Plaintext env var still drives the setting (no `*_SECRET_ARN` set)**

```python
def test_registry_settings_use_plaintext_when_no_arn(monkeypatch):
    monkeypatch.setenv("REGISTRY_API_TOKEN", "legacy-token")
    monkeypatch.delenv("REGISTRY_API_TOKEN_SECRET_ARN", raising=False)
    from registry.core.config import Settings
    s = Settings()
    assert s.registry_api_token == "legacy-token"
```

**Expected:** `legacy-token` - identical to pre-migration behavior.

**2.2 Plaintext env var wins even when ARN is also set**

```python
def test_plaintext_wins_over_secret(monkeypatch):
    monkeypatch.setenv("REGISTRY_API_TOKEN", "legacy-token")
    monkeypatch.setenv("REGISTRY_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:regtoken-xxx")
    from registry.core import secrets_loader
    secrets_loader._fetch_secret_value.cache_clear()
    with patch.object(secrets_loader, "_fetch_secret_value", return_value="secret-token") as mock_fetch:
        from registry.core.config import Settings
        s = Settings()
    assert s.registry_api_token == "legacy-token"
    mock_fetch.assert_not_called()
```

**2.3 CLI / service starts with only plaintext vars (no ARNs, no Secrets Manager)**

Simulates an unmigrated surface (e.g. local Docker Compose without the new vars).

```bash
# Start the registry with only legacy plaintext env vars set; no *_SECRET_ARN.
unset $(env | sed -n 's/^\([^=]*_SECRET_ARN\)=.*/\1/p')
export REGISTRY_API_TOKEN="legacy-token"
export FEDERATION_ENCRYPTION_KEY="legacy-key"
# ... other required non-secret env vars ...
uv run python -m registry.main &
sleep 10
curl -fsS http://localhost:7860/health
# Expected: HTTP 200; service boots exactly as before the migration.
```

**2.4 Terraform plan with old tfvars (no ARN vars) still succeeds**

```bash
# A tfvars file that does NOT set any *_SECRET_ARN variable must still plan cleanly,
# because all *_SECRET_ARN vars default to "".
cd "$TF_DIR"
terraform plan -var-file=terraform.tfvars.legacy -out=/tmp/legacy.tfplan
# Expected: exit 0; no errors about missing *_SECRET_ARN vars.
```

---

## 3. UX Tests

### 3.1 CLI output / error message clarity

**Not directly applicable** - this change adds no CLI command and no user-facing error strings. The only UX-adjacent surface is startup logging.

**3.1.1 Startup log identifies resolver source counts (no secret values)**

```bash
# Start the registry with a mix: some plaintext, some from Secrets Manager.
# Inspect the startup log for the INFO line described in the LLD.
uv run python -m registry.main 2>&1 | tee /tmp/registry-startup.log
grep -E "resolved .* secrets? from Secrets Manager" /tmp/registry-startup.log
grep -E "resolved .* secrets? from plaintext env" /tmp/registry-startup.log
# Expected: both lines present; no secret values appear anywhere in the log.
# Negative: assert no secret value leaks.
! grep -E "legacy-token|secret-token" /tmp/registry-startup.log
```

**3.1.2 Fetch failure is logged with the ARN, not the value**

```bash
# Force a fetch failure (bad ARN) and confirm the log shows the ARN, not a value.
export REGISTRY_API_TOKEN_SECRET_ARN="arn:aws:secretsmanager:us-east-1:111:secret:does-not-exist"
unset REGISTRY_API_TOKEN
uv run python -m registry.main 2>&1 | tee /tmp/registry-fail.log
grep "does-not-exist" /tmp/registry-fail.log   # ARN is logged
! grep -E "<actual-secret-value>" /tmp/registry-fail.log
```

---

## 4. Deployment Surface Tests

### 4.1 Docker wiring

**4.1.1 New variables present (commented/empty) in docker-compose**

```bash
# The new *_SECRET_ARN vars and MCP_SECRETS_RESOLVER_ENABLED should be declared
# (empty by default) in the compose env so local dev can opt in.
grep -rE "REGISTRY_API_TOKEN_SECRET_ARN|MCP_SECRETS_RESOLVER_ENABLED" "$REPO_ROOT/.env.example" "$REPO_ROOT/docker-compose.yml"
# Expected: at least one match per file (or a documented reason for omission).
```

**4.1.2 Compose stack starts with no ARNs set**

```bash
# With no *_SECRET_ARN set and legacy plaintext vars in .env, the stack must start.
./build_and_run.sh
docker compose ps   # all services healthy
```

### 4.2 Terraform / ECS wiring

**4.2.1 `terraform validate`**

```bash
cd "$TF_DIR"
terraform validate
# Expected: "Success! The configuration is valid."
```

**4.2.2 No plaintext secret value in the plan diff**

```bash
terraform plan -out=/tmp/migrate.tfplan -var-file="$TF_VARS_FILE" 2>&1 | tee /tmp/plan-diff.txt
# For each sensitive value actually present in tfvars, assert it does NOT appear in the diff.
for secret_val in $(grep -oE '^[A-Z_]+_TOKEN|^[A-Z_]+_KEY|^[A-Z_]+_SECRET' "$TF_VARS_FILE" | sort -u); do
    :
done
# Concrete: take a known tfvars value, e.g. REGISTRY_API_TOKEN="supersecret-123".
! grep -F "supersecret-123" /tmp/plan-diff.txt
echo "PASS: plaintext secret value absent from plan diff"
```

**4.2.3 Sensitive variables marked `sensitive = true`**

```bash
for v in registry_api_token registry_api_keys federation_static_token \
         federation_encryption_key auth0_management_api_token ans_api_key \
         ans_api_secret github_pat github_app_private_key \
         registration_webhook_auth_token registration_gate_auth_credential \
         registration_gate_oauth2_client_secret ; do
    grep -A4 "variable \"$v\"" "$TF_DIR/modules/mcp-gateway/variables.tf" | grep -q "sensitive = true" \
        && echo "$v: sensitive" || echo "$v: NOT sensitive (FAIL)"
done
```

**4.2.4 IAM policy applies before task definition (deployment ordering)**

```bash
# Assert a depends_on or explicit ordering so the exec role can read the secret
# before the new task definition revision starts.
grep -n "depends_on" "$TF_DIR/modules/mcp-gateway/ecs-services.tf" | head
# Expected: the service module references aws_iam_policy.ecs_secrets_access
# (already attached via task_exec_iam_role_policies), OR a depends_on is added.
```

### 4.3 Helm / EKS wiring

**Not Applicable** - Helm/EKS parity is explicitly out of scope for this issue (see github-issue.md Out of Scope).

### 4.4 Deploy and verify

**4.4.1 Staging apply is non-disruptive**

```bash
cd "$TF_DIR"
terraform apply /tmp/migrate.tfplan
# Observe: secrets created from current tfvars values; new task definition revision registered;
# services perform a rolling deployment; no tasks fail to start.
aws ecs describe-services --cluster <cluster> --services <name-prefix>-registry <name-prefix>-auth \
  --query 'services[].{name:serviceName, running:runningCount, desired:desiredCount, pending:pendingCount}'
# Expected: runningCount == desiredCount; pendingCount == 0; no FAILED deployments.
```

**4.4.2 Injected env var resolves to the secret value inside the container**

```bash
# ECS Exec into a running registry task and confirm the env var is populated.
TASK_ARN=$(aws ecs list-tasks --cluster <cluster> --family <name-prefix>-registry --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster <cluster> --task "$TASK_ARN" --container registry --interactive \
  --command 'sh -c "echo REGISTRY_API_TOKEN is set: ${REGISTRY_API_TOKEN:+yes}"'
# Expected: "REGISTRY_API_TOKEN is set: yes" (value never printed).
```

**4.4.3 CloudTrail records GetSecretValue from the task role**

```bash
# Confirm the task exec role fetched the secret (audit trail).
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=GetSecretValue \
  --max-results 20 --query 'Events[].{user:Username, time:EventTime}' --output table
# Expected: GetSecretValue events from the ECS task exec role within the deploy window.
```

### 4.5 Rollback verification

**4.5.1 Rollback to the previous task definition**

```bash
# Roll the service back to the prior task definition revision (pre-migration).
aws ecs update-service --cluster <cluster> --service <name-prefix>-registry \
  --task-definition <prior-revision-arn> --force-new-deployment
# Expected: services stabilize on the old revision; plaintext env vars are present again;
# the app works because the plaintext path is still supported.
```

**4.5.2 Destroying a secret does not strand the service (recovery window)**

```bash
# If recovery_window_in_days=0 is kept, confirm that a terraform destroy of a single
# secret is immediately followed by a service that can no longer resolve that value
# (documented behavior). If recovery_window_in_days=7 is chosen, confirm the secret
# enters the pending-deletion window and the app continues to resolve it for 7 days.
# This test documents the chosen recovery-window behavior from Circuit's review.
```

---

## 5. End-to-End API Tests

These exercise full business workflows that depend on the migrated secrets.

**5.1 Registry static-token auth (uses `REGISTRY_API_TOKEN` / `REGISTRY_API_KEYS`)**

```bash
export REGISTRY_URL="https://<domain>"
# With the token sourced from Secrets Manager, authenticate against a static-token endpoint.
curl -fsS -H "Authorization: Bearer $REGISTRY_API_TOKEN" "$REGISTRY_URL/api/v1/servers" | jq '.servers | length'
# Expected: 200 OK with a server list (proves the migrated token was resolved and accepted).
```

**5.2 Federation token validation (uses `FEDERATION_ENCRYPTION_KEY` / `FEDERATION_STATIC_TOKEN`)**

```bash
# Exercise a federated registry sync call that validates a peer token signed/encrypted
# with FEDERATION_ENCRYPTION_KEY. Exact endpoint depends on the federation test harness.
curl -fsS -H "Authorization: Bearer $FEDERATION_STATIC_TOKEN" "$REGISTRY_URL/api/v1/federation/verify"
# Expected: 200 OK; the encryption key was resolved from Secrets Manager and used successfully.
```

**5.3 GitHub skill doc fetch (uses `GITHUB_PAT` / `GITHUB_APP_PRIVATE_KEY`)**

```bash
# Register a server whose SKILL.md lives in a private GitHub repo, then fetch the rendered doc.
curl -fsS -H "Authorization: Bearer $REGISTRY_API_TOKEN" \
  "$REGISTRY_URL/api/v1/servers/<server-id>/skill.md" | head -5
# Expected: skill doc content (proves the GitHub credential was resolved from Secrets Manager).
```

**5.4 Auth0 management operation (uses `AUTH0_MANAGEMENT_API_TOKEN`, gated on auth0_enabled)**

```bash
# Only when auth0_enabled. Trigger an auth-server path that calls the Auth0 Management API
# (e.g. group sync) and confirm it succeeds.
curl -fsS -H "Authorization: Bearer $USER_TOKEN" "$REGISTRY_URL/api/v1/auth/groups/sync" 
# Expected: 200 OK; Auth0 management token resolved from Secrets Manager.
```

**5.5 Multi-step: registration webhook (uses `REGISTRATION_WEBHOOK_AUTH_TOKEN`)**

```bash
# Register a server with a webhook configured; confirm the webhook fires with the migrated
# auth token.
curl -fsS -X POST -H "Authorization: Bearer $REGISTRY_API_TOKEN" \
  -d @new-server.json "$REGISTRY_URL/api/v1/servers"
# Expected: 201; the configured webhook receives a request signed with the migrated token.
```

---

## 6. Test Execution Checklist

- [ ] Section 1.1 (Terraform functional / wiring) passes - every secret planned, every migrated name in `secrets` not `environment`, IAM ARNs present.
- [ ] Section 1.2 (resolver unit tests, 7 cases) passes.
- [ ] Section 2 (Backwards Compat) verified - plaintext wins, legacy tfvars plans, stack starts without ARNs.
- [ ] Section 3 (UX) verified - startup logs source counts, no secret values leak, failures log ARN only.
- [ ] Section 4.1 (Docker) verified - new vars present, stack starts.
- [ ] Section 4.2 (Terraform/ECS) verified - validate, plan diff clean, sensitive flags, ordering.
- [ ] Section 4.3 (Helm/EKS) marked Not Applicable.
- [ ] Section 4.4 (Deploy) verified - non-disruptive apply, env var resolves in-container, CloudTrail records GetSecretValue.
- [ ] Section 4.5 (Rollback) verified - rollback to prior revision works; recovery-window behavior documented.
- [ ] Section 5 (E2E) verified - static-token auth, federation, GitHub skill fetch, Auth0 mgmt, webhook.
- [ ] Unit tests added under `tests/unit/test_secrets_loader.py`.
- [ ] Integration tests added under `tests/integration/` for the resolver against a local Secrets Manager (e.g. `moto`) if the project uses it.
- [ ] `uv run pytest tests/ -n 8` passes with no regressions.
- [ ] `uv run ruff check --fix . && uv run ruff format .` clean.
- [ ] `uv run bandit -r registry/core/secrets_loader.py auth_server/secrets_loader.py` clean (no high-severity findings; the boto3 call and `lru_cache` are expected).
- [ ] `uv run python -m py_compile registry/core/secrets_loader.py auth_server/secrets_loader.py registry/core/config.py` succeeds.
