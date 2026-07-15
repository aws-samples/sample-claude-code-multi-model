# Low-Level Design: Remove Amazon EFS from the Terraform AWS ECS deployment

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
The `terraform/aws-ecs` stack provisions an Amazon EFS file system that is no longer the intended persistence layer. The registry service already migrated to ephemeral Fargate storage plus Amazon DocumentDB (the comments at `ecs-services.tf:1367` and `ecs-services.tf:1419` document this), but the rest of the EFS surface was never removed. The EFS file system, six access points (`servers`, `models`, `logs`, `agents`, `auth_config`, `mcpgw_data`), per-subnet mount targets, an NFS security group plus a manual all-egress rule, two module-level variables, three module outputs, three root outputs, one scopes-init task-runner script, one scopes-init Docker image, one branch of the post-deployment script, one required-output validation entry, and one example IAM policy entry all still exist solely to support EFS.

Two ECS services still consume EFS at runtime:
- `ecs_service_auth` mounts the `logs` access point at `/app/logs` and the `auth_config` access point at `/efs/auth_config`, and sets `SCOPES_CONFIG_PATH = "/efs/auth_config/auth_config/scopes.yml"`.
- `ecs_service_mcpgw` mounts the `mcpgw_data` access point at `/app/data`.

The post-deployment script `post-deployment-setup.sh` still treats EFS as the default scopes backend (the `else` branch of `_initialize_scopes()`) and still requires `mcp_gateway_efs_id` in the Terraform outputs validation list.

This design finishes the migration by removing every EFS resource, variable, output, script, and documentation reference, and routing the two remaining consumers onto the ephemeral-plus-DocumentDB pattern already used by the registry service.

### Goals
- Remove all EFS resources from `terraform apply` so the file system, access points, mount targets, and NFS security group are destroyed on the next apply.
- Remove all EFS variables and outputs so the module and root surfaces no longer mention EFS.
- Remove the EFS-only scopes-init task runner and the EFS branch of the post-deployment script, making DocumentDB the only scopes-initialization path.
- Repoint the auth-server scopes config to an image-baked path so it no longer depends on a runtime EFS mount.
- Remove all EFS references from documentation and the example IAM policy.
- Keep the change contained to the `terraform/aws-ecs` surface and the docs; do not touch the Helm/EKS charts or Docker Compose files.

### Non-Goals
- Migrating mcpgw's durable application state (if any) to DocumentDB or S3. This design removes the EFS mount from mcpgw and uses ephemeral Fargate storage for `/app/data`; whether mcpgw needs durable state is flagged as an open question for the mcpgw service owners (see Open Questions).
- Rewriting the auth-server Python scopes loader. Only the `SCOPES_CONFIG_PATH` value and the provisioning mechanism change.
- Editing historical release notes that mention EFS.
- Touching the Helm charts (`charts/`) or Docker Compose files, which are separate deployment surfaces.

## Codebase Analysis

### Key Files Reviewed

