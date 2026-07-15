# Low-Level Design: Migrate ECS Environment Variables to AWS Secrets Manager

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
Several sensitive values are passed to the MCP Gateway ECS containers as plaintext `environment` entries. They originate as Terraform variables, are interpolated into the `container_definitions` JSON, and therefore appear in plaintext in Terraform state, in the registered ECS task definition, and in plan/apply output. A partial Secrets Manager integration already exists for the most sensitive database and IdP credentials; this design completes the migration for the remaining application secrets.

### Goals
- Move every sensitive ECS environment variable for registry, auth-server, keycloak, metrics-service, and mcpgw into an AWS Secrets Manager secret.
- Inject each migrated value through the ECS `secrets` block (resolved by ECS at task start into the same env-var name the app already reads).
- Keep the migration backward compatible: a plaintext env var, when present, still takes precedence so existing tfvars and non-ECS surfaces keep working.
- Add a shared, cached, boto3-based secret resolver the app uses when the plaintext env var is absent.
- Centralize the per-secret resource list so the IAM policy and the container `secrets` blocks stay in lockstep.

### Non-Goals
- Helm/EKS parity.
- Removing the plaintext fallback (follow-up).
- Adding rotation Lambdas for application secrets.
- Migrating Keycloak's SSM-backed admin parameters.

## Codebase Analysis

### Key Files Reviewed

| File/Directory | Purpose | Relevance to This Change |
|----------------|---------|--------------------------|
| `terraform/aws-ecs/modules/mcp-gateway/secrets.tf` | Defines the KMS key and every existing Secrets Manager secret + version | Template for new secrets; new secrets are added here |
| `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` | Defines the registry, auth-server, mcpgw, and demo ECS services via the `terraform-aws-modules/ecs/aws//modules/service` module; each container has an `environment` list and a `secrets` list | Primary edit target: move entries from `environment` to `secrets` |
| `terraform/aws-ecs/modules/mcp-gateway/iam.tf` | Defines `aws_iam_policy.ecs_secrets_access` granting `secretsmanager:GetSecretValue` on an explicit ARN list | Must be extended with every new secret ARN |
| `terraform/aws-ecs/modules/mcp-gateway/observability.tf` | Defines the metrics-service ECS service (gated on `enable_observability`); already injects `METRICS_API_KEY_*` and `OTLP_EXPORTER_HEADERS` via `secrets` | Edit target for any metrics-service plaintext migration |
| `terraform/aws-ecs/keycloak-ecs.tf` | Defines the Keycloak ECS task definition with its own task-execution role; uses SSM params for admin/admin_password/database_url and Secrets Manager for `keycloak_db_secret` (KC_DB_USERNAME/KC_DB_PASSWORD) | Verify no plaintext secrets remain; Keycloak's exec-role policy is separate from `ecs_secrets_access` |
| `terraform/aws-ecs/modules/mcp-gateway/variables.tf` | Declares every input variable, most sensitive ones already `sensitive = true` | New `*_SECRET_ARN` passthrough variables added here |
| `terraform/aws-ecs/secret-rotation.tf`, `secret-rotation-config.tf` | Rotation Lambda + `aws_secretsmanager_secret_rotation` config for DocumentDB/RDS | Reference only; no new rotators in this change |
| `registry/core/config.py` | Pydantic `BaseSettings` (`Settings`) with module-level `settings = Settings()` at line 1209 | Integrate the fallback resolver for registry settings |
| `auth_server/server.py` | Reads config via direct `os.environ.get()` / `os.getenv()` scattered through the file (e.g. `REGISTRY_API_TOKEN` line 187, `FEDERATION_STATIC_TOKEN` line 433, `SECRET_KEY` line 3015) | Integrate the fallback resolver at each sensitive read site |
| `metrics-service/app/config.py` | Plain `Settings` class reading `os.getenv()` at class-definition time | Integrate the fallback resolver for any sensitive metric-service var |
| `servers/fininfo/secrets_manager.py` | Existing example of a secret-loading wrapper (file-based) | Pattern reference for the resolver interface, not reused directly |
| `pyproject.toml`, `auth_server/pyproject.toml` | Declares `boto3>=1.42.87` (root) and `boto3>=1.28.0` (auth_server) as dependencies | Confirms no new dependency is needed |

### Existing Patterns Identified

1. **Secrets Manager + KMS + `secrets` block pattern**: Every secret is an `aws_secretsmanager_secret` (KMS-encrypted with `aws_kms_key.secrets`, `recovery_window_in_days = 0`, tagged with `local.common_tags`) plus an `aws_secretsmanager_secret_version` whose `secret_string` is seeded from a Terraform variable. Secrets whose values are managed externally (e.g. by the Keycloak init script) use `lifecycle { ignore_changes = [secret_string] }`. Container definitions reference them as `{ name = "X", valueFrom = aws_secretsmanager_secret.y.arn }` (flat string) or `{ name = "X", valueFrom = "${aws_secretsmanager_secret.y.arn}:jsonkey::" }` (structured). A future implementer must follow this exact pattern.
   - Files: `modules/mcp-gateway/secrets.tf`, `modules/mcp-gateway/ecs-services.tf`.
   - How to follow: for each migrated var, add a secret + secret_version in `secrets.tf`, then add a `secrets` block entry in `ecs-services.tf` and remove the matching `environment` entry.

