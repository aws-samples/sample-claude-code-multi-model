# Testing Plan: Remove Amazon EFS from the Terraform AWS ECS deployment

*Created: 2026-07-15*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

## Overview

### Scope of Testing
This plan verifies that every Amazon EFS resource, variable, output, script, and documentation reference has been removed from the `terraform/aws-ecs` stack and that the two formerly-EFS-backed ECS services (auth-server and mcpgw) still start and function using ephemeral storage plus an image-baked scopes file. It also verifies the post-deployment script now uses DocumentDB as the only scopes backend.

### Prerequisites
- [ ] Local checkout of the target repo at tag `1.24.4` (the cloned `repo/`).
- [ ] `terraform` CLI installed (any version compatible with the existing `.terraform.lock.hcl`).
- [ ] `jq`, `grep`, `bash -n` available locally.
- [ ] For deployment-surface tests: an AWS staging account with the existing stack deployed, plus `aws` CLI credentials.
- [ ] DocumentDB enabled in the target deployment (the new scopes path requires it).

### Shared Variables
```bash
# Paths (run from the repo root of the cloned target repo)
export TF_DIR="$PWD/terraform/aws-ecs"
export SCRIPTS_DIR="$TF_DIR/scripts"

# For deployment-surface tests against a staging AWS account
export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_PROFILE="${AWS_PROFILE:-default}"
```

## 1. Functional Tests

### 1.1 curl / HTTP Tests
**Not Applicable** - This change does not add or modify any HTTP endpoint. It removes Terraform resources and shell scripts. The auth-server and mcpgw HTTP surfaces are unchanged in contract; their existing endpoints continue to behave as before. (A smoke check that auth-server and mcpgw health endpoints respond after redeploy is covered in Section 5.)

### 1.2 CLI / Script Tests

#### 1.2.1 Deleted script is gone
```bash
# Expected: file does not exist
test ! -f "$SCRIPTS_DIR/run-scopes-init-task.sh" && echo "PASS: run-scopes-init-task.sh removed" || echo "FAIL: run-scopes-init-task.sh still present"
```
Expected output: `PASS: run-scopes-init-task.sh removed`.

#### 1.2.2 Deleted Dockerfile is gone
```bash
test ! -f "$PWD/docker/Dockerfile.scopes-init" && echo "PASS: Dockerfile.scopes-init removed" || echo "FAIL: Dockerfile.scopes-init still present"
test ! -f "$SCRIPTS_DIR/build-and-push-scopes-init.sh" && echo "PASS: build-and-push-scopes-init.sh removed" || echo "FAIL: build-and-push-scopes-init.sh still present"
```
Expected: both PASS.

#### 1.2.3 post-deployment-setup.sh syntax is valid
```bash
bash -n "$SCRIPTS_DIR/post-deployment-setup.sh" && echo "PASS: syntax ok" || echo "FAIL: syntax error"
```
Expected: `PASS: syntax ok`.

#### 1.2.4 post-deployment-setup.sh DRY_RUN uses DocumentDB, never EFS
```bash
# Build a fake outputs file WITH a DocumentDB endpoint
cat > /tmp/outputs-with-docdb.json <<'EOF'
{"documentdb_cluster_endpoint":{"value":"docdb-cluster.example.cluster"},
 "vpc_id":{"value":"vpc-1"},"ecs_cluster_name":{"value":"mcpgw-v2"},
 "ecs_cluster_arn":{"value":"arn:aws:ecs:us-west-2:1:cluster/mcpgw-v2"},
 "mcp_gateway_url":{"value":"http://x"},"mcp_gateway_auth_url":{"value":"http://x"},
 "keycloak_url":{"value":"http://x"}}
EOF

# Run step 6 in dry-run; expect DocumentDB path, no EFS string
OUTPUTS_FILE=/tmp/outputs-with-docdb.json bash -c '
  set -e
  # Source only the scopes function via a targeted dry-run of the whole script with --dry-run --skip-keycloak --skip-everything-else
  # (Adjust flags to match the script'\''s actual options; the assertion is on logged strings.)
' 2>&1 | tee /tmp/postdeploy-dryrun.log

grep -q "Detected DocumentDB storage backend" /tmp/postdeploy-dryrun.log && echo "PASS: DocumentDB path taken" || echo "FAIL: DocumentDB path not taken"
grep -q "Using EFS storage backend" /tmp/postdeploy-dryrun.log && echo "FAIL: EFS path still present" || echo "PASS: no EFS fallback"
```
Expected: `PASS: DocumentDB path taken` and `PASS: no EFS fallback`.