| File/Directory | Purpose | Relevance to This Change |
|----------------|---------|--------------------------|
| `terraform/aws-ecs/modules/mcp-gateway/storage.tf` | Defines `module "efs"` (terraform-aws-modules/efs/aws ~> 2.0) with six access points, mount targets, NFS ingress, and a manual `aws_vpc_security_group_egress_rule "efs_all_outbound"`. | Core file to delete. The `data.aws_vpc.vpc` data source it references is defined elsewhere and stays. |
| `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` | Defines all ECS services. The registry service already has `volume = {}` and `mountPoints = []` with "EFS volumes removed" comments. The auth-server service still mounts `mcp-logs` and `auth-config` EFS volumes (lines 482-557) and sets `SCOPES_CONFIG_PATH` to `/efs/...` (line 221). The mcpgw service still mounts `mcpgw-data` EFS volume (lines 1803-1867). | Remove EFS volume/mount blocks from auth-server and mcpgw; repoint `SCOPES_CONFIG_PATH`. |
| `terraform/aws-ecs/modules/mcp-gateway/outputs.tf` | Module outputs including `efs_id`, `efs_arn`, `efs_access_points` (lines 47-69). | Remove the three EFS outputs. |
| `terraform/aws-ecs/modules/mcp-gateway/variables.tf` | Module variables including `efs_throughput_mode` (default `bursting`) and `efs_provisioned_throughput` (default `100`) at lines 259-274. | Remove the two EFS variables. |
| `terraform/aws-ecs/modules/mcp-gateway/data.tf` | Defines `data "aws_vpc" "vpc"` at line 8. | Stays; `ecs-services.tf` (lines 2122, 2245) still uses `data.aws_vpc.vpc.cidr_block`. |
| `terraform/aws-ecs/modules/mcp-gateway/locals.tf` | Defines `name_prefix = var.name` and `common_tags`. | Stays; no EFS references. |
| `terraform/aws-ecs/modules/mcp-gateway/iam.tf` | ECS task execution and task IAM policies. | No `elasticfilesystem` permissions are granted in code (verified by grep); the only `elasticfilesystem` reference is in the example policy in `terraform/aws-ecs/README.md`. No `.tf` IAM change needed. |
| `terraform/aws-ecs/outputs.tf` | Root outputs including `mcp_gateway_efs_id`, `mcp_gateway_efs_arn`, `mcp_gateway_efs_access_points` (lines 67-81). | Remove the three root EFS outputs. |
| `terraform/aws-ecs/variables.tf` | Root variables. | No EFS references; no change. The root never passes EFS vars to the module. |
| `terraform/aws-ecs/main.tf` | Root `module "mcp_gateway"` invocation (line 23). | No EFS vars are passed to the module (verified by grep); no change. |
| `terraform/aws-ecs/scripts/run-scopes-init-task.sh` | Reads `mcp_gateway_efs_id` and `mcp_gateway_efs_access_points.auth_config` from Terraform outputs, registers an ECS task that mounts the `auth-config` EFS volume at `/mnt`, and runs the scopes-init container to copy `scopes.yml` onto EFS. Entirely EFS-based. | Delete the file. |
| `terraform/aws-ecs/scripts/post-deployment-setup.sh` | Orchestrates post-deploy steps. `_initialize_scopes()` (line 513) detects a DocumentDB endpoint and runs `run-documentdb-init.sh`; the `else` branch (lines 548-565) is the EFS default that calls `run-scopes-init-task.sh`. Line 218 lists `mcp_gateway_efs_id` in `required_outputs`. | Remove the EFS `else` branch, drop `mcp_gateway_efs_id` from required outputs, update the step-6 comment. |
| `terraform/aws-ecs/scripts/run-documentdb-init.sh` | Existing DocumentDB initialization (indexes + scopes). | Already exists; becomes the only scopes path. No change required. |
| `docker/Dockerfile.scopes-init` | Builds the scopes-init image that copies `auth_server/scopes.yml` to `/mnt` (the EFS mount). | Delete; the scopes file will be baked into the auth-server image instead. |
| `auth_server/scopes.yml` | The scopes definition file (10 KB, in-repo). | Stays; it is the source of truth and will be copied into the auth-server image at build time. |
| `README.md` (line 817) | "EFS Shared Storage - Persistent storage for models, logs, and configuration". | Remove or rewrite the line. |
| `terraform/README.md` (line 16) | "Amazon EFS for persistent storage". | Remove or rewrite the line. |
| `terraform/aws-ecs/README.md` (line 1056) | Example IAM policy includes `"elasticfilesystem:*"`. | Remove the `elasticfilesystem:*` action from the example policy. |
| `docs/deployment-modes.md` (line 211) | Troubleshooting: "the MCP scopes haven't been initialized on EFS". | Rewrite to reference DocumentDB. |
| `docs/architecture-diagrams.md` (line 519) | "Encryption at rest: KMS (DocumentDB, EBS, EFS, S3)". | Remove EFS from the list. |

### Existing Patterns Identified
1. **EFS removal precedent (registry service)**: The registry service in `ecs-services.tf` already had its EFS volumes removed and replaced with `volume = {}` and `mountPoints = []`, plus a comment explaining the migration to ephemeral storage plus DocumentDB. Files: `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` (lines 1367-1369, 1419-1420). How a future implementer should follow this: apply the exact same shape (`volume = {}`, `mountPoints = []`, and a one-line comment) to the auth-server and mcpgw services, then remove the underlying `module.efs` so the dangling references cannot survive.
2. **Dual-backend scopes initialization**: `_initialize_scopes()` in `post-deployment-setup.sh` already branches on the presence of a DocumentDB endpoint in the Terraform outputs. Files: `terraform/aws-ecs/scripts/post-deployment-setup.sh` (lines 523-565). How a future implementer should follow this: delete the `else` (EFS) branch and keep only the DocumentDB branch, so the function fails fast when no DocumentDB endpoint is present rather than silently falling back to EFS.
3. **Image-baked config**: `docker/Dockerfile.scopes-init` already `COPY auth_server/scopes.yml /scopes.yml` into an image, proving the scopes file is treated as a build-time artifact. How a future implementer should follow this: in the auth-server Dockerfile, copy `auth_server/scopes.yml` to an in-image path (e.g., `/app/scopes.yml`) and point `SCOPES_CONFIG_PATH` at it, eliminating the runtime EFS dependency.

### Integration Points

