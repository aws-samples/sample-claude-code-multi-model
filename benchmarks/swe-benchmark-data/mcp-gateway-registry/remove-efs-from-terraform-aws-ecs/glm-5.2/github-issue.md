# GitHub Issue: Remove Amazon EFS from the Terraform AWS ECS deployment

## Title
Remove Amazon EFS from the Terraform AWS ECS configuration and documentation

## Labels
- refactor
- infra
- terraform
- tech-debt

## Description

### Problem Statement
The Terraform AWS ECS deployment for the MCP Gateway Registry still provisions an Amazon EFS file system and mounts it into two ECS services (auth-server and mcpgw), even though the codebase has already migrated its primary persistence layer off EFS. The registry service itself was moved to ephemeral storage plus Amazon DocumentDB (see the existing comments in `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` at the registry service: "EFS volumes removed - registry now uses ephemeral storage and DocumentDB for persistence"), but the EFS file system, its access points, security group, module-level variables, outputs, the scopes-init task runner, and several documentation references were never cleaned up.

This leaves the deployment in a half-migrated state with several concrete costs:
- Every `terraform apply` still creates and pays for an EFS file system, six access points, mount targets in every private subnet, and an NFS security group, even though only two services reference it and one of those (auth-server) only needs it for a single config file.
- The post-deployment script `terraform/aws-ecs/scripts/post-deployment-setup.sh` still treats EFS as the default scopes-initialization backend and still lists `mcp_gateway_efs_id` as a required Terraform output, so a deployment that correctly uses DocumentDB-only mode is forced to keep emitting EFS outputs or the validation step fails.
- The `run-scopes-init-task.sh` script, the `docker/Dockerfile.scopes-init` image, and the example IAM policy in `terraform/aws-ecs/README.md` (`elasticfilesystem:*`) all exist solely to support EFS and have no purpose once EFS is gone.
- Documentation (`README.md`, `terraform/README.md`, `docs/deployment-modes.md`, `docs/architecture-diagrams.md`) still advertises EFS as the shared/persistent storage layer, which misleads operators and new contributors.

The goal of this issue is to finish the migration: remove every EFS resource, variable, output, script, and documentation reference from the Terraform AWS ECS stack, and route the two remaining EFS consumers (auth-server scopes config and mcpgw `/app/data`) onto the same ephemeral-plus-DocumentDB pattern the registry already uses.

### Proposed Solution
Remove the EFS surface entirely from the `terraform/aws-ecs/modules/mcp-gateway` module and the root `terraform/aws-ecs` stack, and update the deployment scripts and docs to match. Concretely:

1. Delete the `module "efs"` block and its manual egress rule from `terraform/aws-ecs/modules/mcp-gateway/storage.tf` (the `data.aws_vpc.vpc` data source stays, since `ecs-services.tf` still uses it).
2. Remove the EFS volume configurations and mount points from the auth-server and mcpgw ECS service definitions in `ecs-services.tf`, mirroring the pattern already applied to the registry service (`volume = {}`, `mountPoints = []`).
3. Repoint the auth-server `SCOPES_CONFIG_PATH` environment variable away from `/efs/auth_config/...` to a path baked into the container image (the scopes file already lives in-repo at `auth_server/scopes.yml`), so scopes no longer depend on a runtime EFS mount.
4. Remove the `efs_id`, `efs_arn`, and `efs_access_points` outputs from the module `outputs.tf` and the matching `mcp_gateway_efs_*` outputs from the root `terraform/aws-ecs/outputs.tf`.
5. Remove the `efs_throughput_mode` and `efs_provisioned_throughput` variables from the module `variables.tf` (the root never passed them, so no root change is needed there).
6. Delete `terraform/aws-ecs/scripts/run-scopes-init-task.sh` (entirely EFS-based) and remove the EFS branch from `_initialize_scopes()` in `post-deployment-setup.sh`, making DocumentDB (via the existing `run-documentdb-init.sh`) the only scopes-initialization path; drop `mcp_gateway_efs_id` from the required-outputs validation.
7. Remove the now-orphaned `docker/Dockerfile.scopes-init` and its build script.
8. Update documentation to remove EFS references: `README.md`, `terraform/README.md`, `terraform/aws-ecs/README.md` (remove `elasticfilesystem:*` from the example IAM policy), `docs/deployment-modes.md`, and `docs/architecture-diagrams.md`.