2. **IAM ARN-allowlist pattern**: `aws_iam_policy.ecs_secrets_access` (iam.tf:4-52) grants `secretsmanager:GetSecretValue` on a `concat([...])` of ARNs, conditionally extending per feature flag (entra/okta/auth0/observability). It is attached to both the task-execution role and the task role of every mcp-gateway ECS service (ecs-services.tf:51-64, 636-653, 1685-1700). KMS decrypt is granted on `aws_kms_key.secrets.arn`.
   - How to follow: add each new secret ARN to the `concat([...])` in iam.tf:15-36. Because the policy is attached to all services, a secret referenced by only one service is still readable by the others; this matches the existing posture and is acceptable for this migration (see Alternatives).

3. **Keycloak separate IAM path**: Keycloak (keycloak-ecs.tf) does NOT use `ecs_secrets_access`; it has its own `keycloak_task_exec_role` with an inline `keycloak_task_exec_ssm_policy` granting SSM `GetParameter` on three params and `secretsmanager:GetSecretValue` on `keycloak_db_secret` only (keycloak-ecs.tf:169-209). Any Keycloak-side migration would extend this inline policy, not `ecs_secrets_access`.

4. **App config patterns**: Three distinct loaders coexist.
   - registry: Pydantic `BaseSettings` (`Settings`) instantiated once at module load (`settings = Settings()`).
   - auth_server: ad-hoc `os.environ.get(...)` at module level in `server.py`.
   - metrics-service: plain class with `os.getenv(...)` evaluated at class-definition time.
   - How to follow: introduce ONE shared resolver module and call it from each loader's sensitive read sites, so behavior is consistent and testable.

### Integration Points

| Component | Integration Type | Details |
|-----------|------------------|---------|
| `modules/mcp-gateway/secrets.tf` | Extends | New `aws_secretsmanager_secret` + `_secret_version` resources for each migrated var |
| `modules/mcp-gateway/ecs-services.tf` | Modifies | Move `environment` entries to `secrets` entries for registry, auth-server, mcpgw; add `*_SECRET_ARN` env vars |
| `modules/mcp-gateway/observability.tf` | Modifies | metrics-service: add `*_SECRET_ARN` env vars and any migrated `secrets` entries |
| `modules/mcp-gateway/iam.tf` | Extends | Add new secret ARNs to `ecs_secrets_access` |
| `modules/mcp-gateway/variables.tf` | Extends | Add `*_SECRET_ARN` passthrough variables (sensitive) |
| `registry/core/config.py` | Uses | Call shared resolver for migrated settings |
| `auth_server/server.py` | Uses | Call shared resolver at sensitive read sites |
| `metrics-service/app/config.py` | Uses | Call shared resolver for migrated settings |
| `.env.example` | Extends | Document the new `*_SECRET_ARN` variables |

### Constraints and Limitations Discovered
- ECS resolves a `secrets` block entry into a normal environment variable before the container starts. The app therefore already reads the value via `os.getenv`/Pydantic regardless of whether it came from `environment` or `secrets`. This is what makes the migration low-risk on ECS, and is also why the app-side fallback is primarily for non-ECS surfaces and migration safety.
- The `ecs_secrets_access` policy is shared across all mcp-gateway services, so per-service scoping is not enforced today. This design keeps that posture (see Alternatives).
- `aws_kms_key.secrets` key policy (secrets.tf:11-65) already permits any `*task-exec*` role in the account to decrypt, so new secrets encrypted with it are usable without key-policy changes.
- `recovery_window_in_days = 0` is used on all existing secrets, so destroying a renamed secret takes effect immediately; implementers must take care with `name_prefix` renames.
- Checkov `CKV2_AWS_57` (rotation) is already suppressed per-secret with a justification comment; new application secrets that are not Lambda-rotated must carry the same `#checkov:skip=CKV2_AWS_57:...` justification.
- `boto3` is a synchronous client; the auth-server is async. The resolver performs at most one `GetSecretValue` per secret per process (cached), so a blocking call at startup is acceptable and matches how other AWS SDK calls are already made in this codebase.

## Architecture

### System Context Diagram

```
                    +---------------------------+
                    |    Operator (terraform)    |
                    |  sets *_SECRET_ARN + seed  |
                    +-------------+-------------+
                                  | terraform apply
                                  v
+----------------------+   +-----------------------------+   +-----------------------+
|  Secrets Manager     |   |     ECS Task Definition     |   |   KMS (secrets key)   |
|  (new app secrets)   |<--|  secrets[] -> valueFrom ARN |-->|  Decrypt at task start|
|  seeded from tfvars  |   |  environment[] -> *_SECRET_ARN| +-----------------------+
+----------+-----------+   +--------------+--------------+
           |                              | ECS injects secret as env var
           | boto3 GetSecretValue         | (same name the app already reads)
           | (fallback path only)         v
           |                  +-----------------------------+
           +<----- fallback --|   Application process        |
                  if env var  |  registry / auth_server /    |
                  absent      |  metrics-service / mcpgw     |
                              +-----------------------------+
```