| Component | Integration Type | Details |
|-----------|------------------|---------|
| `module "efs"` in `storage.tf` | Depends on | `data.aws_vpc.vpc` (defined in `data.tf`) and `var.private_subnet_ids`, `var.vpc_id`, `local.name_prefix`, `local.common_tags`, `var.efs_throughput_mode`, `var.efs_provisioned_throughput`. Removing the module removes all these references from `storage.tf`; the data source and the other variables stay because they are used elsewhere. |
| `ecs_service_auth` volume block | Uses | `module.efs.id` and `module.efs.access_points["logs"].id` / `["auth_config"].id` (lines 544-553). Must be removed; the auth-server `/app/logs` path becomes ephemeral container storage (logs already ship to CloudWatch) and `/efs/auth_config` is replaced by an image-baked scopes path. |
| `ecs_service_auth` env var | Uses | `SCOPES_CONFIG_PATH = "/efs/auth_config/auth_config/scopes.yml"` (line 221). Repoint to the image-baked path. |
| `ecs_service_mcpgw` volume block | Uses | `module.efs.id` and `module.efs.access_points["mcpgw_data"].id` (lines 1861-1863), mounted at `/app/data` (line 1806). Remove the volume; `/app/data` becomes ephemeral Fargate storage. |
| Module `outputs.tf` | Exposes | `efs_id`, `efs_arn`, `efs_access_points`. Remove all three. |
| Root `outputs.tf` | Exposes | `mcp_gateway_efs_id`, `mcp_gateway_efs_arn`, `mcp_gateway_efs_access_points` (which forward the module outputs). Remove all three. |
| `post-deployment-setup.sh` | Uses | `mcp_gateway_efs_id` output (required-outputs validation, line 218) and `run-scopes-init-task.sh` (EFS branch, line 560). Remove both; route to `run-documentdb-init.sh` only. |

### Constraints and Limitations Discovered
- EFS is fully contained inside the `mcp-gateway` sub-module: the root `terraform/aws-ecs/variables.tf` declares no EFS variables and `terraform/aws-ecs/main.tf` passes no EFS variables to `module "mcp_gateway"` (verified by grep). This means the removal has no blast radius outside the module and the root outputs.
- The `data.aws_vpc.vpc` data source is shared: `storage.tf` uses it for the EFS NFS ingress rule, but `ecs-services.tf` (lines 2122 and 2245) also uses it for other services' ingress rules. The data source in `data.tf` must stay; only the `storage.tf` consumer is removed.
- No `.tf` IAM policy grants `elasticfilesystem:*`. The only such reference is in the example policy documented in `terraform/aws-ecs/README.md`. This means there is no IAM policy resource to edit in Terraform; the README example is the sole IAM touch-point. (If the deployment relies on a customer-managed policy applied out-of-band, that is outside this repo and out of scope.)
- `run-scopes-init-task.sh` references a build script `$SCRIPT_DIR/build-and-push-scopes-init.sh` (line 54) that is not present in `terraform/aws-ecs/scripts/`. This suggests the EFS scopes-init flow may already be partially decommissioned; regardless, the script and its Dockerfile are deleted wholesale.
- The mcpgw service mounts EFS at `/app/data`, which implies stateful data. Removing the EFS mount makes `/app/data` ephemeral. If mcpgw stores durable state there, that state will be lost on task restart. This is the single biggest risk and is flagged in Open Questions; the implementer must confirm with the mcpgw service owners whether `/app/data` is transient before applying.

## Architecture

### System Context Diagram
```
                        +-----------------------------+
                        |   terraform/aws-ecs (root)  |
                        |  main.tf / variables.tf     |
                        |  outputs.tf                 |
                        +-------------+---------------+
                                      | module "mcp_gateway"
                                      v
        +---------------------------------------------------+
        |     modules/mcp-gateway  (sub-module)             |
        |                                                   |
        |   storage.tf   ----[ REMOVE module.efs + SG ]---- |
        |   ecs-services.tf                                   |
        |     ecs_service_auth   --[ remove EFS volumes ]--> |
        |       SCOPES_CONFIG_PATH --[ repoint to image ]--> |
        |     ecs_service_registry --[ already EFS-free ]--> |
        |     ecs_service_mcpgw   --[ remove EFS volume ]-->|
        |   outputs.tf --[ remove efs_* outputs ]----------> |
        |   variables.tf --[ remove efs_* vars ]-----------> |
        |   data.tf (data.aws_vpc.vpc) --[ KEEP ]----------> |
        +---------------------------------------------------+
                                      |
                                      v
                        +-----------------------------+
                        |  scripts/ (post-deploy)    |
                        |  post-deployment-setup.sh   |
                        |   _initialize_scopes()      |
                        |     DocumentDB branch  KEEP |
                        |     EFS branch        REMOVE|
                        |  run-scopes-init-task.sh REMOVE (delete file) |
                        |  run-documentdb-init.sh KEEP (only path)      |
                        +-----------------------------+
                                      |
                                      v
                        +-----------------------------+
                        |  docs / README / IAM example|
                        |  remove EFS references      |
                        +-----------------------------+
```