### User Stories
- As a platform operator, I want the Terraform AWS ECS stack to stop provisioning EFS so that I am not paying for an unused file system and its mount targets on every deployment.
- As a platform operator, I want scopes initialization to use DocumentDB as the only backend so that post-deployment validation no longer fails when EFS outputs are absent.
- As a new contributor, I want the README and architecture docs to reflect the actual storage layer (ephemeral storage plus DocumentDB) so that I am not misled into thinking EFS is still in use.
- As an SRE, I want the example IAM policy to drop `elasticfilesystem:*` so that the documented least-privilege policy matches what the deployment actually needs.

### Acceptance Criteria
- [ ] `terraform/aws-ecs/modules/mcp-gateway/storage.tf` no longer defines `module "efs"` or the `efs_all_outbound` security group egress rule.
- [ ] `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` has no `efs_volume_configuration` blocks and no `module.efs` references; the auth-server and mcpgw services use `volume = {}` / `mountPoints = []` (or an equivalent non-EFS volume) consistent with the registry service.
- [ ] The auth-server `SCOPES_CONFIG_PATH` environment variable points at a non-EFS path (image-baked) and the auth-server container starts and loads scopes without an EFS mount.
- [ ] `terraform/aws-ecs/modules/mcp-gateway/outputs.tf` no longer exposes `efs_id`, `efs_arn`, or `efs_access_points`.
- [ ] `terraform/aws-ecs/outputs.tf` no longer exposes `mcp_gateway_efs_id`, `mcp_gateway_efs_arn`, or `mcp_gateway_efs_access_points`.
- [ ] `terraform/aws-ecs/modules/mcp-gateway/variables.tf` no longer declares `efs_throughput_mode` or `efs_provisioned_throughput`.
- [ ] `terraform/aws-ecs/scripts/run-scopes-init-task.sh` is deleted, and `post-deployment-setup.sh` no longer references EFS, no longer calls `run-scopes-init-task.sh`, and no longer lists `mcp_gateway_efs_id` as a required output; DocumentDB is the only scopes path.
- [ ] `docker/Dockerfile.scopes-init` and its build script are removed (or explicitly justified if retained).
- [ ] No file under `terraform/`, `docs/`, `README.md`, or `docker/` contains an EFS or `elasticfilesystem` reference (verified by grep), except where EFS is mentioned in historical release notes.
- [ ] `terraform validate` and `terraform plan` succeed against `terraform/aws-ecs` with no EFS resources in the plan, and `terraform plan` shows the EFS file system, access points, mount targets, and NFS security group being destroyed.
- [ ] Existing tests (Helm unittest suites, shell syntax checks) still pass; any test referencing EFS outputs is updated.

### Out of Scope
- Removing EFS from any deployment surface other than Terraform AWS ECS (the Helm/EKS charts and Docker Compose files are separate and not part of this issue).
- Changing the mcpgw application's data model beyond repointing its `/app/data` mount off EFS; if mcpgw requires durable state, that migration is tracked separately (see Open Questions in the LLD).
- Rewriting the scopes loader in the auth-server Python code; this issue only changes where `scopes.yml` is located (image-baked path) and how it is provisioned (DocumentDB init, not an EFS copy task).
- Historical/release-note edits: `release-notes/` entries that mention EFS are immutable history and are left as-is.

### Dependencies
- The DocumentDB init path (`terraform/aws-ecs/scripts/run-documentdb-init.sh`) must already be functional, which it is in this tag. This issue makes DocumentDB the required backend, so a deployment without a DocumentDB endpoint will fail scopes initialization by design.

### Related Issues
- The in-progress EFS-to-DocumentDB migration (evidenced by the registry service comments and the dual-mode `_initialize_scopes` logic). This issue completes that migration for the Terraform AWS ECS surface.