### Sequence Diagram

```
Startup (ECS, primary path):
  ECS agent --GetSecretValue--> Secrets Manager --(KMS decrypt)--> value
  ECS agent injects value as env var "REGISTRY_API_TOKEN"
  app reads os.getenv("REGISTRY_API_TOKEN")  -> value present -> used directly

Startup (non-ECS / migration fallback):
  app reads os.getenv("REGISTRY_API_TOKEN")  -> empty
  app reads os.getenv("REGISTRY_API_TOKEN_SECRET_ARN")  -> arn:...
  resolver --GetSecretValue(arn)--> Secrets Manager --(KMS decrypt)--> value
  resolver caches value in-process -> returned to caller
```

### Component Diagram

```
+--------------------------------------------------------------+
|  terraform/aws-ecs/modules/mcp-gateway                       |
|  secrets.tf   -- new aws_secretsmanager_secret[_version] x N |
|  iam.tf       -- ecs_secrets_access ARN list += N            |
|  ecs-services.tf -- environment -= N, secrets += N           |
|  observability.tf -- metrics-service wiring                  |
|  variables.tf  -- new *_SECRET_ARN variables                 |
+--------------------------------------------------------------+
              | shared module (new)
              v
+--------------------------------------------------------------+
|  registry/core/secrets_loader.py  (new shared resolver)      |
|    get_secret(env_name, arn_env_name) -> str | None          |
|    - env var first, then Secrets Manager by ARN, cached      |
+--------------------------------------------------------------+
   | reused by                | reused by            | reused by
   v                          v                      v
registry/core/config.py   auth_server/server.py   metrics-service/app/config.py
```

## Data Models

### New Models
No new Pydantic domain models are introduced. The shared resolver is a module of functions, not a model:

```python
# registry/core/secrets_loader.py (sketch; full code in Implementation Details)
def get_secret(
    env_name: str,
    arn_env_name: str,
) -> str | None:
    """Return a secret value with a plaintext-env-var-first fallback.

    Resolution order:
    1. The plaintext environment variable ``env_name`` if present and non-empty
       (preserves pre-migration behavior on any deployment surface).
    2. The Secrets Manager secret referenced by the ARN in ``arn_env_name``,
       fetched once via boto3 and cached for the process lifetime.

    Returns ``None`` when neither source is configured.
    """
```

### Model Changes
The registry `Settings` (Pydantic `BaseSettings`) gains one optional field per migrated secret to carry the ARN, used only by the resolver:

```python
# registry/core/config.py (additions)
class Settings(BaseSettings):
    # ... existing fields ...

    # Secrets Manager ARNs for migrated application secrets. Empty by default;
    # the resolver only contacts Secrets Manager when these are set AND the
    # plaintext env var is absent. Remove together with the fallback in the
    # follow-up issue.
    registry_api_token_secret_arn: str = ""
    registry_api_keys_secret_arn: str = ""
    federation_static_token_secret_arn: str = ""
    federation_encryption_key_secret_arn: str = ""
    auth0_management_api_token_secret_arn: str = ""
    ans_api_key_secret_arn: str = ""
    ans_api_secret_secret_arn: str = ""
    github_pat_secret_arn: str = ""
    github_app_private_key_secret_arn: str = ""
    registration_webhook_auth_token_secret_arn: str = ""
    registration_gate_auth_credential_secret_arn: str = ""
    registration_gate_oauth2_client_secret_secret_arn: str = ""
```

The auth-server and metrics-service do not use Pydantic; they read the `*_SECRET_ARN` env vars directly through the shared resolver.

## API / CLI Design

This change adds no HTTP endpoints and no CLI commands. It is a configuration/infrastructure change. The only externally observable interface is the new set of environment variables (see Configuration Parameters) and the behavioral guarantee that the services still start and authenticate exactly as before.

## Configuration Parameters

### New Environment Variables

| Variable Name | Type | Default | Required | Description |
|---------------|------|---------|----------|-------------|
| `REGISTRY_API_TOKEN_SECRET_ARN` | string (ARN) | `""` | No | Secrets Manager ARN backing `REGISTRY_API_TOKEN`; used only when the plaintext env var is absent |
| `REGISTRY_API_KEYS_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `REGISTRY_API_KEYS` |
| `FEDERATION_STATIC_TOKEN_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `FEDERATION_STATIC_TOKEN` |
| `FEDERATION_ENCRYPTION_KEY_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `FEDERATION_ENCRYPTION_KEY` |
| `AUTH0_MANAGEMENT_API_TOKEN_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `AUTH0_MANAGEMENT_API_TOKEN` |
| `ANS_API_KEY_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `ANS_API_KEY` |
| `ANS_API_SECRET_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `ANS_API_SECRET` |
| `GITHUB_PAT_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `GITHUB_PAT` |
| `GITHUB_APP_PRIVATE_KEY_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `GITHUB_APP_PRIVATE_KEY` |
| `REGISTRATION_WEBHOOK_AUTH_TOKEN_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `REGISTRATION_WEBHOOK_AUTH_TOKEN` |
| `REGISTRATION_GATE_AUTH_CREDENTIAL_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `REGISTRATION_GATE_AUTH_CREDENTIAL` |
| `REGISTRATION_GATE_OAUTH2_CLIENT_SECRET_SECRET_ARN` | string (ARN) | `""` | No | ARN backing `REGISTRATION_GATE_OAUTH2_CLIENT_SECRET` |
| `MCP_SECRETS_RESOLVER_ENABLED` | bool | `true` | No | Master switch for the app-side Secrets Manager fallback; set `false` to disable fetches entirely (env vars only) |
| `MCP_SECRETS_RESOLVER_REGION` | string | `""` | No | Region for the boto3 client; defaults to `AWS_REGION`/`AWS_DEFAULT_REGION` when empty |