### Sequence Diagram
```
Post-deployment (after this change):

  operator -> post-deployment-setup.sh : run
  post-deployment-setup.sh -> terraform-outputs.json : read documentdb_cluster_endpoint
  alt endpoint present
    post-deployment-setup.sh -> run-documentdb-init.sh : init indexes + scopes
    run-documentdb-init.sh -> DocumentDB : write scopes
  else endpoint absent
    post-deployment-setup.sh -> operator : FAIL "DocumentDB endpoint required"
  end

  (No EFS read, no scopes-init ECS task, no mcp_gateway_efs_id output read.)

Runtime (after this change):

  ECS auth-server task starts
    -> reads SCOPES_CONFIG_PATH = image-baked /app/scopes.yml  (no EFS mount)
    -> writes logs to CloudWatch (no /app/logs EFS volume)

  ECS mcpgw task starts
    -> /app/data is ephemeral Fargate task storage  (no EFS mount)
```

### Component Diagram
```
[auth-server container]  --scopes-->  [/app/scopes.yml  (image-baked)]
                          --logs---->  [CloudWatch Logs]
[mcpgw container]        --data----->  [ephemeral Fargate task storage]
[registry container]    --persist-->  [DocumentDB]   (unchanged)
```

## Data Models

### New Models
Not applicable. This change removes infrastructure resources and repoints environment variables; it does not introduce new Pydantic models, dataclasses, or schemas.

### Model Changes
Not applicable. No application data models change. The only value change is the `SCOPES_CONFIG_PATH` environment variable string in the auth-server ECS task definition, which moves from an EFS path to an image-baked path.

## API / CLI Design

### New Endpoints / Commands
Not applicable. This change does not add or modify any HTTP endpoint or CLI command. It removes one shell script (`run-scopes-init-task.sh`) and simplifies the behavior of an existing script (`post-deployment-setup.sh`).

### Modified Command Behavior
**Description:** `post-deployment-setup.sh` step 6 (Initialize MCP Scopes) currently branches between DocumentDB and EFS. After this change it uses DocumentDB only.

**Invocation (unchanged):**
```bash
./scripts/post-deployment-setup.sh
```

**Expected behavior after change:**
- Reads `documentdb_cluster_endpoint` from `terraform-outputs.json`.
- If present, runs `run-documentdb-init.sh`.
- If absent, logs an error and fails step 6 (no EFS fallback).

**Error Cases:**
- If `documentdb_cluster_endpoint` is missing from the outputs: step 6 fails with a clear message directing the operator to deploy with DocumentDB enabled. Previously this would have silently fallen back to EFS.

### Removed Command
`run-scopes-init-task.sh` is deleted. Any operator runbook or CI step that invoked it must be updated to use `run-documentdb-init.sh` instead.

## Configuration Parameters

### New Environment Variables
None.

### Removed Configuration Parameters

| Variable Name | Type | Default | Was Used By | Disposition |
|---------------|------|---------|-------------|-------------|
| `efs_throughput_mode` (module var) | string | `bursting` | `module.efs` in `storage.tf` | Removed from `variables.tf`. Root never passed it. |
| `efs_provisioned_throughput` (module var) | number | `100` | `module.efs` in `storage.tf` | Removed from `variables.tf`. Root never passed it. |
| `SCOPES_CONFIG_PATH` (auth-server env) | string | `/efs/auth_config/auth_config/scopes.yml` | auth-server container | Value changes to image-baked path (e.g., `/app/scopes.yml`). The variable itself stays. |

### Settings / Config Class Updates
None. There is no Pydantic settings class for Terraform variables. The auth-server's Python config may read `SCOPES_CONFIG_PATH` from the environment; that code path is unchanged in behavior (it reads a YAML file from the given path), only the path value changes.