#### 1.2.5 post-deployment-setup.sh fails fast without DocumentDB (negative case)
```bash
# Outputs file WITHOUT a documentdb_cluster_endpoint and WITHOUT mcp_gateway_efs_id
cat > /tmp/outputs-no-docdb.json <<'EOF'
{"vpc_id":{"value":"vpc-1"},"ecs_cluster_name":{"value":"mcpgw-v2"},
 "ecs_cluster_arn":{"value":"arn:aws:ecs:us-west-2:1:cluster/mcpgw-v2"},
 "mcp_gateway_url":{"value":"http://x"},"mcp_gateway_auth_url":{"value":"http://x"},
 "keycloak_url":{"value":"http://x"}}
EOF

# Step 6 should now fail fast (non-zero) and NOT mention EFS as a fallback option that was tried.
# Run the full script in dry-run; expect a non-zero exit and an error mentioning DocumentDB is required.
OUTPUTS_FILE=/tmp/outputs-no-docdb.json bash "$SCRIPTS_DIR/post-deployment-setup.sh" --dry-run 2>&1 | tee /tmp/postdeploy-nodocdb.log; echo "exit=$?"

grep -qi "DocumentDB.*required\|DocumentDB endpoint not found" /tmp/postdeploy-nodocdb.log && echo "PASS: fail-fast on missing DocumentDB" || echo "FAIL: did not fail fast on missing DocumentDB"
grep -qi "Using EFS storage backend\|run-scopes-init-task" /tmp/postdeploy-nodocdb.log && echo "FAIL: EFS fallback attempted" || echo "PASS: no EFS fallback attempted"
```
Expected: a non-zero exit code, `PASS: fail-fast on missing DocumentDB`, `PASS: no EFS fallback attempted`.
Note: this test also confirms the behavior change flagged in the review: `--dry-run` now exits non-zero when DocumentDB is absent. Any CI that expects dry-run to always succeed must be updated.

## 2. Backwards Compatibility Tests

### 2.1 SCOPES_CONFIG_PATH still resolves to a readable scopes file
The auth-server previously read `SCOPES_CONFIG_PATH = "/efs/auth_config/auth_config/scopes.yml"`. After the change it reads an image-baked path (e.g., `/app/scopes.yml`). The variable name is unchanged; only the value changes.

```bash
# Confirm the env var still exists in the auth-server task definition and points at a non-EFS path
grep -n "SCOPES_CONFIG_PATH" "$TF_DIR/modules/mcp-gateway/ecs-services.tf"
# Expected: value is a path that does NOT start with /efs
grep -q 'SCOPES_CONFIG_PATH' "$TF_DIR/modules/mcp-gateway/ecs-services.tf" && echo "PASS: var present"
VALUE=$(grep -A1 'SCOPES_CONFIG_PATH' "$TF_DIR/modules/mcp-gateway/ecs-services.tf" | grep -o '"/[^"]*"' | tr -d '"')
case "$VALUE" in
  /efs/*) echo "FAIL: still points at EFS ($VALUE)" ;;
  "")     echo "FAIL: could not parse value" ;;
  *)      echo "PASS: points at non-EFS path ($VALUE)" ;;
esac
```
Expected: `PASS: var present` and `PASS: points at non-EFS path (/app/scopes.yml)` (or whatever image-baked path was chosen).

### 2.2 scopes.yml is present at the image-baked path
```bash
# Confirm the auth-server Dockerfile copies scopes.yml to the configured path.
# Locate the auth-server Dockerfile (likely under docker/).
AUTH_DOCKERFILE=$(grep -rl "auth_server" "$PWD/docker/" --include="Dockerfile*" | head -1)
echo "Auth Dockerfile: $AUTH_DOCKERFILE"
grep -n "scopes.yml" "$AUTH_DOCKERFILE"
# Expected: a COPY line placing scopes.yml at the path used by SCOPES_CONFIG_PATH
```
Expected: a `COPY auth_server/scopes.yml <path>` line where `<path>` matches the `SCOPES_CONFIG_PATH` value.