### Settings / Config Class Updates
See Data Models for the Pydantic field additions. The auth-server and metrics-service read the same env vars via the resolver without a settings class change.

### Deployment Surface Checklist
- [x] Terraform variables: `modules/mcp-gateway/variables.tf` (new `*_SECRET_ARN` variables) and `terraform/aws-ecs/variables.tf` (passthrough if needed).
- [x] Terraform ECS wiring: `ecs-services.tf` (registry, auth-server, mcpgw) and `observability.tf` (metrics-service).
- [x] IAM: `iam.tf` (`ecs_secrets_access` ARN list).
- [x] `.env.example`: document the new `*_SECRET_ARN` variables and `MCP_SECRETS_RESOLVER_ENABLED`.
- [ ] Docker Compose: add the new variables to the compose env (commented/empty by default) so local dev can opt in. (Helm/EKS out of scope.)

## New Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| _(none)_ | _(none)_ | `boto3` is already declared in `pyproject.toml` (`boto3>=1.42.87`) and `auth_server/pyproject.toml` (`boto3>=1.28.0`) |

This change uses only existing dependencies.

## Implementation Details

### Step-by-Step Plan (for a future implementer)

#### Step 1: Add the new Secrets Manager secrets
**File:** `terraform/aws-ecs/modules/mcp-gateway/secrets.tf`
**Lines:** append after the existing `otlp_exporter_headers` block (~line 376)

Add a secret + secret_version for each migrated value, seeded from the existing variable. Use the existing `name_prefix`, KMS key, and tags. For example:

```hcl
# REGISTRY_API_TOKEN (migrated from plaintext environment block)
#checkov:skip=CKV2_AWS_57:Application API token - rotation requires coordinated consumer update
resource "aws_secretsmanager_secret" "registry_api_token" {
  name_prefix             = "${local.name_prefix}-registry-api-token-"
  description             = "Static API token for registry access (migrated from ECS env var)"
  recovery_window_in_days = 0
  kms_key_id              = aws_kms_key.secrets.id
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "registry_api_token" {
  secret_id     = aws_secretsmanager_secret.registry_api_token.id
  secret_string = var.registry_api_token
}
```

Repeat the same shape for: `registry_api_keys`, `federation_static_token`, `federation_encryption_key`, `auth0_management_api_token` (gate with `count = var.auth0_enabled ? 1 : 0` to match existing Auth0 posture), `ans_api_key`, `ans_api_secret`, `github_pat`, `github_app_private_key`, `registration_webhook_auth_token`, `registration_gate_auth_credential`, and `registration_gate_oauth2_client_secret`.

Notes for the implementer:
- Mirror the existing convention: secrets whose seed value may be empty should still be created (the app treats an empty resolved value the same as an absent one) OR gated with `count` behind the relevant feature flag. Prefer `count` gating only where an existing equivalent gate exists (e.g. Auth0). Otherwise create unconditionally with `recovery_window_in_days = 0`.
- Keep `lifecycle { ignore_changes = [secret_string] }` OFF for these secrets so that re-running `terraform apply` with an updated tfvars value updates the secret. (This differs from the IdP secrets, which are managed externally.)

#### Step 2: Extend the IAM policy
**File:** `terraform/aws-ecs/modules/mcp-gateway/iam.tf`
**Lines:** ~15-36 (the `Resource = concat([...])` in `ecs_secrets_access`)

Add every new secret ARN to the list. For count-gated secrets, use the conditional form already used for Entra/Okta/Auth0:

```hcl
Resource = concat(
  [
    aws_secretsmanager_secret.secret_key.arn,
    # ... existing entries ...
    aws_secretsmanager_secret.registry_api_token.arn,
    aws_secretsmanager_secret.registry_api_keys.arn,
    aws_secretsmanager_secret.federation_static_token.arn,
    aws_secretsmanager_secret.federation_encryption_key.arn,
    aws_secretsmanager_secret.ans_api_key.arn,
    aws_secretsmanager_secret.ans_api_secret.arn,
    aws_secretsmanager_secret.github_pat.arn,
    aws_secretsmanager_secret.github_app_private_key.arn,
    aws_secretsmanager_secret.registration_webhook_auth_token.arn,
    aws_secretsmanager_secret.registration_gate_auth_credential.arn,
    aws_secretsmanager_secret.registration_gate_oauth2_client_secret.arn,
  ],
  var.auth0_enabled ? [aws_secretsmanager_secret.auth0_management_api_token[0].arn] : [],
  # ... existing conditional entries ...
)
```

