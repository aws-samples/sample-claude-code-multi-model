# Low-Level Design: Replace Keycloak DB password with RDS IAM authentication

*Created: 2026-07-15*
*Author: Claude (model: glm-5.2)*
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
15. [Open Questions](#open-questions)
16. [References](#references)

## Overview

### Problem Statement
Keycloak's ECS task connects to its Aurora MySQL database using a static password. Today that password is operator-supplied (`keycloak_database_password` in `terraform.tfvars`), stored in the Secrets Manager secret `keycloak/database`, injected into the Keycloak container as `KC_DB_PASSWORD`, and rotated every 30 days by the `rotate-rds` Lambda. A long-lived credential therefore lives in operator config and in the container environment. The goal is to replace this with AWS IAM database authentication (short-lived, 15-minute tokens generated from the ECS task role), and to remove the static password from operator-supplied config and from the Keycloak container environment entirely, while keeping the password path available as a fallback behind a feature flag.

### Goals
- Keycloak authenticates to the database using an IAM auth token, not a static password, when the feature is enabled.
- The static DB password is no longer present in `terraform.tfvars`, the ECS task environment, or any SSM/Secrets value that Keycloak reads, in IAM mode.
- The change is fully reversible: with the flag off, the deployment matches today's behaviour.
- The existing rotation Lambda and Secrets Manager secret continue to back the proxy's backend credential.
- No Keycloak version change (remains 25.0). No Helm/EKS changes.

### Non-Goals
- Removing the Secrets Manager secret or the rotation Lambda (still required to back the proxy and for break-glass access).
- Changing the Keycloak version or the database engine.
- Touching the docker-compose / Helm deployment surfaces.
- Implementing direct IAM DB auth to the Aurora cluster endpoint (proxy-based auth is chosen; see Alternatives).

## Codebase Analysis

### Key Files Reviewed

| File/Directory | Purpose | Relevance to This Change |
|----------------|---------|--------------------------|
| `terraform/aws-ecs/keycloak-database.tf` | Aurora cluster, RDS Proxy, KMS key, proxy IAM role/policy, Secrets Manager secret `keycloak/database`, SSM `/keycloak/database/url` | Primary change site: proxy `iam_auth`, conditional `KC_DB_URL` target, secret content, master password source |
| `terraform/aws-ecs/keycloak-ecs.tf` | Keycloak ECS task definition, container env + secrets, task exec role, task role | Conditional injection of `KC_DB_USERNAME`/`KC_DB_PASSWORD`, driver config env, task-role `rds-db:connect` policy |
| `terraform/aws-ecs/variables.tf` | Input variables | Add `keycloak_db_iam_auth_enabled`; relax/conditionalise `keycloak_database_password` |
| `terraform/aws-ecs/secret-rotation.tf` / `secret-rotation-config.tf` | Rotation Lambda + 30-day schedule for the `keycloak/database` secret | Verify rotation still works when Keycloak no longer reads the secret (it does; the proxy reads it) |
| `terraform/aws-ecs/lambda/rotate-rds/index.py` | 4-step rotation that calls `rds:ModifyDBCluster` on the master password | Unchanged; still rotates the proxy's backend credential |
| `terraform/aws-ecs/keycloak-security-groups.tf` | `keycloak_db` SG (attached to cluster + proxy), ECS egress to `keycloak_db` on 3306 | Already permits ECS->proxy on 3306; no SG change needed |
| `terraform/aws-ecs/terraform.tfvars.example` | Documented variable defaults | Remove/comment `keycloak_database_password` in IAM mode; document the new flag |
| `terraform/aws-ecs/outputs.tf` | Stack outputs | Optionally surface the proxy endpoint for operability |
| `docker/keycloak/Dockerfile` | Custom Keycloak image build (`KC_DB=mysql`, token-exchange, `kc.sh build`) | Vendor the AWS Advanced JDBC Driver wrapper JAR |
| `terraform/aws-ecs/locals.tf` | `common_tags`, `is_aws_documentdb` etc. | Add an `is_keycloak_db_iam` local derived from the flag |

### Existing Patterns Identified

1. **Feature gating via `count` + locals boolean**: DocumentDB-only resources are gated with `count = local.is_aws_documentdb ? 1 : 0`, where `locals.is_aws_documentdb = var.storage_backend == "documentdb"` (see `locals.tf:40` and the issue #955 comments throughout `secret-rotation.tf`). The IAM-auth resources will reuse this exact pattern with `count = local.is_keycloak_db_iam ? 1 : 0`.

2. **Conditional container env / secrets via locals**: `keycloak_container_env` and `keycloak_container_secrets` are already `local` lists built with Terraform `concat()`. New conditional entries are added by `concat`-ing a `count`-gated list, mirroring how DocumentDB statements are conditionally concatenated in `secret-rotation.tf:54-124`.

3. **Secrets/SSM injection via `valueFrom`**: ECS secrets use `valueFrom = "<arn>:<key>::"` for Secrets Manager and `valueFrom = "<arn>"` for SSM (see `keycloak-ecs.tf:86-104`). The IAM-mode driver config will use plain `environment` entries (not secrets) since region/endpoint are non-sensitive.

4. **checkov skip comments with justifications**: Every suppressed check has a `#checkov:skip=CKV_...:reason` comment. The `CKV_AWS_162` skip on the Aurora cluster (`keycloak-database.tf:43`) currently says "IAM database authentication not used - Keycloak uses password auth". This comment must be updated in IAM mode because the proxy (not the cluster) now enforces IAM auth.

5. **KMS-encrypted secrets + task exec role `secretsmanager:GetSecretValue` + `kms:Decrypt`**: The exec role already has the permissions needed to read the secret and decrypt with KMS (`keycloak-ecs.tf:188-206`). In IAM mode these are simply not exercised by Keycloak (the proxy reads the secret instead).

### Integration Points

| Component | Integration Type | Details |
|-----------|------------------|---------|
| `aws_db_proxy.keycloak` | Repurposed (currently orphaned) | Set `iam_auth = "REQUIRED"` when flag on; wire its endpoint into `KC_DB_URL` |
| `aws_ssm_parameter.keycloak_database_url` | Modified (conditional value) | Value = proxy endpoint when IAM on, cluster endpoint when off |
| `local.keycloak_container_secrets` | Modified (conditional entries) | `KC_DB_USERNAME`/`KC_DB_PASSWORD` injected only when flag off |
| `local.keycloak_container_env` | Modified (conditional entries) | Driver/plugin config env injected only when flag on |
| `aws_iam_role.keycloak_task_role` | Extended | Attach `rds-db:connect` policy for the proxy `dbuser` when flag on |
| `aws_rds_cluster.keycloak` | Modified (master password source) | `master_password` = auto-generated random value when IAM on, `var.keycloak_database_password` when off |
| `aws_secretsmanager_secret_version.keycloak_db_secret` | Modified (password source) | `password` = auto-generated value when IAM on |
| `docker/keycloak/Dockerfile` | Extended | Add AWS Advanced JDBC Driver wrapper JAR to `/opt/keycloak/providers/` |

### Constraints and Limitations Discovered
- **Orphaned proxy**: The RDS Proxy is provisioned but `aws_db_proxy.keycloak.endpoint` is referenced nowhere; `KC_DB_URL` points at the cluster endpoint. Wiring the proxy is a required, safe change (it is already paid-for infrastructure).
- **IAM auth token lifetime**: RDS IAM auth tokens are valid for 15 minutes. Keycloak's connection pool acquires connections over time, so the token must be regenerated per connection. A driver-level plugin (AWS Advanced JDBC Driver `iam` plugin) is required; a one-shot sidecar/env-var password will not work.
- **RDS Proxy IAM auth username**: With `iam_auth = REQUIRED`, the connection username must be an IAM-principal-mapped DB user name and the caller needs `rds-db:connect` on `arn:aws:rds-db:<region>:<account>:dbuser:<proxy-resource-id>/<db-user>`. The proxy maps that to the Secrets Manager secret's `username` to authenticate to Aurora. The DB user itself does NOT need `AWSAuthenticationPlugin` enabled (that is only required for direct-to-cluster IAM auth).
- **Keycloak optimized vs non-optimized start**: The custom image runs `kc.sh build` (optimized) and uses `ENTRYPOINT ["kc.sh","start","--optimized"]`; the public prebuilt image runs `command = ["start"]` (non-optimized) per `keycloak-ecs.tf:297`. The driver JAR placed in `/opt/keycloak/providers/` is picked up in both cases.
- **Engine**: Aurora MySQL 8.0 (`aurora-mysql` 3.10.3). RDS Proxy IAM auth supports MySQL. No engine change needed.
- **The `master_username` cannot be an IAM user**: Aurora requires the master user to be password-based. IAM auth therefore cannot replace the master credential entirely; it removes it from Keycloak's environment and operator config, while the master password remains in Secrets Manager (proxy backend + break-glass). This is the intended and accepted trade-off.
- **`require_tls = false` on the proxy**: IAM auth tokens are only valid over TLS. The proxy currently has `require_tls = false`. For IAM auth to work, the client->proxy leg must use TLS. See Open Questions / Implementation Details for the TLS handling.

## Architecture

### System Context Diagram

```
                    Today (password mode, flag = false)
                    -----------------------------------
   Operator tfvars  ---> var.keycloak_database_password
        |                           |
        |                           v
        |            Secrets Manager secret "keycloak/database" {username,password}
        |                           |  (rotated every 30d by rotate-rds Lambda)
        |                           v
   ECS task (Keycloak) <-- valueFrom -- KC_DB_USERNAME / KC_DB_PASSWORD
        |   KC_DB_URL (SSM) = jdbc:mysql://<cluster-endpoint>:3306/keycloak
        `-------------------> Aurora MySQL cluster (password auth, master user)


    Target (IAM mode, flag = true)
    ------------------------------
   Operator tfvars  ---> (no DB password supplied)
        |
        v
   random_password.keycloak_db_master (auto-generated)
        |                           |
        |                           v
        |            Secrets Manager secret "keycloak/database" {username,password}
        |                           |  (still rotated every 30d; backs the proxy)
        |                           v
        |                    RDS Proxy (iam_auth = REQUIRED)
        |                  ^          |
        |   KC_DB_URL (SSM) =          |  Secrets auth to Aurora
        |   jdbc:aws-wrapper:mysql:// |
        |   <proxy-endpoint>:3306/...  v
   ECS task (Keycloak) -----> Aurora MySQL cluster (master user, password)
        |   task role has rds-db:connect on proxy dbuser
        |   AWS Advanced JDBC Driver "iam" plugin generates 15-min token
        `-- no KC_DB_PASSWORD / KC_DB_USERNAME in container env
```

### Sequence Diagram

```
Keycloak container            AWS JDBC wrapper          RDS Proxy              Aurora          IAM/STS
      |                            |                        |                    |                |
      | openConnection()           |                        |                    |                |
      |--------------------------->|                        |                    |                |
      |                            | generate-db-auth-token |                    |                |
      |                            |  (proxy endpoint,      |                    |                |
      |                            |   db user, region)     |                    |                |
      |                            |----------------------------------------------------->| (SigV4)
      |                            |<-----------------------------------------------------| 15-min token
      |                            | connect(user=db user, password=token)        |                |
      |                            |----------------------->|                    |                |
      |                            |                        | validate IAM token |                |
      |                            |                        | (rds-db:connect)   |                |
      |                            |                        |----read secret---->| ( Secrets Mgr )|
      |                            |                        | connect(user,pwd)  |                |
      |                            |                        |------------------->|                |
      |                            |<----- connected -------|                    |                |
      |<-- connection ready -------|                        |                    |                |
```

### Component Diagram

```
+----------------------------- terraform/aws-ecs -----------------------------+
|                                                                            |
|  locals.is_keycloak_db_iam = var.keycloak_db_iam_auth_enabled              |
|                                                                            |
|  keycloak-database.tf                                                      |
|    aws_db_proxy.keycloak        iam_auth = REQUIRED when on                |
|    aws_ssm_parameter.keycloak_  value = proxy endpoint / cluster endpoint  |
|      database_url                                                          |
|    random_password.keycloak_    count = is_keycloak_db_iam ? 1 : 0         |
|      db_master (new)                                                       |
|    aws_rds_cluster.keycloak     master_password = coalesce(random?, var)   |
|    aws_secretsmanager_secret_   password = same source                     |
|      version.keycloak_db_secret                                            |
|                                                                            |
|  keycloak-ecs.tf                                                           |
|    local.keycloak_container_env    + IAM driver config (conditional)       |
|    local.keycloak_container_secrets - KC_DB_USERNAME/PASSWORD (condit.)    |
|    aws_iam_role_policy.keycloak_   rds-db:connect on proxy dbuser (new)    |
|      task_rds_iam_connect (new)                                            |
|                                                                            |
|  variables.tf   keycloak_db_iam_auth_enabled (new)                         |
|  locals.tf      is_keycloak_db_iam (new)                                   |
|  terraform.tfvars.example   document flag, comment out password            |
|                                                                            |
+-------------------------------- docker/keycloak ----------------------------+
|  Dockerfile   ADD aws-advanced-jdbc-wrapper.jar -> /opt/keycloak/providers  |
+----------------------------------------------------------------------------+
```

## Data Models

This change is infrastructure/config-only; there are no new Pydantic or domain models. The relevant "data shapes" are the Secrets Manager secret payload and the ECS container definition fragments.

### Secrets Manager secret `keycloak/database` (unchanged shape, changed source)

The secret JSON keeps its existing two-field shape so the rotation Lambda and the proxy need no changes:

```json
{
  "username": "keycloak",
  "password": "<auto-generated when IAM on; var.keycloak_database_password when off>"
}
```

In IAM mode the `password` field is populated from `random_password.keycloak_db_master.result` rather than from an operator-supplied variable. The rotation Lambda's `_create_secret` step overwrites `password` with a fresh random value on each rotation, so the auto-generated initial value is only the seed.

### ECS container definition fragments (locals)

```hcl
# IAM-mode-only env entries (added to local.keycloak_container_env via concat)
locals {
  keycloak_iam_driver_env = local.is_keycloak_db_iam ? [
    {
      name  = "KC_DB_URL_DRIVER"
      value = "software.amazon.jdbc.Driver"
    },
    {
      name  = "KC_DB_URL_PROPERTIES"
      value = "?wrapperPlugins=iam&wrapperLoggerLevel=INFO"
    }
  ] : []
}

# Password-mode-only secret entries (already present; conditionally removed)
locals {
  keycloak_password_secrets = local.is_keycloak_db_iam ? [] : [
    {
      name      = "KC_DB_USERNAME"
      valueFrom = "${aws_secretsmanager_secret.keycloak_db_secret.arn}:username::"
    },
    {
      name      = "KC_DB_PASSWORD"
      valueFrom = "${aws_secretsmanager_secret.keycloak_db_secret.arn}:password::"
    }
  ]
}
```

## API / CLI Design

There is no new HTTP endpoint or application CLI. The user-facing interface is Terraform input variables and `terraform` CLI commands.

### New / changed Terraform variable

```bash
# Enable RDS IAM auth for Keycloak's DB connection (default: false = password mode)
TF_VAR_keycloak_db_iam_auth_enabled=true terraform apply
```

### Expected effect of `terraform plan`

With `keycloak_db_iam_auth_enabled=false`: plan is identical to today (no changes).

With `keycloak_db_iam_auth_enabled=true`: plan shows
- `aws_db_proxy.keycloak` updated in place (`iam_auth` DISABLED -> REQUIRED).
- `aws_ssm_parameter.keycloak_database_url` updated in place (value -> proxy endpoint).
- `random_password.keycloak_db_master` created (1).
- `aws_rds_cluster.keycloak` updated in place (`master_password` -> new value).
- `aws_secretsmanager_secret_version.keycloak_db_secret` updated (password -> new value).
- `aws_ecs_task_definition.keycloak` updated (env gains driver config; secrets lose KC_DB_USERNAME/PASSWORD).
- `aws_iam_role_policy.keycloak_task_rds_iam_connect` created (1).

### Error Cases
- `terraform validate` fails if the `random_password` resource is referenced in a non-conditional context (it would not exist in password mode). All references must be `count`-gated or use `coalesce`/`one()`.
- `terraform plan` errors if `rds-db:connect` resource ARN references the proxy resource id before the proxy exists. Use `aws_db_proxy.keycloak` attributes and a depends_on, or `data` lookups where ordering requires it.

## Configuration Parameters

### New Environment Variables (Keycloak container)

| Variable Name | Type | Default | Required | Description |
|---------------|------|---------|----------|-------------|
| `KC_DB_URL_DRIVER` | string | (unset) | Only in IAM mode | JDBC driver class `software.amazon.jdbc.Driver` (AWS Advanced JDBC Driver wrapper) |
| `KC_DB_URL_PROPERTIES` | string | (unset) | Only in IAM mode | URL query activating the `iam` plugin, e.g. `?wrapperPlugins=iam` |

These are non-sensitive and therefore plain `environment` entries, not ECS `secrets`.

### New Terraform Variables

| Variable | Type | Default | Required | Description |
|----------|------|---------|----------|-------------|
| `keycloak_db_iam_auth_enabled` | bool | `false` | No | Master flag. `true` = RDS IAM auth via proxy; `false` = existing password path. |

### Changed Terraform Variables

| Variable | Change |
|----------|--------|
| `keycloak_database_password` | Remains for password-mode fallback. In IAM mode it is unused; the example tfvars comments it out and notes it is only needed when `keycloak_db_iam_auth_enabled = false`. It should gain `default = null` so it can be omitted in IAM mode without a validation error (it is currently required with no default). |

### Settings / Config Class Updates
N/A (no Pydantic settings in the Terraform path). The authoritative config is `variables.tf` + `terraform.tfvars`.

### Deployment Surface Checklist
- [x] `terraform/aws-ecs/variables.tf` - new variable + default change.
- [x] `terraform/aws-ecs/locals.tf` - new `is_keycloak_db_iam` local.
- [x] `terraform/aws-ecs/keycloak-database.tf` - proxy `iam_auth`, conditional URL, random password, secret/cluster password source.
- [x] `terraform/aws-ecs/keycloak-ecs.tf` - conditional env/secrets, task-role IAM policy.
- [x] `terraform/aws-ecs/terraform.tfvars.example` - document flag, comment password.
- [ ] `terraform/aws-ecs/outputs.tf` - (optional) output the proxy endpoint.
- [x] `docker/keycloak/Dockerfile` - vendor wrapper JAR.
- [ ] `.env.example` (root) - only documents docker-compose env vars; the IAM flag is Terraform-only, so no change. (Document this explicitly so reviewers do not expect an `.env` entry.)
- [ ] Helm charts (`charts/**`) - explicitly NOT modified (out of scope).

## New Dependencies

| Package / Artifact | Version | Purpose |
|---------|---------|---------|
| `aws-advanced-jdbc-wrapper` JAR | latest stable (e.g. `2.x`) | Drop-in JDBC driver wrapper whose `iam` plugin generates and refreshes RDS IAM auth tokens; vendored into the Keycloak image at `/opt/keycloak/providers/`. |
| Terraform `random` provider | already in use via `random_password` (verify in `terraform.tf` / `required_providers`) | Generates the Aurora master password in IAM mode. |

If the `random` provider is not already declared, add it to `terraform/aws-ecs/main.tf` `required_providers`. The wrapper JAR is the only new application-level artifact, and it is the enabling dependency for transparent token refresh.

If no new runtime dependencies were needed beyond the above, that would be stated here; the AWS Advanced JDBC Driver wrapper is the one genuinely new runtime dependency.

## Implementation Details

### Step-by-Step Plan (for a future implementer)

#### Step 1: Add the feature flag and local
**File:** `terraform/aws-ecs/variables.tf`
**Lines:** after `keycloak_database_password` block (~line 101)

```hcl
variable "keycloak_db_iam_auth_enabled" {
  description = "Use RDS IAM authentication for Keycloak's DB connection via the RDS Proxy. When false, the existing password-based path is used."
  type        = bool
  default     = false
}
```

Also relax the password variable so it can be omitted in IAM mode:

```hcl
variable "keycloak_database_password" {
  description = "Keycloak database password. Required when keycloak_db_iam_auth_enabled = false. Unused (auto-generated) when IAM auth is enabled."
  type        = string
  sensitive   = true
  default     = null
}
```

Add a validation that the password is supplied when IAM is off:

```hcl
variable "keycloak_database_password" {
  # ... as above ...
  validation {
    condition     = var.keycloak_db_iam_auth_enabled || var.keycloak_database_password != null
    error_message = "keycloak_database_password must be set when keycloak_db_iam_auth_enabled is false."
  }
}
```

**File:** `terraform/aws-ecs/locals.tf`
**Lines:** near the other `is_*` locals (~line 40)

```hcl
  is_keycloak_db_iam = var.keycloak_db_iam_auth_enabled
```

#### Step 2: Auto-generate the master password in IAM mode
**File:** `terraform/aws-ecs/keycloak-database.tf`
**Lines:** near the secret version (~line 268)

```hcl
resource "random_password" "keycloak_db_master" {
  count   = local.is_keycloak_db_iam ? 1 : 0
  length  = 32
  special = true
  override_special = "!#$%&*()-_=+[]{}:?"
}
```

Local that resolves the effective password source:

```hcl
locals {
  keycloak_db_password_value = local.is_keycloak_db_iam ? (
    random_password.keycloak_db_master[0].result
  ) : var.keycloak_database_password
}
```

#### Step 3: Point the Aurora master password and secret at the resolved source
**File:** `terraform/aws-ecs/keycloak-database.tf`
**Lines:** cluster `master_password` (line 54) and secret version `password` (line 272)

```hcl
  master_password = local.keycloak_db_password_value
```

```hcl
  secret_string = jsonencode({
    username = var.keycloak_database_username
    password = local.keycloak_db_password_value
  })
```

Note: changing `master_password` triggers an Aurora master-password rotation. This is a one-time cutover event; schedule it in a maintenance window. The rotation Lambda will continue to rotate it thereafter.

#### Step 4: Enable IAM auth on the proxy and fix TLS
**File:** `terraform/aws-ecs/keycloak-database.tf`
**Lines:** `aws_db_proxy.keycloak` auth block (lines 10-15) and `require_tls` (line 21)

```hcl
  auth {
    auth_scheme               = "SECRETS"
    secret_arn                = aws_secretsmanager_secret.keycloak_db_secret.arn
    client_password_auth_type = "MYSQL_CACHING_SHA2_PASSWORD"
    iam_auth                  = local.is_keycloak_db_iam ? "REQUIRED" : "DISABLED"
  }

  require_tls = local.is_keycloak_db_iam ? true : false
```

IAM auth tokens are only valid over TLS, so `require_tls` must be `true` in IAM mode. The AWS Advanced JDBC Driver wrapper negotiates TLS by default when the `iam` plugin is active (it appends `?sslMode=REQUIRED` semantics). Confirm the wrapper's default `sslMode` in testing; if needed, append `&sslMode=REQUIRED` to `KC_DB_URL_PROPERTIES`.

Update the now-stale checkov skip comment on the cluster:

```hcl
#checkov:skip=CKV_AWS_162:When keycloak_db_iam_auth_enabled=true, IAM database authentication is enforced at the RDS Proxy (iam_auth=REQUIRED); the cluster master credential is the proxy's backend secret, not a client-facing password.
```

#### Step 5: Wire the proxy endpoint into `KC_DB_URL`
**File:** `terraform/aws-ecs/keycloak-database.tf`
**Lines:** `aws_ssm_parameter.keycloak_database_url` value (line 281)

```hcl
  value = local.is_keycloak_db_iam ? (
    "jdbc:aws-wrapper:mysql://${aws_db_proxy.keycloak.endpoint}:3306/keycloak"
  ) : (
    "jdbc:mysql://${aws_rds_cluster.keycloak.endpoint}:3306/keycloak"
  )
```

The `aws-wrapper:mysql` scheme tells the AWS Advanced JDBC Driver to wrap the underlying MySQL/MariaDB driver and enables plugin processing.

#### Step 6: Add the IAM driver env entries and drop the password secret entries
**File:** `terraform/aws-ecs/keycloak-ecs.tf`
**Lines:** the `keycloak_container_env` and `keycloak_container_secrets` locals (lines 15-105)

Add the IAM-mode env local (shown in Data Models) and rewire the two locals:

```hcl
  keycloak_container_env = concat(
    [ /* ...existing entries... */ ],
    local.keycloak_iam_driver_env
  )

  keycloak_container_secrets = concat(
    [
      { name = "KEYCLOAK_ADMIN",        valueFrom = aws_ssm_parameter.keycloak_admin.arn },
      { name = "KEYCLOAK_ADMIN_PASSWORD", valueFrom = aws_ssm_parameter.keycloak_admin_password.arn },
      { name = "KC_DB_URL",             valueFrom = aws_ssm_parameter.keycloak_database_url.arn },
    ],
    local.keycloak_password_secrets
  )
```

In IAM mode `keycloak_password_secrets` is empty, so `KC_DB_USERNAME`/`KC_DB_PASSWORD` are absent from the container. The AWS wrapper `iam` plugin supplies the username (from the connection URL / `user` property) and the token (password) at connection time.

The connection username in IAM mode is the DB user the proxy maps to (`var.keycloak_database_username`, default `keycloak`). Pass it as a URL property so the wrapper uses it for token generation:

```hcl
      value = "?wrapperPlugins=iam&user=${var.keycloak_database_username}&wrapperLoggerLevel=INFO"
```

(`user` is not secret; it is the IAM-mapped DB user name.)

#### Step 7: Grant the task role `rds-db:connect` to the proxy
**File:** `terraform/aws-ecs/keycloak-ecs.tf`
**Lines:** new resource after the existing task role policies (~line 274)

```hcl
resource "aws_iam_role_policy" "keycloak_task_rds_iam_connect" {
  count = local.is_keycloak_db_iam ? 1 : 0
  name  = "keycloak-task-rds-iam-connect"
  role  = aws_iam_role.keycloak_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RdsDbConnectKeycloakProxy"
        Effect = "Allow"
        Action = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_proxy.keycloak.id}/${var.keycloak_database_username}"
      }
    ]
  })
}
```

`data.aws_caller_identity` and `data.aws_region` are already used elsewhere in `keycloak-database.tf`, so they are available. `aws_db_proxy.keycloak.id` is the proxy resource identifier used in the `rds-db` ARN. This policy is attached to the **task role** (the role the container assumes at runtime), not the task-execution role, because the token is generated from inside the running container.

#### Step 8: Vendor the AWS Advanced JDBC Driver wrapper into the image
**File:** `docker/keycloak/Dockerfile`
**Lines:** after the builder stage copies (around line 15)

```dockerfile
# Vendor the AWS Advanced JDBC Driver wrapper so the iam plugin can generate
# short-lived RDS auth tokens. Placed in providers/ so both optimized and
# non-optimized Keycloak starts pick it up.
COPY aws-advanced-jdbc-wrapper.jar /opt/keycloak/providers/aws-advanced-jdbc-wrapper.jar
```

Pin the JAR version by downloading a specific release into the build context (e.g. via a `make` target or a documented fetch step), and document the version + source URL in a comment. The MariaDB JDBC driver bundled with Keycloak remains the underlying target driver; the wrapper delegates to it.

A driver-only change to the image is not a Keycloak version change (base image stays `quay.io/keycloak/keycloak:25.0`).

### Error Handling
- **Missing password in password mode**: handled by the `validation` block on `keycloak_database_password` (plan-time error with a clear message).
- **Proxy not yet created when IAM policy references it**: Terraform resolves `aws_db_proxy.keycloak.id` from the same state; add `depends_on = [aws_db_proxy.keycloak]` to the policy resource if a cycle appears.
- **TLS mismatch**: if the wrapper does not enforce TLS, connections will be rejected by `require_tls = true`. Surface this in testing.md and fix by appending `&sslMode=REQUIRED`.
- **Token generation failure at runtime**: the wrapper logs the IAM failure; Keycloak's connection pool reports a connection error. Mitigate via the IAM policy and task-role trust (verify with the E2E test in testing.md).

### Logging
- Set `wrapperLoggerLevel=INFO` (downgrade to `SEVERE` in production after verification) so token-generation failures are visible in the Keycloak CloudWatch log group `/ecs/keycloak`.
- Add a one-time `terraform` output/log line (via a `null_resource` local exec, optional) printing the connection mode during apply so operators can confirm which path is active. Keep this non-sensitive (mode only, never the password).

## Observability
- **CloudWatch Logs**: Keycloak startup logs will show the datasource driver class and any `iam` plugin errors. The existing log group `/ecs/keycloak` (7-day retention) captures this.
- **RDS Proxy metrics**: `UserConnectionsToProxy`, `DatabaseConnectionRequests`, `IAMAuthenticationFailures` (CloudWatch namespace `AWS/RDS`, dimension `ProxyName=keycloak-proxy`). Add a CloudWatch alarm on `IAMAuthenticationFailures > 0` for the first rollout week.
- **Terraform output**: add `output "keycloak_db_auth_mode"` = `"iam"` or `"password"` so the active mode is queryable post-apply.
- **Rotation**: the existing `/aws/lambda/<name>-rotate-rds` log group continues to record each 30-day rotation; in IAM mode these rotations update the proxy's backend credential and do not affect Keycloak.

## Scaling Considerations
- **Current load**: single Keycloak Fargate task (desired_count = 1), autoscaled 1-4. Aurora Serverless v2 0.5-2 ACU. Connection volume is low.
- **Proxy sizing**: the RDS Proxy already exists; enabling IAM auth does not change its capacity. The proxy pools connections to Aurora, which is the existing intent.
- **Token generation cost**: each new connection triggers an `generate-db-auth-token` SigV4 call (local, cheap). No measurable overhead at this scale.
- **Bottlenecks**: none new. The proxy's connection pool size defaults are adequate for 1-4 tasks.
- **Caching**: not applicable; tokens are short-lived by design and the wrapper refreshes them per connection.

## File Changes

### New Files

| File Path | Description |
|-----------|-------------|
| `docker/keycloak/aws-advanced-jdbc-wrapper.jar` | Vendored AWS Advanced JDBC Driver wrapper JAR (pinned version). Added to build context. |

### Modified Files

| File Path | Lines | Change Description |
|-----------|-------|--------------------|
| `terraform/aws-ecs/variables.tf` | ~10 | Add `keycloak_db_iam_auth_enabled`; add `default = null` + `validation` to `keycloak_database_password` |
| `terraform/aws-ecs/locals.tf` | ~2 | Add `is_keycloak_db_iam` and `keycloak_db_password_value` locals |
| `terraform/aws-ecs/keycloak-database.tf` | ~40 | Proxy `iam_auth`/`require_tls` conditional; `random_password` resource; conditional `KC_DB_URL`; cluster/secret password source; checkov comment update |
| `terraform/aws-ecs/keycloak-ecs.tf` | ~45 | Conditional env (`KC_DB_URL_DRIVER`, `KC_DB_URL_PROPERTIES`); conditional `KC_DB_USERNAME`/`KC_DB_PASSWORD` secrets; new `keycloak_task_rds_iam_connect` policy |
| `terraform/aws-ecs/terraform.tfvars.example` | ~8 | Document `keycloak_db_iam_auth_enabled`; comment out `keycloak_database_password` with a note |
| `terraform/aws-ecs/outputs.tf` | ~5 (optional) | Optional `keycloak_db_auth_mode` and proxy-endpoint outputs |
| `docker/keycloak/Dockerfile` | ~4 | `COPY` the wrapper JAR into `/opt/keycloak/providers/` |
| `docker/keycloak/README` or build notes | ~10 | Document the pinned wrapper version and fetch command |

### Estimated Lines of Code

| Category | Lines |
|----------|-------|
| New Terraform code | ~90 |
| Modified Terraform code | ~40 |
| Dockerfile / build | ~15 |
| New tests (helm-unittest n/a; terraform tests) | ~80 |
| **Total** | **~225** |

## Testing Strategy
The full plan lives in `./testing.md`. Summary: Terraform `validate`/`fmt`/`plan` in both modes, checkov pass, a `terraform plan` no-op assertion for flag=false, an IAM policy assertion for flag=true, and an end-to-end Keycloak-to-DB connectivity check via ECS exec after apply.

## Alternatives Considered

### Alternative 1: Direct IAM DB auth to the Aurora cluster (no proxy)
**Description:** Enable `enable_iam_database_authentication = true` on the cluster, create a `keycloak` DB user with `AWSAuthenticationPlugin` via an init SQL task, and point the wrapper's `iam` plugin at the cluster endpoint.
**Pros / Cons:** Removes the proxy dependency; one fewer moving part. Requires a DB-level IAM user (init SQL + lifecycle), and the master credential still exists for break-glass. Init-SQL lifecycle on Aurora Serverless is fiddly.
**Why Rejected:** More moving parts (init SQL, IAM DB user lifecycle) and does not reuse the already-provisioned RDS Proxy. The proxy approach keeps the rotation Lambda and secret unchanged and gives connection pooling for free.

### Alternative 2: Custom Quarkus `CredentialsProvider` SPI
**Description:** Implement a Java `CredentialsProvider` that calls `aws rds generate-db-auth-token` on each `getCredentials()` and wire it via `quarkus.datasource.credentials-provider`.
**Pros / Cons:** No new JDBC driver; native Quarkus integration. Requires writing and maintaining Java SPI code, a custom image, and careful lifecycle (token refresh per acquisition).
**Why Rejected:** More code to maintain than a vendored, AWS-supported driver wrapper, and harder for an entry-level developer to operate. The wrapper achieves the same result with configuration only.

### Alternative 3: Sidecar token refresher writing the password to a shared file
**Description:** A sidecar generates the IAM token every ~10 minutes and writes it to a shared volume; Keycloak reads it.
**Why Rejected:** Keycloak reads `KC_DB_PASSWORD` once at startup and does not re-read a file per connection. The 15-minute token expiry would break pooled connections. Fundamentally does not work without a driver-level hook.

### Comparison Matrix

| Criteria | Chosen (Proxy + wrapper) | Alt 1 (Direct cluster IAM) | Alt 2 (Custom SPI) | Alt 3 (Sidecar) |
|----------|--------------------------|----------------------------|--------------------|-----------------|
| Complexity | Medium | Medium-High | High | Low (but broken) |
| Reuses existing infra | Yes (proxy, lambda, secret) | No (new init SQL) | Partial | No |
| Token refresh correctness | Yes (driver plugin) | Yes (driver plugin) | Yes (SPI) | No |
| Entry-level operability | Good | Fair | Poor | n/a |
| Backwards-compatible fallback | Yes (flag) | Yes (flag) | Yes (flag) | n/a |

## Rollout Plan
- **Phase 1 - Implementation (out of scope for this skill):** a future implementer applies the LLD steps.
- **Phase 2 - Verification:** run `testing.md` in a non-prod account; confirm flag=false is a no-op and flag=true yields a working IAM-authed Keycloak with no `KC_DB_PASSWORD` in the container env.
- **Phase 3 - Cutover:** in a maintenance window, set `keycloak_db_iam_auth_enabled = true` and `terraform apply`. The apply rotates the Aurora master password once (proxy backend). Monitor `IAMAuthenticationFailures` for one week. Keep the flag toggleable so a rollback to password mode is a single `terraform apply`.
- **Phase 4 - Decommission (future, separate issue):** once IAM mode is proven stable, consider removing the password-mode code path. Out of scope here.

## Open Questions
1. **Wrapper `sslMode` default**: does the AWS Advanced JDBC Driver enforce TLS by default when the `iam` plugin is active, or must `&sslMode=REQUIRED` be appended? Resolve during testing; the LLD defaults to appending it if needed.
2. **Proxy `require_tls` cutover**: flipping `require_tls` to true affects the password-mode path too if the flag is later toggled back. Confirm the password-mode driver also supports TLS (MariaDB driver does, via `sslMode`). If the password path must remain plaintext, the proxy's `require_tls` should itself be conditional and the password path should keep connecting to the cluster endpoint (which it does). This is already handled by conditionalising `require_tls` on the flag, but the password-mode path bypasses the proxy entirely, so `require_tls` only governs the IAM path. Verified consistent.
3. **`random` provider presence**: is the `hashicorp/random` provider already in `required_providers`? If not, add it. (To be confirmed by the implementer in `main.tf`.)
4. **Proxy resource id for `rds-db` ARN**: confirm `aws_db_proxy.keycloak.id` returns the value used in the `dbuser` ARN. AWS docs use the proxy's resource id; the Terraform `id` attribute is the proxy name in some provider versions. Verify in testing; fall back to `aws_db_proxy.keycloak.id` vs a `data.aws_rds_proxy` lookup if mismatched.
5. **Master password cutover blast radius**: changing `master_password` on apply is a one-time disruptive event on the cluster. Confirm the maintenance-window scheduling with the operator before Phase 3.

## References
- AWS RDS IAM database authentication: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html
- RDS Proxy IAM authentication: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-setup.html#rds-proxy-iam-auth
- AWS Advanced JDBC Driver (iam plugin): https://github.com/aws/aws-advanced-jdbc-wrapper
- Keycloak hostname v2 / DB config: https://www.keycloak.org/server/hostname , https://www.keycloak.org/server/db
- Existing repo context: `terraform/aws-ecs/keycloak-database.tf` (issue #1026 comment on SSM removal), `terraform/aws-ecs/keycloak-ecs.tf` (KC25 env notes)