### Deployment Surface Checklist
- [x] `terraform/aws-ecs/modules/mcp-gateway/variables.tf` - remove `efs_throughput_mode`, `efs_provisioned_throughput`.
- [x] `terraform/aws-ecs/modules/mcp-gateway/storage.tf` - remove `module "efs"` and the egress rule.
- [x] `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` - remove EFS volumes from auth-server and mcpgw; repoint `SCOPES_CONFIG_PATH`.
- [x] `terraform/aws-ecs/modules/mcp-gateway/outputs.tf` - remove `efs_id`, `efs_arn`, `efs_access_points`.
- [x] `terraform/aws-ecs/outputs.tf` - remove `mcp_gateway_efs_id`, `mcp_gateway_efs_arn`, `mcp_gateway_efs_access_points`.
- [x] `terraform/aws-ecs/scripts/post-deployment-setup.sh` - remove EFS branch and required-output entry.
- [x] `terraform/aws-ecs/scripts/run-scopes-init-task.sh` - delete.
- [x] `docker/Dockerfile.scopes-init` - delete; copy `scopes.yml` into the auth-server image instead.
- [x] `docker/Dockerfile.registry` / `docker/Dockerfile.scopes-init` - verify no other EFS reference remains (the earlier grep flagged `Dockerfile.registry` for an "efs" substring; verify it is a false positive such as "preferences").
- [x] `README.md`, `terraform/README.md`, `terraform/aws-ecs/README.md`, `docs/deployment-modes.md`, `docs/architecture-diagrams.md` - remove EFS references.
- [x] `.env.example` - no EFS references (the file is large but grep found no real EFS env var); verify during implementation.

## New Dependencies

This change uses only existing dependencies. It removes a Terraform module dependency (`terraform-aws-modules/efs/aws`) from the module's effective dependency graph (the module is no longer referenced, so `terraform init` will no longer need to fetch it), and it removes a Docker image (`Dockerfile.scopes-init`). No new packages, providers, or images are introduced.

## Implementation Details

### Step-by-Step Plan (for a future implementer)

#### Step 1: Delete the EFS storage module
**File:** `terraform/aws-ecs/modules/mcp-gateway/storage.tf`
**Lines:** entire file (1-182)

Delete the whole file, or replace its contents with a short comment noting EFS was removed. The file currently defines `module "efs"` (lines 4-163) and `resource "aws_vpc_security_group_egress_rule" "efs_all_outbound"` (lines 169-181). Both must go. Do not delete `data.tf`; `data.aws_vpc.vpc` is still used by `ecs-services.tf`.

```hcl
# EFS storage removed.
# The MCP Gateway Registry now uses ephemeral ECS task storage plus Amazon DocumentDB
# for persistence. See ecs-services.tf for the volume/mount configuration per service.
```

#### Step 2: Remove EFS volumes from the auth-server service
**File:** `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf`
**Lines:** 482-493 (mountPoints) and 542-557 (volume block), plus 221 (SCOPES_CONFIG_PATH)

Replace the `mountPoints` list (lines 482-493) with an empty list, mirroring the registry service:

```hcl
      # EFS volumes removed - auth-server now uses ephemeral storage and DocumentDB
      # Logs go to CloudWatch only; scopes are read from an image-baked path.
      mountPoints = []
```

Replace the `volume` block (lines 542-557) with an empty volume map:

```hcl
  # EFS volumes removed - auth-server uses ephemeral storage and DocumentDB for persistence
  volume = {}
```

Repoint `SCOPES_CONFIG_PATH` (line 221) from `/efs/auth_config/auth_config/scopes.yml` to the image-baked path:

```hcl
        {
          name  = "SCOPES_CONFIG_PATH"
          value = "/app/scopes.yml"
        },
```

#### Step 3: Bake scopes.yml into the auth-server image
**File:** the auth-server Dockerfile (e.g., `docker/Dockerfile.auth-server` or wherever the auth-server image is built; the implementer should locate it and confirm `auth_server/scopes.yml` is not already copied).

Add a copy step so the scopes file is present at `/app/scopes.yml` inside the image:

```dockerfile
COPY auth_server/scopes.yml /app/scopes.yml
```

This makes `SCOPES_CONFIG_PATH = "/app/scopes.yml"` resolve without a runtime EFS mount. If the auth-server image already copies `scopes.yml` to some path, point `SCOPES_CONFIG_PATH` at that existing path instead of adding a new copy.

#### Step 4: Remove EFS volume from the mcpgw service
**File:** `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf`
**Lines:** 1803-1809 (mountPoints) and 1859-1867 (volume block)

Replace the `mountPoints` list (lines 1803-1809) with an empty list:

```hcl
      # EFS volumes removed - mcpgw uses ephemeral task storage for /app/data
      mountPoints = []
```

Replace the `volume` block (lines 1859-1867) with an empty volume map:

```hcl
  # EFS volumes removed - mcpgw uses ephemeral task storage for /app/data
  volume = {}
```

Note: if the mcpgw service owners confirm `/app/data` must be durable, do NOT apply this step as-is; instead wire an S3-backed or DocumentDB-backed store for that data (tracked separately). See Open Questions.

#### Step 5: Remove the module EFS outputs
**File:** `terraform/aws-ecs/modules/mcp-gateway/outputs.tf`
**Lines:** 47-69