#### Step 3: Move env entries to secrets blocks and wire `*_SECRET_ARN`
**File:** `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf`

For the **registry** container (the `environment = concat([...])` block starting ~line 698 and the `secrets = concat([...])` block starting ~line 1288):

- Remove the plaintext entries for `REGISTRY_API_TOKEN`, `REGISTRY_API_KEYS`, `FEDERATION_STATIC_TOKEN`, `FEDERATION_ENCRYPTION_KEY`, `AUTH0_MANAGEMENT_API_TOKEN`, `ANS_API_KEY`, `ANS_API_SECRET`, `GITHUB_PAT`, `GITHUB_APP_PRIVATE_KEY`, `REGISTRATION_WEBHOOK_AUTH_TOKEN`, `REGISTRATION_GATE_AUTH_CREDENTIAL`, `REGISTRATION_GATE_OAUTH2_CLIENT_SECRET` from the `environment` list.
- Add matching entries to the `secrets` list, e.g.:

```hcl
secrets = concat(
  [
    # ... existing entries ...
    { name = "REGISTRY_API_TOKEN", valueFrom = aws_secretsmanager_secret.registry_api_token.arn },
    { name = "REGISTRY_API_KEYS",  valueFrom = aws_secretsmanager_secret.registry_api_keys.arn },
    { name = "FEDERATION_STATIC_TOKEN",    valueFrom = aws_secretsmanager_secret.federation_static_token.arn },
    { name = "FEDERATION_ENCRYPTION_KEY",  valueFrom = aws_secretsmanager_secret.federation_encryption_key.arn },
    { name = "AUTH0_MANAGEMENT_API_TOKEN", valueFrom = aws_secretsmanager_secret.auth0_management_api_token[0].arn },
    { name = "ANS_API_KEY",     valueFrom = aws_secretsmanager_secret.ans_api_key.arn },
    { name = "ANS_API_SECRET",  valueFrom = aws_secretsmanager_secret.ans_api_secret.arn },
    { name = "GITHUB_PAT",              valueFrom = aws_secretsmanager_secret.github_pat.arn },
    { name = "GITHUB_APP_PRIVATE_KEY",  valueFrom = aws_secretsmanager_secret.github_app_private_key.arn },
    { name = "REGISTRATION_WEBHOOK_AUTH_TOKEN",       valueFrom = aws_secretsmanager_secret.registration_webhook_auth_token.arn },
    { name = "REGISTRATION_GATE_AUTH_CREDENTIAL",     valueFrom = aws_secretsmanager_secret.registration_gate_auth_credential.arn },
    { name = "REGISTRATION_GATE_OAUTH2_CLIENT_SECRET", valueFrom = aws_secretsmanager_secret.registration_gate_oauth2_client_secret.arn },
  ],
  # ... existing conditional entries ...
)
```

- Add the `*_SECRET_ARN` env entries to the `environment` list so the app-side fallback can find the secret on non-ECS surfaces:

```hcl
{
  name  = "REGISTRY_API_TOKEN_SECRET_ARN"
  value = aws_secretsmanager_secret.registry_api_token.arn
},
# ... one per migrated secret ...
```

For the **auth-server** container (environment block ~line 97, secrets block ~line 413): apply the same transformation for the subset of secrets the auth-server consumes (`REGISTRY_API_TOKEN`, `REGISTRY_API_KEYS`, `FEDERATION_STATIC_TOKEN`, `FEDERATION_ENCRYPTION_KEY`, `AUTH0_MANAGEMENT_API_TOKEN`, `ANS_API_KEY`, `ANS_API_SECRET`). The auth-server does not consume the GitHub or registration-gate secrets.

For the **mcpgw** container (currently `secrets = []` at line 1801): mcpgw has no plaintext sensitive env vars today, so no migration is needed; only ensure `MCP_SECRETS_RESOLVER_ENABLED` is not required. No change required unless a future audit finds a sensitive mcpgw var.

#### Step 4: metrics-service wiring
**File:** `terraform/aws-ecs/modules/mcp-gateway/observability.tf`
**Lines:** metrics-service `environment` (~206) and `secrets` (~257)

The metrics-service currently has no plaintext sensitive env vars (its secrets are already injected). Verify and, if a sensitive plaintext var is found during implementation, apply the same move-to-`secrets` pattern. Otherwise this step is a no-op aside from confirming the ADOT sidecar env vars are non-sensitive.

#### Step 5: Add the shared app-side resolver
**File:** `registry/core/secrets_loader.py` (new)

