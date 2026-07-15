# GitHub Issue: Replace Keycloak DB password with RDS IAM authentication

## Title
Use RDS IAM authentication for Keycloak's database connection and remove the static DB password from config/env vars

## Labels
- enhancement
- security
- infra

## Description

### Problem Statement
The Keycloak service deployed via `terraform/aws-ecs` connects to its Aurora MySQL database using a static `master_password`. That password is supplied by the operator in `terraform.tfvars` (`keycloak_database_password = "CHANGE-ME-DB-PASSWORD"`), stored in a Secrets Manager secret (`keycloak/database`), injected into the Keycloak ECS task as the `KC_DB_PASSWORD` environment variable, and rotated every 30 days by the `rotate-rds` Lambda.

This means a long-lived database secret is present in operator-supplied configuration, is materialised into the Keycloak container environment, and is the sole authentication factor between Keycloak and the database. A leak of the tfvars file, the secret value, or the container environment is sufficient to authenticate to the database for the full rotation window. There is also an orphaned RDS Proxy (`aws_db_proxy.keycloak`) that is provisioned but whose endpoint is never wired into the Keycloak connection string, so the connection-pooling and IAM-authentication surface it could provide is unused.

The desired end state is that Keycloak authenticates to the database using short-lived AWS IAM database authentication tokens (15-minute lifetime, generated from the ECS task role) instead of a static password, and that the static DB password is removed from operator-supplied config and from the Keycloak container environment entirely.

### Proposed Solution
Introduce a feature-flagged RDS IAM authentication path for Keycloak's database connection, anchored to the existing Terraform/ECS deployment:

1. Repurpose the existing (currently orphaned) RDS Proxy as the IAM authentication boundary. When the flag is on, set the proxy `iam_auth` to `REQUIRED` so every connection to the proxy must present an IAM auth token; the proxy continues to authenticate to Aurora using the Secrets Manager secret. The static password therefore leaves Keycloak's environment and lives only inside the proxy, backed by Secrets Manager and the existing rotation Lambda.
2. Point Keycloak's `KC_DB_URL` at the **proxy endpoint** (instead of the cluster endpoint) when the flag is on, via the existing SSM parameter `/keycloak/database/url`.
3. Make the Keycloak ECS task role allowed to generate IAM auth tokens for the proxy by granting `rds-db:connect` on the proxy's `dbuser` resource.
4. Give Keycloak a JDBC driver that generates and refreshes the IAM auth token transparently (the AWS Advanced JDBC Driver wrapper with the `iam` plugin), added to the Keycloak image. No Keycloak version change (still Keycloak 25.0).
5. In IAM mode, stop reading the static password from the secret into the container (drop the `KC_DB_PASSWORD` and `KC_DB_USERNAME` secret injections) and auto-generate the Aurora master password instead of requiring the operator to supply it.
6. Keep the existing password-based path fully intact behind the flag (default off) so deployments that cannot move to IAM auth yet continue to work unchanged.

### User Stories
- As a platform operator, I want Keycloak to authenticate to RDS using IAM so that no long-lived database password is stored in my tfvars or injected into the container environment.
- As a security engineer, I want the database credential to be a short-lived token bound to the ECS task role so that a leaked token is useless after 15 minutes and access is revocable via IAM.
- As a platform operator, I want to opt into IAM auth with a single flag so that I can roll it out per environment without a breaking change.
- As an SRE, I want the existing password rotation Lambda and Secrets Manager secret to keep working so that the proxy's backend credential stays rotated even after the cutover.

### Acceptance Criteria
- [ ] A new boolean variable `keycloak_db_iam_auth_enabled` (default `false`) gates the entire change. With it `false`, the deployment is byte-for-byte equivalent to today's password-based deployment.
- [ ] When the flag is `true`: the RDS Proxy `iam_auth` is set to `REQUIRED`; Keycloak's `KC_DB_URL` resolves to the proxy endpoint; the Keycloak task no longer receives `KC_DB_USERNAME` or `KC_DB_PASSWORD` from the secret.
- [ ] When the flag is `true`: the Aurora master password is auto-generated (not operator-supplied) and written to the Secrets Manager secret used by the proxy; the operator is no longer required to set `keycloak_database_password`.
- [ ] When the flag is `true`: the Keycloak task role has an IAM policy granting `rds-db:connect` for the proxy's `dbuser` resource scoped to the Keycloak database user.
- [ ] When the flag is `true`: the Keycloak image includes the AWS Advanced JDBC Driver wrapper and is configured to use the `iam` plugin so the 15-minute auth token is generated and refreshed automatically per connection.
- [ ] The existing `rotate-rds` Lambda and the `keycloak/database` secret rotation schedule continue to function in both modes.
- [ ] `terraform plan` with the flag `false` produces no changes versus a pre-change plan; `terraform plan` with the flag `true` is valid and free of unresolvable references.
- [ ] `terraform validate`, `terraform fmt`, and `checkov` pass (the now-stale `CKV_AWS_162` skip is updated or removed where IAM auth is enabled).
- [ ] The Helm/EKS surfaces are not touched (out of scope).

### Out of Scope
- Helm/EKS deployment surface changes (this issue targets the Terraform/ECS surface only).
- Changing the Keycloak version (remains 25.0).
- Removing the Secrets Manager secret or the rotation Lambda (still needed to back the proxy's backend credential and for break-glass).
- Switching the database engine from Aurora MySQL to PostgreSQL (the docker-compose Postgres path is unrelated and not changed).
- Direct IAM DB auth to the Aurora cluster endpoint (the proxy-based approach is chosen; see LLD alternatives).

### Dependencies
- The existing RDS Proxy resource (`aws_db_proxy.keycloak`) and its target group (`aws_db_proxy_target.keycloak`) must be wired into the connection path. They already exist in `terraform/aws-ecs/keycloak-database.tf` but are currently unused.
- The AWS Advanced JDBC Driver wrapper JAR must be vendored into the Keycloak image build (`docker/keycloak/Dockerfile`).

### Related Issues
- Issue #1026 (DB username/password now read from the Secrets Manager secret managed by the rotation Lambda) - this change reuses that secret as the proxy's backend credential and stops injecting it into the Keycloak container in IAM mode.
- Issue #955 (conditional/gated Terraform resources) - the same `count`-based gating pattern is reused for the IAM-auth resources.