Delete the `efs_id`, `efs_arn`, and `efs_access_points` output blocks.

#### Step 6: Remove the module EFS variables
**File:** `terraform/aws-ecs/modules/mcp-gateway/variables.tf`
**Lines:** 259-274

Delete the `efs_throughput_mode` and `efs_provisioned_throughput` variable blocks (including the validation block on `efs_throughput_mode`).

#### Step 7: Remove the root EFS outputs
**File:** `terraform/aws-ecs/outputs.tf`
**Lines:** 67-81

Delete the `mcp_gateway_efs_id`, `mcp_gateway_efs_arn`, and `mcp_gateway_efs_access_points` output blocks.

#### Step 8: Simplify the post-deployment scopes step
**File:** `terraform/aws-ecs/scripts/post-deployment-setup.sh`
**Lines:** 12 (comment), 218 (required output), 523-565 (the branch)

- Line 12: change `# 6. Initializes MCP scopes on EFS` to `# 6. Initializes MCP scopes on DocumentDB`.
- Line 218: remove the `"mcp_gateway_efs_id"` entry from the `required_outputs` array.
- Lines 523-565: replace the `if ... else` with a DocumentDB-only flow that fails fast when the endpoint is absent:

```bash
    # DocumentDB is the only supported scopes backend
    local documentdb_endpoint
    documentdb_endpoint=$(jq -r '.documentdb_cluster_endpoint.value // empty' "$OUTPUTS_FILE" 2>/dev/null)

    if [[ -z "$documentdb_endpoint" || "$documentdb_endpoint" == "null" ]]; then
        log_error "DocumentDB endpoint not found in terraform outputs."
        log_info "EFS scopes initialization has been removed; DocumentDB is now required."
        log_info "Re-run with a deployment that enables DocumentDB, or run run-documentdb-init.sh manually."
        STEPS_FAILED=$((STEPS_FAILED + 1))
        return 1
    fi

    log_info "Detected DocumentDB storage backend"
    log_info "DocumentDB endpoint: $documentdb_endpoint"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would run: $SCRIPT_DIR/run-documentdb-init.sh"
        STEPS_SKIPPED=$((STEPS_SKIPPED + 1))
        return 0
    fi

    log_info "Running DocumentDB initialization (indexes + scopes)..."
    if "$SCRIPT_DIR/run-documentdb-init.sh"; then
        log_success "DocumentDB initialized with indexes and scopes!"
        STEPS_PASSED=$((STEPS_PASSED + 1))
    else
        log_error "DocumentDB initialization failed."
        STEPS_FAILED=$((STEPS_FAILED + 1))
        return 1
    fi
```

#### Step 9: Delete the EFS scopes-init task runner and image
**Files:** `terraform/aws-ecs/scripts/run-scopes-init-task.sh` and `docker/Dockerfile.scopes-init`

Delete both files. Also delete `terraform/aws-ecs/scripts/build-and-push-scopes-init.sh` if it exists (it is referenced by `run-scopes-init-task.sh` line 54 but was not present in the scripts directory at this tag; check and remove if found anywhere under `terraform/aws-ecs/scripts/` or `docker/`).

#### Step 10: Update documentation
- `README.md` line 817: remove the "EFS Shared Storage" bullet, or replace it with a description of the ephemeral-plus-DocumentDB storage model.
- `terraform/README.md` line 16: replace "Amazon EFS for persistent storage" with "Amazon DocumentDB for persistence (ephemeral ECS task storage for transient data)".
- `terraform/aws-ecs/README.md` line 1056: remove the `"elasticfilesystem:*",` line from the example IAM policy JSON.
- `docs/deployment-modes.md` line 211: change "the MCP scopes haven't been initialized on EFS" to "the MCP scopes haven't been initialized on DocumentDB".
- `docs/architecture-diagrams.md` line 519: change "Encryption at rest: KMS (DocumentDB, EBS, EFS, S3)" to "Encryption at rest: KMS (DocumentDB, EBS, S3)".

#### Step 11: Verify no dangling references
Run a repo-wide grep to confirm no EFS or `elasticfilesystem` reference remains under `terraform/`, `docs/`, `README.md`, `CLAUDE.md`, or `docker/` (excluding historical release notes and false-positive substrings like "preferences" or "efSearch"). See the testing plan for the exact commands.

### Error Handling
- Terraform: removing resources that still exist in state will cause `terraform plan` to show them as destroyed. The implementer should review the plan before applying to confirm only EFS resources are destroyed. If any non-EFS resource appears in the destroy list, a dangling reference remains and the plan must be re-inspected.
- Post-deploy script: the new fail-fast branch returns a non-zero status from `_initialize_scopes` when DocumentDB is absent, which propagates to the script's exit code. This is intentional and surfaces misconfiguration immediately rather than silently falling back to a removed backend.