```python
"""Shared Secrets Manager resolver with a plaintext-env-var-first fallback.

During the ECS env-var -> Secrets Manager migration, every migrated setting can
still be supplied as a plaintext environment variable (the pre-migration path).
This resolver returns the plaintext env var when present and non-empty, and
otherwise fetches the value from AWS Secrets Manager by ARN. Results are cached
for the process lifetime so at most one GetSecretValue call is made per secret.

This fallback is temporary and will be removed once all deployment surfaces are
migrated. See github-issue.md (follow-up).
"""

import logging
import os
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_RESOLVER_ENABLED: bool = os.getenv("MCP_SECRETS_RESOLVER_ENABLED", "true").lower() == "true"


def _client_region() -> str | None:
    return os.getenv("MCP_SECRETS_RESOLVER_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None


@lru_cache(maxsize=None)
def _fetch_secret_value(arn: str) -> str | None:
    """Fetch and cache a single secret value from Secrets Manager."""
    if not _RESOLVER_ENABLED:
        logger.debug("Secrets resolver disabled; skipping fetch for %s", arn)
        return None
    try:
        client = boto3.client("secretsmanager", region_name=_client_region())
        response = client.get_secret_value(SecretId=arn)
    except ClientError:
        logger.exception("Failed to fetch secret from Secrets Manager: %s", arn)
        return None
    if "SecretString" in response:
        return response["SecretString"]
    # Binary secrets are not used for these settings.
    logger.warning("Secret %s has no SecretString; returning None", arn)
    return None


def get_secret(
    env_name: str,
    arn_env_name: str,
) -> str | None:
    """Return a secret value with a plaintext-env-var-first fallback.

    Args:
        env_name: Name of the plaintext environment variable (pre-migration path).
        arn_env_name: Name of the environment variable holding the Secrets Manager ARN.

    Returns:
        The secret value, or None when neither source is configured.
    """
    plaintext = os.environ.get(env_name, "").strip()
    if plaintext:
        return plaintext
    arn = os.environ.get(arn_env_name, "").strip()
    if not arn:
        return None
    return _fetch_secret_value(arn)
```

Design notes:
- `lru_cache` on `_fetch_secret_value` keys by ARN, so the same secret fetched from multiple call sites is resolved once.
- The resolver is synchronous and called at config-load time (startup), not in hot request paths.
- Failures to fetch return `None` and log an exception; the caller decides whether `None` is fatal (it already does today, since these env vars default to empty).

#### Step 6: Wire the resolver into the registry config
**File:** `registry/core/config.py`
**Lines:** near the relevant field defaults (e.g. `registry_api_token` ~line 80, `registration_webhook_auth_token` ~line 93, `registration_gate_auth_credential` ~line 115)

Replace direct env-var-backed defaults with resolver calls. Because Pydantic `BaseSettings` reads env vars at instantiation, the cleanest approach is a `@model_validator` (or field validators) that, after env loading, fills any still-empty sensitive field from the resolver:

```python
from registry.core.secrets_loader import get_secret

class Settings(BaseSettings):
    # ... existing fields, plus the *_SECRET_ARN fields from Data Models ...

    @model_validator(mode="after")
    def _resolve_secrets_manager_fallbacks(self) -> "Settings":
        """Fill sensitive fields from Secrets Manager when the plaintext env var is absent."""
        fallbacks = [
            ("registry_api_token", "REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN"),
            ("registry_api_keys", "REGISTRY_API_KEYS", "REGISTRY_API_KEYS_SECRET_ARN"),
            ("federation_static_token", "FEDERATION_STATIC_TOKEN", "FEDERATION_STATIC_TOKEN_SECRET_ARN"),
            ("federation_encryption_key", "FEDERATION_ENCRYPTION_KEY", "FEDERATION_ENCRYPTION_KEY_SECRET_ARN"),
            ("registration_webhook_auth_token", "REGISTRATION_WEBHOOK_AUTH_TOKEN", "REGISTRATION_WEBHOOK_AUTH_TOKEN_SECRET_ARN"),
            ("registration_gate_auth_credential", "REGISTRATION_GATE_AUTH_CREDENTIAL", "REGISTRATION_GATE_AUTH_CREDENTIAL_SECRET_ARN"),
            ("registration_gate_oauth2_client_secret", "REGISTRATION_GATE_OAUTH2_CLIENT_SECRET", "REGISTRATION_GATE_OAUTH2_CLIENT_SECRET_SECRET_ARN"),
        ]
        for field_name, env_name, arn_env_name in fallbacks:
            if not getattr(self, field_name):
                value = get_secret(env_name, arn_env_name)
                if value:
                    object.__setattr__(self, field_name, value)
        return self
```

For settings consumed outside `Settings` (e.g. the GitHub vars, ANS vars, Auth0 management token), patch the read sites directly or add them to the same validator if they live on `Settings`. Audit `registry/` for every `os.getenv("GITHUB_PAT")`, `os.getenv("ANS_API_KEY")`, etc. and route each through `get_secret`.

#### Step 7: Wire the resolver into the auth-server
**File:** `auth_server/server.py`
**Lines:** the sensitive `os.environ.get(...)` sites (e.g. line 187 `REGISTRY_API_TOKEN`, line 190 `REGISTRY_API_KEYS`, line 433 `FEDERATION_STATIC_TOKEN`)

Replace each with a resolver call. The auth-server is a separate package (`auth_server/pyproject.toml`) that already depends on boto3; the resolver module should be importable from it. Because the auth-server does not share the `registry` package, either:
- (a) duplicate the small resolver into `auth_server/secrets_loader.py`, or
- (b) extract it into a shared package both depend on.

Option (a) is simpler and avoids a new shared package; prefer it. Keep the two copies byte-for-byte identical and note the duplication in a comment.