### 2.3 Pre-change Terraform output consumers no longer break
The root outputs `mcp_gateway_efs_id`, `mcp_gateway_efs_arn`, `mcp_gateway_efs_access_points` are removed. Verify nothing else in the repo reads them:

```bash
# Expected: no matches outside this skill's own artifacts and historical release notes
grep -rn "mcp_gateway_efs_id\|mcp_gateway_efs_arn\|mcp_gateway_efs_access_points" \
  --include='*.sh' --include='*.tf' --include='*.md' --include='*.yml' --include='*.yaml' \
  "$PWD/terraform" "$PWD/scripts" "$PWD/docs" "$PWD/README.md" 2>/dev/null
```
Expected: no output (all matches removed). If `post-deployment-setup.sh` line 218 was the only consumer, this confirms it was cleaned up.

### 2.4 Terraform variables removed have no remaining consumers
```bash
grep -rn "efs_throughput_mode\|efs_provisioned_throughput" --include='*.tf' "$PWD/terraform"
```
Expected: no output.

### 2.5 data.aws_vpc.vpc is not orphaned
```bash
# The data source is defined in data.tf and must still be referenced by ecs-services.tf
grep -rn "data.aws_vpc.vpc" --include='*.tf' "$PWD/terraform/aws-ecs/modules/mcp-gateway/"
```
Expected: at least one reference in `data.tf` (definition) and one in `ecs-services.tf` (consumer). Confirms removing `storage.tf` did not orphan the data source.

## 3. UX Tests

### 3.1 Documentation no longer advertises EFS
Manual review of the edited doc lines. Each line below should read as described, not as the old EFS text.

| File | Old text | Expected new text |
|------|----------|-------------------|
| `README.md:817` | `EFS Shared Storage - Persistent storage for models, logs, and configuration` | Removed, or replaced with ephemeral-plus-DocumentDB description |
| `terraform/README.md:16` | `Amazon EFS for persistent storage` | `Amazon DocumentDB for persistence (ephemeral ECS task storage for transient data)` |
| `terraform/aws-ecs/README.md:1056` | `"elasticfilesystem:*",` | Line removed from the example IAM policy |
| `docs/deployment-modes.md:211` | `the MCP scopes haven't been initialized on EFS` | `the MCP scopes haven't been initialized on DocumentDB` |
| `docs/architecture-diagrams.md:519` | `Encryption at rest: KMS (DocumentDB, EBS, EFS, S3)` | `Encryption at rest: KMS (DocumentDB, EBS, S3)` |