### Logging
- `post-deployment-setup.sh`: keep the existing `log_info` / `log_success` / `log_error` conventions. The new fail-fast branch logs an error explaining that EFS scopes initialization has been removed and DocumentDB is required, with an actionable hint. No new logging framework is introduced.
- No application log changes; the auth-server and mcpgw containers continue to log to CloudWatch as before.

## Observability

### Tracing / Metrics / Logging Points
- No new metrics or tracing spans. The change removes infrastructure, so the relevant observability signal is the absence of EFS resources in the Terraform plan and the absence of EFS-related log lines in `post-deployment-setup.sh` output.
- Operators can confirm the removal by checking the CloudWatch log group for the post-deploy step: after the change, step 6 logs "Detected DocumentDB storage backend" and never logs "Using EFS storage backend".
- Cost observability: the EFS file system and mount targets will disappear from the AWS billing console after the destroy is applied; this is the primary business signal that the removal succeeded.

## Scaling Considerations
- Removing EFS removes a shared storage bottleneck. The auth-server and mcpgw services were previously coupled to a single EFS file system and its throughput mode (`bursting` by default). After the change, each task uses independent ephemeral Fargate storage, which scales horizontally with the task count without EFS throughput contention.
- DocumentDB remains the shared persistence layer and is unchanged; its scaling characteristics are out of scope for this issue.
- No caching strategy changes. The only transient-data concern is mcpgw `/app/data`; if that path holds regenerable data, ephemeral storage is fine; if it holds durable state, it must be migrated (see Open Questions).

## File Changes

### New Files
None. This change only deletes and modifies existing files.

### Modified Files

| File Path | Lines | Change Description |
|-----------|-------|--------------------|
| `terraform/aws-ecs/modules/mcp-gateway/storage.tf` | 1-182 | Delete `module "efs"` and the `efs_all_outbound` egress rule; leave a short comment. |
| `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` | 221, 482-493, 542-557, 1803-1809, 1859-1867 | Repoint `SCOPES_CONFIG_PATH` to `/app/scopes.yml`; set auth-server and mcpgw `mountPoints = []` and `volume = {}`. |
| `terraform/aws-ecs/modules/mcp-gateway/outputs.tf` | 47-69 | Remove `efs_id`, `efs_arn`, `efs_access_points`. |
| `terraform/aws-ecs/modules/mcp-gateway/variables.tf` | 259-274 | Remove `efs_throughput_mode`, `efs_provisioned_throughput`. |
| `terraform/aws-ecs/outputs.tf` | 67-81 | Remove `mcp_gateway_efs_id`, `mcp_gateway_efs_arn`, `mcp_gateway_efs_access_points`. |
| `terraform/aws-ecs/scripts/post-deployment-setup.sh` | 12, 218, 523-565 | Update comment; drop `mcp_gateway_efs_id` from required outputs; replace EFS branch with DocumentDB-only fail-fast. |
| `docker/Dockerfile.scopes-init` (or auth-server Dockerfile) | n/a | Delete scopes-init image; add `COPY auth_server/scopes.yml /app/scopes.yml` to the auth-server image. |
| `README.md` | 817 | Remove/rewrite the EFS Shared Storage bullet. |
| `terraform/README.md` | 16 | Replace EFS line with DocumentDB/ephemeral description. |
| `terraform/aws-ecs/README.md` | 1056 | Remove `elasticfilesystem:*` from the example IAM policy. |
| `docs/deployment-modes.md` | 211 | Replace "on EFS" with "on DocumentDB". |
| `docs/architecture-diagrams.md` | 519 | Remove EFS from the KMS encryption-at-rest list. |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| `terraform/aws-ecs/scripts/run-scopes-init-task.sh` | Entirely EFS-based; replaced by `run-documentdb-init.sh`. |
| `docker/Dockerfile.scopes-init` | Builds the EFS scopes-init image; no longer needed. |
| `terraform/aws-ecs/scripts/build-and-push-scopes-init.sh` (if present) | Referenced only by the deleted task runner. |

### Estimated Lines of Code

| Category | Lines |
|----------|-------|
| New code (Dockerfile COPY, comments, fail-fast branch) | ~25 |
| Deleted Terraform / script / Dockerfile code | ~260 |
| Modified code (volume/mount/env rewrites, doc edits) | ~30 |
| New tests (grep-based + syntax checks, see testing.md) | ~40 |
| **Total touched** | **~355** |