```python
# auth_server/server.py
from auth_server.secrets_loader import get_secret

REGISTRY_API_TOKEN: str = get_secret("REGISTRY_API_TOKEN", "REGISTRY_API_TOKEN_SECRET_ARN") or ""
_REGISTRY_API_KEYS_RAW: str = (get_secret("REGISTRY_API_KEYS", "REGISTRY_API_KEYS_SECRET_ARN") or "").strip()
FEDERATION_STATIC_TOKEN: str = get_secret("FEDERATION_STATIC_TOKEN", "FEDERATION_STATIC_TOKEN_SECRET_ARN") or ""
# ... etc for each migrated auth-server secret ...
```

#### Step 8: Wire the resolver into the metrics-service
**File:** `metrics-service/app/config.py`

The metrics-service currently has no plaintext sensitive env vars to migrate. If, during implementation, a sensitive var is identified, route it through a local copy of the resolver. Otherwise this step confirms no change is needed beyond verifying the existing `METRICS_API_KEY_*` secrets remain wired.

#### Step 9: Document the new variables
**File:** `.env.example`

Add a section listing every `*_SECRET_ARN`, `MCP_SECRETS_RESOLVER_ENABLED`, and `MCP_SECRETS_RESOLVER_REGION`, with a note that they are optional and used only by the migration fallback.