### 3.2 CLI output clarity
Run `post-deployment-setup.sh --help` (or the script's usage) and confirm step 6 is described as "Initializing MCP Scopes" on DocumentDB, with no mention of EFS.

```bash
bash "$SCRIPTS_DIR/post-deployment-setup.sh" --help 2>&1 | grep -i "scopes"
# Expected: a line referencing scopes initialization on DocumentDB; no EFS mention
bash "$SCRIPTS_DIR/post-deployment-setup.sh" --help 2>&1 | grep -i "efs" && echo "FAIL: EFS in help" || echo "PASS: no EFS in help"
```
Expected: `PASS: no EFS in help`.

## 4. Deployment Surface Tests

### 4.1 Docker wiring
**Not Applicable** - The change deletes `docker/Dockerfile.scopes-init` and adds one `COPY` line to the auth-server Dockerfile. No new Docker Compose env var or volume is introduced. The Docker Compose files (`docker-compose.yml`, etc.) do not reference EFS (verified: their "efs" grep matches were false-positive substrings).

### 4.2 Terraform / ECS wiring

#### 4.2.1 terraform validate
```bash
cd "$TF_DIR"
terraform init -input=false
terraform validate
```
Expected: `Success! The configuration is valid.`

#### 4.2.2 terraform init removes the EFS module from the lock file
```bash
cd "$TF_DIR"
# After init, the EFS module should no longer be a recorded dependency
grep -i "terraform-aws-modules/efs/aws" .terraform.lock.hcl && echo "FAIL: EFS module still in lock file" || echo "PASS: EFS module removed from lock file"
```
Expected: `PASS: EFS module removed from lock file`. If it still appears, run `terraform init -upgrade` and re-check (this addresses the review note about lock-file hygiene).

#### 4.2.3 terraform plan destroys only EFS resources
```bash
cd "$TF_DIR"
terraform plan -destroy-target=module.mcp_gateway.module.efs -out=/dev/null 2>&1 | tee /tmp/plan-destroy.log || true
# Better: run a full plan and inspect the destroy list
terraform plan -out=/tmp/efs-removal.tfplan 2>&1 | tee /tmp/plan-full.log
# Confirm EFS resources are in the destroy list
grep -E "module\.mcp_gateway\.module\.efs" /tmp/plan-full.log | head
# Confirm no non-EFS resource is unexpectedly destroyed
grep -E "will be destroyed" /tmp/plan-full.log | grep -vi "efs\|elasticfilesystem" && echo "WARN: non-EFS destroy detected - inspect" || echo "PASS: only EFS resources destroyed"
```
Expected: the plan shows `aws_efs_file_system`, `aws_efs_access_point` (x6), `aws_efs_mount_target` (per subnet), and the `aws_vpc_security_group_egress_rule` for EFS being destroyed; and `PASS: only EFS resources destroyed`.

#### 4.2.4 No EFS resources remain in the plan to create
```bash
grep -E "will be created" /tmp/plan-full.log | grep -i "efs\|elasticfilesystem" && echo "FAIL: EFS resource still being created" || echo "PASS: no EFS resources created"
```
Expected: `PASS: no EFS resources created`.

### 4.3 Helm / EKS wiring
**Not Applicable** - This change is scoped to the Terraform AWS ECS surface. The Helm charts under `charts/` are a separate deployment surface and are not touched by this issue. (If a future issue removes EFS from the Helm charts, that will carry its own helm-unittest suite changes per the CLAUDE.md Helm guidance.)

### 4.4 Deploy and verify (staging AWS account)
```bash
cd "$TF_DIR"
terraform apply /tmp/efs-removal.tfplan

# Confirm the EFS file system is gone
aws efs describe-file-systems --region "$AWS_REGION" --query 'FileSystems[?contains(Name,`efs`)].[Name,FileSystemId]' --output table || true
# Expected: no EFS file systems matching the deployment name prefix

# Confirm the auth-server task definition no longer has an efsVolumeConfiguration
aws ecs describe-task-definition --region "$AWS_REGION" \
  --task-definition <auth-server-task-def> \
  --query 'taskDefinition.volumes[].efsVolumeConfiguration' --output json
# Expected: [] (empty) or null

# Confirm the mcpgw task definition no longer has an efsVolumeConfiguration
aws ecs describe-task-definition --region "$AWS_REGION" \
  --task-definition <mcpgw-task-def> \
  --query 'taskDefinition.volumes[].efsVolumeConfiguration' --output json
# Expected: [] (empty) or null
```
Expected: no EFS file systems remain; both task definitions show no `efsVolumeConfiguration`.

#### 4.4.1 Out-of-band IAM check (review hardening)
```bash
# List inline and attached policies on the ECS task roles and confirm none still grant elasticfilesystem:*
for ROLE in <auth-server-task-role> <mcpgw-task-role>; do
  echo "=== $ROLE ==="
  aws iam list-attached-role-policies --role-name "$ROLE" --region "$AWS_REGION" --output text
  aws iam list-role-policies --role-name "$ROLE" --region "$AWS_REGION" --output text
done
# Inspect each policy document for elasticfilesystem; expected: none
```
Expected: no policy document grants `elasticfilesystem:*`. If one does (out-of-band customer-managed policy), file a follow-up to remove it.

### 4.5 Rollback verification
If the apply breaks auth-server (e.g., scopes path wrong), roll back:

```bash
cd "$TF_DIR"
# Revert the Terraform changes and re-apply the previous revision
git revert <merge-commit>   # or check out the prior revision
terraform init -input=false
terraform apply
# Redeploy the previous auth-server task definition so it mounts EFS again
aws ecs describe-task-definition --region "$AWS_REGION" --task-definition <previous-auth-task-def> ...
```
Expected: the rollback re-creates the EFS resources and restores the EFS-backed task definitions. Document the actual rollback time observed.

## 5. End-to-End API Tests

### 5.1 auth-server loads scopes and serves auth after EFS removal
```bash
# After redeploy, the auth-server should start, read /app/scopes.yml, and serve its health endpoint
curl -fsS "http://<auth-server-host>/health" && echo "PASS: auth-server healthy" || echo "FAIL: auth-server unhealthy"
# Confirm scopes are loaded (hit an endpoint that exercises scope checks, per the repo's existing E2E conventions)
```
Expected: `PASS: auth-server healthy` and scope-gated requests behave as before.

### 5.2 mcpgw starts and serves with ephemeral /app/data
```bash
# After redeploy, mcpgw should start and pass its health check (nc -z localhost 8003)
curl -fsS "http://<mcpgw-host>:8003/" 2>/dev/null || true
# Inspect CloudWatch logs for mcpgw to confirm no "EFS mount failed" / "permission denied" errors
aws logs tail "/ecs/<name-prefix>-mcpgw" --region "$AWS_REGION" --since 10m | grep -i "efs\|mount\|permission denied" && echo "FAIL: mount errors in logs" || echo "PASS: no mount errors"
```
Expected: `PASS: no mount errors`. If mcpgw requires durable `/app/data` (the open question in the LLD), this is where a data-loss regression would surface; monitor for missing state across task restarts.

### 5.3 Scopes initialization via DocumentDB succeeds end-to-end
```bash
# Run the post-deploy scopes step (not dry-run) against the staging deployment
OUTPUTS_FILE=$TF_DIR/terraform-outputs.json bash "$SCRIPTS_DIR/post-deployment-setup.sh" 2>&1 | tee /tmp/postdeploy-real.log
grep -q "DocumentDB initialized with indexes and scopes" /tmp/postdeploy-real.log && echo "PASS: scopes initialized on DocumentDB" || echo "FAIL: scopes init failed"
grep -qi "Using EFS storage backend\|run-scopes-init-task" /tmp/postdeploy-real.log && echo "FAIL: EFS path invoked" || echo "PASS: EFS path not invoked"
```
Expected: `PASS: scopes initialized on DocumentDB` and `PASS: EFS path not invoked`.

## 6. Absence-of-EFS Grep Sweep (cross-cutting)

```bash
# From the repo root of the cloned target repo
grep -rn -i -w "efs\|elasticfilesystem" \
  --include='*.tf' --include='*.tfvars' --include='*.sh' --include='*.md' --include='*.yml' --include='*.yaml' --include='Dockerfile*' \
  terraform/ docs/ README.md docker/ scripts/ 2>/dev/null \
  | grep -vi "efsearch\|envfrom\|preferences"
```
Expected: no output (all real EFS references removed). Historical `release-notes/` entries that mention EFS are explicitly out of scope and may still match; exclude `release-notes/` from the sweep if needed:
```bash
grep -rn -i -w "efs\|elasticfilesystem" \
  --include='*.md' release-notes/ 2>/dev/null | head
# These are allowed (immutable history).
```

## 7. Secret Scan of scopes.yml (review hardening)

```bash
# Confirm the image-baked scopes file contains no secrets before baking it into the auth-server image.
# The repo ships a detect-secrets baseline at .secrets.baseline.
detect-secrets scan auth_server/scopes.yml 2>/dev/null | jq '.results | length'
# Expected: 0 (no findings). If >0, do NOT image-bake until the secret is removed.
```
Expected: `0`.

## 8. Test Execution Checklist
- [ ] Section 1 (Functional / script) passes (1.2.1 - 1.2.5)
- [ ] Section 2 (Backwards Compat) passes (2.1 - 2.5)
- [ ] Section 3 (UX / docs) passes (3.1 - 3.2)
- [ ] Section 4 (Deployment / Terraform) passes (4.2.1 - 4.2.4, 4.4, 4.4.1, 4.5)
- [ ] Section 4.1 (Docker) marked Not Applicable
- [ ] Section 4.3 (Helm) marked Not Applicable
- [ ] Section 5 (E2E) passes (5.1 - 5.3)
- [ ] Section 6 (absence grep sweep) returns no real EFS references
- [ ] Section 7 (scopes.yml secret scan) returns 0
- [ ] `terraform validate` and `terraform plan` succeed with only EFS resources destroyed
- [ ] `bash -n post-deployment-setup.sh` passes
- [ ] No regression in existing tests: any test referencing EFS outputs updated; Helm unittest suites unaffected
