# GitHub Issue: Migrate ECS Environment Variables to AWS Secrets Manager

## Title
Move sensitive ECS environment variables into AWS Secrets Manager across all ECS services (registry, auth-server, keycloak, metrics-service, mcpgw)

## Labels
- enhancement
- security
- infra
- refactor

## Description

### Problem Statement
The ECS task definitions for the MCP Gateway services currently pass a number of sensitive values as plaintext `environment` entries in the container definitions. These values flow from `terraform.tfvars` / Terraform variables into the `container_definitions` JSON, which means they land in plaintext in Terraform state, in the task definition payload stored by ECS, and in any plan/apply logs. A partial Secrets Manager integration already exists: `SECRET_KEY`, the Keycloak client secrets, `KEYCLOAK_ADMIN_PASSWORD`, `EMBEDDINGS_API_KEY`, the DocumentDB credentials, the IdP (Entra/Okta/Auth0) client secrets, `METRICS_API_KEY`, and the OTLP exporter headers are already injected through the ECS `secrets` block. However, several equally sensitive values are still wired through the plaintext `environment` block, including:

- `AUTH0_MANAGEMENT_API_TOKEN`
- `REGISTRY_API_TOKEN` and `REGISTRY_API_KEYS`
- `FEDERATION_STATIC_TOKEN` and `FEDERATION_ENCRYPTION_KEY`
- `ANS_API_KEY` and `ANS_API_SECRET`
- `GITHUB_PAT` and `GITHUB_APP_PRIVATE_KEY`
- `REGISTRATION_WEBHOOK_AUTH_TOKEN` and `REGISTRATION_WEBHOOK_AUTH_HEADER`
- `REGISTRATION_GATE_AUTH_CREDENTIAL` and `REGISTRATION_GATE_OAUTH2_CLIENT_SECRET`

These remaining plaintext secrets should be migrated to AWS Secrets Manager so they benefit from KMS encryption at rest, centralized rotation, cross-account access, and auditability via CloudTrail, and so they no longer appear in plaintext in Terraform state or ECS task definitions.

The destination store is AWS Secrets Manager only (not SSM Parameter Store). Secrets Manager is chosen because it provides automatic rotation Lambda support and native cross-account resource-policy access, both of which are required by the platform.

### Proposed Solution
1. Create one Secrets Manager secret (KMS-encrypted with the existing `aws_kms_key.secrets`) for each sensitive value that is currently passed as a plaintext ECS environment variable, seeded from the existing Terraform variable so existing deployments continue to work without operator action.
2. For each migrated value, remove the entry from the container `environment` list and add a corresponding entry to the container `secrets` list (referencing the new secret ARN, with a JSON-key stage where the secret stores a structured object). ECS resolves the secret at task start and injects it as the same environment variable name, so the running application sees no change.
3. Extend the existing `ecs_secrets_access` IAM policy (and the Keycloak task-execution role policy, where applicable) to grant `secretsmanager:GetSecretValue` on each new secret ARN.
4. Add a small, shared, cached app-side secret loader (`boto3`-based) that implements a migration fallback: for each migrated setting, use the environment variable if it is present and non-empty (preserving today's behavior on surfaces that have not been migrated yet, e.g. local Docker Compose and CI); otherwise fetch the value from Secrets Manager by ARN. This keeps the migration safe and reversible and supports non-ECS deployment surfaces that do not perform ECS secret injection. The fallback is temporary and will be removed in a follow-up once all surfaces are migrated.
5. Wire each new secret's ARN into the affected services through a new set of `*_SECRET_ARN` environment variables so the app loader knows which secret to fetch when the plaintext env var is absent.

The change is scoped to the Terraform AWS-ECS deployment surface plus the application configuration loaders. Helm/EKS parity is explicitly out of scope for this issue.

### User Stories
- As a platform SRE, I want sensitive configuration to live in AWS Secrets Manager so that it is encrypted at rest with KMS and never written to Terraform state or ECS task definitions in plaintext.
- As a security engineer, I want every sensitive ECS env var to be backed by a Secrets Manager secret so that rotation and cross-account access can be governed centrally.
- As an operator migrating an existing deployment, I want the migration to be backward compatible so that setting the plaintext variable still works until my tfvars are updated, with no service downtime.
- As a developer running the stack locally via Docker Compose, I want the application to fall back to fetching secrets from Secrets Manager (or a local env var) so I do not need ECS-specific secret injection to run the services.

### Acceptance Criteria
- [ ] Every sensitive environment variable currently passed as plaintext in the `environment` block of the registry, auth-server, keycloak, metrics-service, and mcpgw container definitions is backed by an AWS Secrets Manager secret created in Terraform.
- [ ] Each migrated value is removed from the container `environment` list and added to the container `secrets` list, referencing the new secret ARN.
- [ ] The `ecs_secrets_access` IAM policy grants `secretsmanager:GetSecretValue` on every newly created secret ARN, and the Keycloak task-execution role is updated for any Keycloak-side migration.
- [ ] All new secrets are encrypted with the existing `aws_kms_key.secrets` and tagged with `local.common_tags`.
- [ ] The application configuration loaders (registry `core/config.py`, auth-server `server.py`, metrics-service `app/config.py`) support a fallback resolver that returns the plaintext env var when present, otherwise fetches from Secrets Manager by ARN, with results cached for the process lifetime.
- [ ] A `*_SECRET_ARN` environment variable is wired into each affected ECS service for each migrated secret, and is listed in `.env.example` and the Terraform variables.
- [ ] Existing deployments continue to start and function with no tfvars changes required (secrets are seeded from the current variable values).
- [ ] `terraform plan` against a representative configuration shows no plaintext secret values in the diff; sensitive values are marked `sensitive = true` in the Terraform variables.
- [ ] No new runtime third-party dependency is added; `boto3`, already a project dependency, is used for the app-side loader.
- [ ] Helm charts and EKS manifests are not modified (out of scope).

### Out of Scope
- Helm chart / EKS parity changes.
- Removing the plaintext env-var fallback (tracked as a follow-up issue to be opened after this migration is verified in production).
- Rotating the newly created application secrets (e.g. `REGISTRY_API_TOKEN`) on a schedule. Rotation Lambdas exist only for database credentials today; wiring rotation for application secrets is a separate effort.
- Migrating the Keycloak SSM Parameter Store parameters (`/keycloak/admin`, `/keycloak/admin_password`, `/keycloak/database_url`) to Secrets Manager. Keycloak already sources its DB credentials from Secrets Manager; the SSM-backed admin parameters are out of scope unless they are found to be ECS env vars in plaintext.
- Changes to the docker-compose deployment files beyond what is required to expose the new `*_SECRET_ARN` variables for local development.

### Dependencies
- The existing `aws_kms_key.secrets` and the `ecs_secrets_access` IAM policy in `terraform/aws-ecs/modules/mcp-gateway/`.
- The existing Secrets Manager + rotation-Lambda patterns in `terraform/aws-ecs/secret-rotation.tf` and `secret-rotation-config.tf` (for reference; this issue does not add new rotators).

### Related Issues
- (Open) Remove the plaintext env-var fallback once all deployment surfaces are migrated (to be created as a follow-up).
- Reference: PR #947 (MongoDB connection string Secrets Manager variant) and issue #1026 (Keycloak DB credentials sourced from Secrets Manager rotation Lambda) established the patterns this migration follows.