### Error Handling
- A failed `GetSecretValue` logs an exception and returns `None`; callers already tolerate empty values (today's default). The service logs a clear error at startup when a required secret is missing (existing behavior, since the app already fails closed on missing required config).
- Malformed ARNs are surfaced by boto3 as `ClientError`; the resolver catches and logs them rather than crashing startup. The caller decides severity.
- When `MCP_SECRETS_RESOLVER_ENABLED=false`, the resolver never contacts Secrets Manager; this gives operators an escape hatch if AWS API issues block startup.

### Logging
- `logger.debug` when the resolver is disabled or a fetch is skipped.
- `logger.warning` when a secret has no `SecretString`.
- `logger.exception` on `ClientError`, including the ARN (never the value).
- An INFO log at startup listing how many migrated settings were resolved from Secrets Manager vs. plaintext env vars (counts only, no values), to aid migration tracking. This is added in the validator.

## Observability
- **Logs**: startup INFO line with resolver source counts; per-secret `WARNING`/`ERROR` on fetch failure.
- **Metrics**: no new application metrics. ECS-side, CloudTrail records every `GetSecretValue` call with the calling task role, providing the audit trail for secret access.
- **Tracing**: the resolver call happens once at startup; no per-request spans are needed.

## Scaling Considerations
- **Current load assumptions**: one `GetSecretValue` per secret per process at startup, then cached. With ~12 secrets and a handful of service replicas, this is negligible.
- **Horizontal scaling**: each new task makes its own cached fetches; Secrets Manager handles high request rates and retries automatically. No bottleneck.
- **Caching strategy**: `lru_cache(maxsize=None)` on `_fetch_secret_value` bounds memory to one entry per distinct ARN per process (a handful of small strings). No TTL is needed because config is immutable for the process lifetime; a rolling restart picks up rotated values.
- **Cross-account access**: the KMS key policy (secrets.tf:23-42) already permits `*task-exec*` roles in the account to decrypt. Cross-account consumers would be added via a resource-based secret policy in a follow-up; the Secrets Manager choice (vs. SSM) keeps that option open.

## File Changes

### New Files

| File Path | Description |
|-----------|-------------|
| `terraform/aws-ecs/modules/mcp-gateway/secrets_app.tf` | (Optional split) New `aws_secretsmanager_secret[_version]` resources for migrated app secrets, to keep `secrets.tf` focused. Alternatively append to `secrets.tf`. |
| `registry/core/secrets_loader.py` | Shared resolver with env-var-first fallback |
| `auth_server/secrets_loader.py` | Identical copy of the resolver for the auth-server package |

### Modified Files

| File Path | Lines | Change Description |
|-----------|-------|--------------------|
| `terraform/aws-ecs/modules/mcp-gateway/secrets.tf` (or `secrets_app.tf`) | ~+120 | Add ~12 secret + secret_version resources |
| `terraform/aws-ecs/modules/mcp-gateway/iam.tf` | ~+15 | Extend `ecs_secrets_access` ARN list |
| `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` | ~+60 / ~-40 | Move env entries to secrets; add `*_SECRET_ARN` env entries for registry and auth-server |
| `terraform/aws-ecs/modules/mcp-gateway/observability.tf` | ~+0-10 | Verify/no-op for metrics-service |
| `terraform/aws-ecs/modules/mcp-gateway/variables.tf` | ~+60 | Add `*_SECRET_ARN` passthrough variables if ARNs are passed from the root module (otherwise the ARNs are computed inside the module and no new variable is needed) |
| `registry/core/config.py` | ~+40 | Add `*_SECRET_ARN` fields and the `_resolve_secrets_manager_fallbacks` validator |
| `auth_server/server.py` | ~+15 / ~-15 | Route sensitive read sites through `get_secret` |
| `metrics-service/app/config.py` | ~+0-5 | No-op unless a sensitive var is found |
| `.env.example` | ~+20 | Document new `*_SECRET_ARN` and resolver switches |

### Estimated Lines of Code

| Category | Lines |
|----------|-------|
| New code (resolver + validators + wiring) | ~150 |
| New tests | ~180 |
| Modified code (Terraform + config) | ~250 |
| **Total** | **~580** |

## Testing Strategy
The full plan lives in `./testing.md`. Highlights: Terraform `plan`/`validate` assertions that no plaintext secret appears in the diff and that every migrated secret ARN is present in the `secrets` block and the IAM policy; unit tests for the resolver (env-first, ARN fallback, caching, disabled switch, error path); backwards-compat tests that a plaintext env var still wins; and an ECS deploy-and-verify that the services start and authenticate unchanged.

## Alternatives Considered

### Alternative 1: ECS `secrets` block only, no app-side resolver
**Description:** Move every env var to the `secrets` block and change nothing in the app. ECS injects the value as the same env var name.
**Pros / Cons:** Simplest; zero app code. But it breaks non-ECS surfaces (Docker Compose, CI) that do not perform ECS secret injection, and it removes the migration safety net (no plaintext fallback while operators update tfvars).
**Why Rejected:** The task explicitly requires keeping the plaintext env-var fallback during migration and supporting the app config loader changes. This alternative fails Q6.

### Alternative 2: SSM Parameter Store (SecureString) instead of Secrets Manager
**Description:** Store the migrated values as SSM SecureStrings and reference via the ECS `secrets` block with `valueFrom` pointing at the parameter ARN.
**Pros / Cons:** Cheaper; already used by Keycloak for admin params. But no native rotation Lambda support and more cumbersome cross-account access.
**Why Rejected:** The task specifies AWS Secrets Manager only (Q5), for rotation support and cross-account access.

### Alternative 3: Per-service IAM scoping
**Description:** Give each ECS service an IAM policy that lists only the secrets it actually consumes, instead of the shared `ecs_secrets_access` allowlist.
**Pros / Cons:** Better least-privilege posture.
**Why Rejected:** Out of scope for this migration (it would be a larger IAM refactor across all services). The existing posture already shares the policy; this change keeps consistency and notes the follow-up. A reviewer may flag this (see review.md).

### Alternative 4: A new shared Python package for the resolver
**Description:** Extract `secrets_loader.py` into a shared package imported by registry, auth-server, and metrics-service.
**Pros / Cons:** Removes duplication; single source of truth.
**Why Rejected:** Adds packaging/build complexity disproportionate to ~60 lines of code. Duplicating the small module across the two packages that need it (with a comment noting the duplication) is simpler and matches the repo's current style of per-package config loaders.

### Comparison Matrix

| Criteria | Chosen (SM + resolver) | Alt 1 (secrets only) | Alt 2 (SSM) | Alt 3 (per-svc IAM) |
|----------|------------------------|----------------------|-------------|---------------------|
| Meets Q5 (Secrets Manager) | Yes | Yes | No | Yes |
| Meets Q6 (plaintext fallback) | Yes | No | Yes | Yes |
| Non-ECS surface support | Yes | No | Yes | Yes |
| Implementation complexity | Medium | Low | Medium | High |
| Least-privilege IAM | Partial (follow-up) | Partial | Partial | Yes |

## Rollout Plan
- Phase 1: Implementation (out of scope for this skill).
- Phase 2: Testing (see `testing.md`): Terraform validate/plan assertions, resolver unit tests, backwards-compat tests, then a staging ECS deploy with verification that all services start and authenticate unchanged.
- Phase 3: Deployment: apply Terraform (secrets are seeded from current tfvars, so the deploy is non-disruptive), roll services, confirm CloudTrail shows `GetSecretValue` from the task roles and that no plaintext secrets remain in the task definition. Open the follow-up issue to remove the plaintext fallback.

## Open Questions
- Should the new application secrets be count-gated behind their feature flags (e.g. `ANS_API_KEY` only when `ans_integration_enabled`)? The current IdP secrets gate on their provider flag; matching that is cleaner but means the `secrets` block must also be conditional. Recommendation: gate where a clear feature flag exists, create unconditionally otherwise.
- Do the `REGISTRATION_WEBHOOK_AUTH_HEADER` and `GITHUB_APP_ID`/`GITHUB_APP_INSTALLATION_ID` values need migration? They are borderline (header name is config, app id is not secret). Recommendation: migrate `REGISTRATION_WEBHOOK_AUTH_HEADER` only if it carries a credential in practice; leave the GitHub IDs as plaintext config. Confirm with the maintainer.
- Should the resolver live in a shared package long-term? Defer to the follow-up that removes the fallback.

## References
- Existing secret pattern: `terraform/aws-ecs/modules/mcp-gateway/secrets.tf`
- Existing ECS `secrets` block usage: `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf:413-480` (auth-server), `1288-1365` (registry)
- IAM allowlist: `terraform/aws-ecs/modules/mcp-gateway/iam.tf:4-52`
- Keycloak Secrets Manager sourcing (issue #1026): `terraform/aws-ecs/keycloak-ecs.tf:77-105, 189-199`
- Rotation Lambda reference: `terraform/aws-ecs/secret-rotation.tf`
- AWS docs: ECS container definition `secrets` (https://docs.aws.amazon.com/AmazonECS/latest/developerguide/specifying-sensitive-data.html)