## Testing Strategy
The full testing plan lives in `./testing.md`. It covers: a grep-based absence check for EFS references, `terraform validate` / `terraform plan` assertions that EFS resources are destroyed, shell syntax checks for the modified post-deploy script, backwards-compatibility checks for the `SCOPES_CONFIG_PATH` value, and a deployment-surface walkthrough for the IAM policy and docs.

## Alternatives Considered

### Alternative 1: Keep EFS but stop mounting it
**Description:** Remove the ECS volume mounts but leave the `module "efs"` in place so existing state is not destroyed.
**Pros / Cons:** Pro: zero destroy risk, fully reversible. Con: continues to provision and pay for EFS, access points, mount targets, and the NFS security group on every apply; leaves the dead code and misleading docs in place; does not satisfy the acceptance criteria.
**Why Rejected:** The issue's goal is to finish the migration, not to half-finish it again. Leaving the module in place recreates exactly the half-migrated state being fixed.

### Alternative 2: Replace EFS with an S3-backed mount (Mountpoint for S3)
**Description:** Mount an S3 bucket via Mountpoint for S3 into auth-server and mcpgw instead of EFS.
**Pros / Cons:** Pro: keeps a shared filesystem semantic for mcpgw `/app/data` if durable. Con: introduces a new dependency (Mountpoint CSI/driver or a sidecar), new IAM permissions, and a new storage backend to operate, for a benefit only mcpgw might need; auth-server scopes are better served by an image-baked file; contradicts the simplicity-first principle in CLAUDE.md.
**Why Rejected:** Over-engineered for the auth-server case (a single config file) and premature for mcpgw until the service owners confirm `/app/data` needs durability. If mcpgw does need durable shared storage, that is a separate, scoped decision.

### Alternative 3: Load scopes from DocumentDB at runtime instead of image-baking
**Description:** Have the auth-server read scopes from DocumentDB (like the registry does) rather than from an image-baked YAML file.
**Pros / Cons:** Pro: single source of truth for scopes across services. Con: requires changes to the auth-server Python scopes loader (out of scope for this issue), couples auth-server startup to DocumentDB availability for a config it currently reads from a file, and is a larger change than the migration goal requires.
**Why Rejected:** Exceeds the issue scope. Image-baking the scopes file is the minimal change that removes the EFS dependency while preserving the auth-server's existing file-based scopes loader behavior.

### Comparison Matrix

| Criteria | Chosen (ephemeral + image-baked scopes) | Alt 1 (keep EFS module) | Alt 2 (S3 Mountpoint) | Alt 3 (scopes from DocumentDB) |
|----------|-----------------------------------------|--------------------------|------------------------|--------------------------------|
| Complexity | Low | Lowest | High | Medium |
| Removes EFS cost | Yes | No | Yes | Yes |
| New dependencies | None | None | Mountpoint driver + IAM | None (uses existing DocumentDB) |
| Scope creep | None | None | Some (mcpgw durable data) | High (auth-server loader rewrite) |
| Reversibility | Medium (re-add module) | High | Medium | Low |

## Rollout Plan
- Phase 1: Implementation (out of scope for this skill) - apply Steps 1-11 above.
- Phase 2: Testing - run the `testing.md` plan; confirm `terraform plan` destroys only EFS resources; confirm `post-deployment-setup.sh --dry-run` logs the DocumentDB path and never the EFS path.
- Phase 3: Deployment - apply in a staging AWS account first; confirm the EFS file system, access points, mount targets, and NFS security group are destroyed; confirm auth-server loads scopes from the image-baked path; confirm mcpgw `/app/data` behavior is acceptable (transient); promote to production after the mcpgw durability question is resolved.

## Open Questions
- **mcpgw `/app/data` durability**: Does mcpgw store durable state at `/app/data`? If yes, removing the EFS mount will lose that state on task restart, and Step 4 must be replaced with a durable backend (S3 or DocumentDB) before applying. The mcpgw service owners must confirm. This is the single blocker for a safe production apply.
- **auth-server Dockerfile location**: Which Dockerfile builds the auth-server image, and does it already copy `scopes.yml`? The implementer must locate it (likely under `docker/`) and confirm before adding the `COPY` in Step 3.
- **Out-of-band IAM**: If the deployment relies on a customer-managed IAM policy applied outside this repo that grants `elasticfilesystem:*`, removing the README reference does not revoke the permission. Confirm whether any out-of-band policy needs updating.

## References
- Existing EFS removal precedent in the registry service: `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` lines 1367-1369 and 1419-1420.
- DocumentDB scopes init path: `terraform/aws-ecs/scripts/run-documentdb-init.sh`.
- Scopes source file: `auth_server/scopes.yml`.
- CLAUDE.md guidelines: simplicity-first, modern tooling, no emojis in docs, "Amazon Bedrock" naming (not applicable here but followed).
