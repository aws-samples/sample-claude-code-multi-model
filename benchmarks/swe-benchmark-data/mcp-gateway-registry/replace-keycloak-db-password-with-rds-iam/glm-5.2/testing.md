# Testing Plan: Replace Keycloak DB password with RDS IAM authentication

*Created: 2026-07-15*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

## Overview
### Scope of Testing
Verify that the Terraform/ECS change correctly switches Keycloak's database connection from a static password to RDS IAM auth (via the RDS Proxy + AWS Advanced JDBC Driver wrapper) when `keycloak_db_iam_auth_enabled = true`, that it is a strict no-op when the flag is `false`, and that the IAM-mode path works end-to-end in an applied AWS stack. The docker-compose and Helm surfaces are explicitly out of scope and are not tested here.

### Prerequisites
- [ ] The cloned repo at `benchmarks/swe-benchmark-data/mcp-gateway-registry/repo/` is checked out at tag `1.24.4`.
- [ ] A non-prod AWS account with permission to run `terraform plan/apply` against the `terraform/aws-ecs` stack.
- [ ] `terraform`, `checkov`, `aws` CLI, and `jq` installed locally.
- [ ] An applied baseline stack with `keycloak_db_iam_auth_enabled = false` (the current state) so the no-op plan can be compared.
- [ ] ECS exec enabled on the Keycloak service (for the E2E connectivity check).
- [ ] MongoDB is NOT required for this change (Keycloak uses Aurora MySQL on this surface).

### Shared Variables
```bash
# Repo and Terraform paths
export REPO="/Users/prsinp/claude-code-multi-model/benchmarks/swe-benchmark-data/mcp-gateway-registry/repo"
export TF_DIR="$REPO/terraform/aws-ecs"

# AWS
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Deployment name prefix used by the stack (default mcp-gateway)
export NAME="${NAME:-mcp-gateway}"
```

---

## 1. Functional Tests

### 1.1 Terraform static checks (no apply required)

These run against the Terraform source and do not touch AWS state.

#### 1.1.1 `terraform validate` passes in both modes
```bash
cd "$TF_DIR"
terraform init -backend=false
terraform validate
```
Expected: `Success! The configuration is valid.`

#### 1.1.2 `terraform fmt` is clean
```bash
terraform fmt -check -recursive "$TF_DIR"
```
Expected: exit code 0 (no diff). If it reports files, run `terraform fmt "$TF_DIR"` and re-commit.

#### 1.1.3 `checkov` passes
```bash
checkov -d "$TF_DIR" --framework terraform
```
Expected: no new failed checks versus baseline. The `CKV_AWS_162` skip on the Aurora cluster must still carry a justification comment that mentions the proxy enforces IAM auth when the flag is on.

Assertion (grep the skip is present and justified):
```bash
grep -n "CKV_AWS_162" "$TF_DIR/keycloak-database.tf"
```
Expected: one line whose trailing comment references IAM auth being enforced at the proxy.

### 1.2 `terraform plan` - password mode (flag = false) is a no-op

Goal: prove backwards compatibility. Against an existing baseline stack, the plan must show no changes.

```bash
cd "$TF_DIR"
terraform init
terraform plan -var keycloak_db_iam_auth_enabled=false -out /tmp/pw-mode.tfplan
```
Expected: `No changes. Your infrastructure matches the configuration.`

Negative case (the plan WOULD change if the flag were miswired):
```bash
terraform plan -var keycloak_db_iam_auth_enabled=false -detailed-exitcode
```
Expected: exit code 0 (no changes). Exit code 2 means there are changes, which is a regression for the fallback path.

### 1.3 `terraform plan` - IAM mode (flag = true) shows the expected change set

```bash
cd "$TF_DIR"
terraform plan -var keycloak_db_iam_auth_enabled=true -out /tmp/iam-mode.tfplan
```
Expected plan contains exactly these resource actions (assert each):
- `aws_db_proxy.keycloak` - `update in-place` (`iam_auth` DISABLED -> REQUIRED, `require_tls` -> true)
- `aws_ssm_parameter.keycloak_database_url` - `update in-place` (value -> `jdbc:aws-wrapper:mysql://<proxy-endpoint>:3306/keycloak`)
- `random_password.keycloak_db_master` - `create` (1)
- `aws_rds_cluster.keycloak` - `update in-place` (`master_password` -> new value)
- `aws_secretsmanager_secret_version.keycloak_db_secret` - `update` (`password` -> new value)
- `aws_ecs_task_definition.keycloak` - `update` (env gains `KC_DB_URL_DRIVER` + `KC_DB_URL_PROPERTIES`; secrets lose `KC_DB_USERNAME` + `KC_DB_PASSWORD`)
- `aws_iam_role_policy.keycloak_task_rds_iam_connect` - `create` (1)

Assertion script:
```bash
terraform show -json /tmp/iam-mode.tfplan > /tmp/iam-plan.json
jq -r '.resource_changes[] | "\(.change.actions[]) \(.address)"' /tmp/iam-plan.json | sort
```
Expected: the seven lines above appear (plus any `replace` on the task definition if the env change forces replacement, which is acceptable).

Negative case (the plan must NOT touch the docker-compose or Helm surfaces):
```bash
jq -r '.resource_changes[].address' /tmp/iam-plan.json | grep -Ei 'helm|compose|kubernetes' || echo "OK: no compose/helm resources in plan"
```
Expected: `OK: no compose/helm resources in plan`.

### 1.4 `terraform plan` - password variable required when flag is false

```bash
cd "$TF_DIR"
# Omit keycloak_database_password entirely with the flag off
terraform plan -var keycloak_db_iam_auth_enabled=false -var keycloak_database_password= -out /tmp/missing.tfplan 2>&1 | tee /tmp/missing.out
```
Expected: a validation error from the `validation` block:
```
keycloak_database_password must be set when keycloak_db_iam_auth_enabled is false.
```
(Empty string still fails the `!= null` + non-empty check; tighten the validation to also reject `""` if desired.)

### 1.5 CLI Tests

There is no new application CLI. The only CLI surface is `terraform`.

```bash
# Confirm the new variable is accepted and documented
terraform -chdir="$TF_DIR" console <<< 'var.keycloak_db_iam_auth_enabled'
```
Expected: prints `false` (the default).

---

## 2. Backwards Compatibility Tests

### 2.1 Pre-change request shapes still accepted (Terraform input)
Operators who supply `keycloak_database_password` and do not set the flag must get an unchanged deployment.
```bash
cd "$TF_DIR"
terraform plan -var keycloak_database_password='CHANGE-ME-DB-PASSWORD' -out /tmp/bc.tfplan
```
Expected: `No changes.` (because the flag defaults to false and the password matches the current state). This proves existing tfvars files continue to work without modification.

### 2.2 CLI without the new flag behaves as before
```bash
cd "$TF_DIR"
terraform plan -out /tmp/noflag.tfplan
```
Expected: `No changes.` The flag is optional with a safe default.

### 2.3 Defaults preserve prior behavior
- `keycloak_db_iam_auth_enabled` default `false` -> password mode (today's behavior). Verified by 2.1/2.2.
- `keycloak_database_password` default `null` -> only valid when flag is true; when flag is false the validation block requires it, preserving the prior "required" semantics for the password path.

### 2.4 Rotation Lambda still wired in both modes
```bash
grep -n "aws_secretsmanager_secret_rotation.*keycloak_db_secret" "$TF_DIR/secret-rotation-config.tf"
grep -n "aws_lambda_function.rds_rotation" "$TF_DIR/secret-rotation.tf"
```
Expected: both resources present and ungated (the rotation is not conditioned on the IAM flag). The 30-day schedule remains.

### 2.5 `KC_DB_USERNAME` / `KC_DB_PASSWORD` present only in password mode
```bash
# Render the task definition container definitions for IAM mode and assert absence
cd "$TF_DIR"
terraform plan -var keycloak_db_iam_auth_enabled=true -out /tmp/iam.tfplan
terraform show -json /tmp/iam.tfplan \
  | jq -r '.resource_changes[] | select(.address=="aws_ecs_task_definition.keycloak") | .change.after.container_definitions' \
  | jq -r '.[0].secrets[].name'
```
Expected (IAM mode): `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD`, `KC_DB_URL` only. `KC_DB_USERNAME` and `KC_DB_PASSWORD` must NOT appear.

Repeat for password mode and assert the opposite:
```bash
terraform plan -var keycloak_db_iam_auth_enabled=false -out /tmp/pw.tfplan
terraform show -json /tmp/pw.tfplan \
  | jq -r '.resource_changes[] | select(.address=="aws_ecs_task_definition.keycloak") | .change.after.container_definitions' \
  | jq -r '.[0].secrets[].name' 2>/dev/null || \
  terraform show -json /tmp/pw.tfplan \
  | jq -r '.planned_values.resources[] | select(.address=="aws_ecs_task_definition.keycloak") | .values.container_definitions' \
  | jq -r '.[0].secrets[].name'
```
Expected (password mode): includes `KC_DB_USERNAME` and `KC_DB_PASSWORD`.

---

## 3. UX Tests

### 3.1 CLI output / error message clarity
The error message from the validation block (section 1.4) must name the variable and the condition. Assert the message contains both `keycloak_database_password` and `keycloak_db_iam_auth_enabled`.

### 3.2 tfvars example readability
```bash
grep -n "keycloak_db_iam_auth_enabled" "$TF_DIR/terraform.tfvars.example"
grep -n "keycloak_database_password" "$TF_DIR/terraform.tfvars.example"
```
Expected: the new flag is documented with a comment explaining both modes, and `keycloak_database_password` is commented out with a note that it is only required when the flag is false.

### 3.3 Web UI
**Not Applicable** - this change has no web UI surface (no Keycloak admin UI or frontend changes).

---

## 4. Deployment Surface Tests

This change is anchored to the Terraform/ECS surface only. Docker Compose and Helm are explicitly out of scope.

### 4.1 Docker wiring
**Not Applicable** - the docker-compose files (`docker-compose.yml`, `docker-compose.podman.yml`, `docker-compose.prebuilt.yml`) use PostgreSQL for local dev and are intentionally not modified by this change. The `KEYCLOAK_DB_PASSWORD` references there remain unchanged.

Assertion (out-of-scope surfaces untouched by the plan):
```bash
terraform show -json /tmp/iam.tfplan \
  | jq -r '.resource_changes[].address' \
  | grep -Ei 'compose|docker' || echo "OK: docker-compose untouched"
```
Expected: `OK: docker-compose untouched`.

### 4.2 Terraform / ECS wiring
This is the primary surface. Anchored on concrete files:

- `terraform/aws-ecs/variables.tf` - new `keycloak_db_iam_auth_enabled` variable present.
  ```bash
  grep -n 'variable "keycloak_db_iam_auth_enabled"' "$TF_DIR/variables.tf"
  ```
- `terraform/aws-ecs/locals.tf` - `is_keycloak_db_iam` local present.
  ```bash
  grep -n 'is_keycloak_db_iam' "$TF_DIR/locals.tf"
  ```
- `terraform/aws-ecs/keycloak-database.tf` - proxy `iam_auth` is conditional; `random_password` resource present; `KC_DB_URL` value conditional.
  ```bash
  grep -n 'iam_auth' "$TF_DIR/keycloak-database.tf"
  grep -n 'random_password" "keycloak_db_master"' "$TF_DIR/keycloak-database.tf"
  grep -n 'aws-wrapper:mysql' "$TF_DIR/keycloak-database.tf"
  ```
- `terraform/aws-ecs/keycloak-ecs.tf` - new `keycloak_task_rds_iam_connect` policy; conditional `KC_DB_URL_DRIVER` env.
  ```bash
  grep -n 'keycloak_task_rds_iam_connect' "$TF_DIR/keycloak-ecs.tf"
  grep -n 'KC_DB_URL_DRIVER' "$TF_DIR/keycloak-ecs.tf"
  ```
- `docker/keycloak/Dockerfile` - wrapper JAR vendored.
  ```bash
  grep -n 'aws-advanced-jdbc-wrapper' "$REPO/docker/keycloak/Dockerfile"
  ```

### 4.3 Helm / EKS wiring
**Not Applicable** - Helm charts (`charts/**`) are out of scope per the issue and are not modified. No `charts/` file should appear in the plan.

### 4.4 Deploy and verify
Apply the IAM-mode plan in a non-prod account and verify the running stack.

```bash
cd "$TF_DIR"
terraform apply /tmp/iam-mode.tfplan
```

Post-apply assertions:

```bash
# 1) Proxy has IAM auth required
aws rds describe-db-proxies --proxy-name keycloak-proxy \
  --query 'DBProxies[0].RequireTLS' --output text
# Expected: true

aws rds describe-db-proxies --proxy-name keycloak-proxy \
  --query 'DBProxies[0].Auth[0].IAMAuth' --output text
# Expected: REQUIRED

# 2) SSM parameter points at the proxy endpoint
aws ssm get-parameter --name /keycloak/database/url --with-decryption \
  --query 'Parameter.Value' --output text
# Expected: jdbc:aws-wrapper:mysql://keycloak-proxy.<hash>.<region>.rds.amazonaws.com:3306/keycloak

# 3) Task role has the rds-db:connect policy attached
aws iam list-role-policies --role-name keycloak-task-role-$AWS_REGION \
  --query 'PolicyNames' --output text
# Expected: includes keycloak-task-rds-iam-connect (when flag on)

# 4) Running task definition has NO KC_DB_PASSWORD in its secrets
TASK_DEF=$(aws ecs describe-services --cluster keycloak --services keycloak \
  --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$TASK_DEF" \
  --query 'taskDefinition.containerDefinitions[0].secrets[].name' --output text
# Expected: KEYCLOAK_ADMIN KEYCLOAK_ADMIN_PASSWORD KC_DB_URL  (no KC_DB_PASSWORD / KC_DB_USERNAME)
```

Force a new deployment so running tasks pick up the new task definition (addresses review blocker on cutover sequencing):
```bash
aws ecs update-service --cluster keycloak --service keycloak --force-new-deployment
# Wait for steady
aws ecs wait services-stable --cluster keycloak --services keycloak
```

### 4.5 Rollback verification
Roll back to password mode by toggling the flag and re-applying.

```bash
cd "$TF_DIR"
terraform plan -var keycloak_db_iam_auth_enabled=false -out /tmp/rollback.tfplan
terraform apply /tmp/rollback.tfplan
aws ecs update-service --cluster keycloak --service keycloak --force-new-deployment
aws ecs wait services-stable --cluster keycloak --services keycloak
```
Post-rollback assertions:
```bash
# Proxy IAM auth back to DISABLED
aws rds describe-db-proxies --proxy-name keycloak-proxy \
  --query 'DBProxies[0].Auth[0].IAMAuth' --output text
# Expected: DISABLED

# SSM URL back to cluster endpoint
aws ssm get-parameter --name /keycloak/database/url --with-decryption \
  --query 'Parameter.Value' --output text
# Expected: jdbc:mysql://<cluster-endpoint>:3306/keycloak

# Task secrets include KC_DB_PASSWORD again
TASK_DEF=$(aws ecs describe-services --cluster keycloak --services keycloak \
  --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$TASK_DEF" \
  --query 'taskDefinition.containerDefinitions[0].secrets[].name' --output text
# Expected: includes KC_DB_USERNAME and KC_DB_PASSWORD
```

### 4.6 Wrapper JAR supply-chain check
```bash
# Confirm the vendored JAR is pinned and hashed in the Dockerfile
grep -niE 'aws-advanced-jdbc-wrapper.*[0-9]+\.[0-9]+\.[0-9]+|sha256' "$REPO/docker/keycloak/Dockerfile"
```
Expected: a comment or ARG pinning a specific version, and ideally a recorded hash. Run a CVE scan on the pinned version before rollout.

---

## 5. End-to-End API Tests

The IAM-mode E2E cannot be meaningfully unit-tested; it requires an applied stack in an AWS account. These steps exercise the full Keycloak->proxy->Aurora path with IAM auth.

### 5.1 Keycloak is healthy after IAM-mode cutover
```bash
# ALB health
ALB_DNS=$(terraform -chdir="$TF_DIR" output -raw keycloak_alb_dns)
curl -fsS "http://$ALB_DNS/health/ready" | jq .
# Expected: status "UP"

# ECS task health
aws ecs describe-services --cluster keycloak --services keycloak \
  --query 'services[0].runningCount' --output text
# Expected: 1 (desired count)
```

### 5.2 Keycloak can actually reach the DB (IAM token works)
```bash
# Exec into a running Keycloak task and confirm a DB-backed operation works
TASK_ARN=$(aws ecs list-tasks --cluster keycloak --service-name keycloak --desired-status RUNNING \
  --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster keycloak --task "$TASK_ARN" --container keycloak --interactive \
  --command "curl -fsS http://localhost:9000/health/ready" 
# Expected: ready check passes (this implicitly proves the datasource connected on startup)
```

Then exercise a real DB-backed Keycloak operation (login realm query):
```bash
# Hit the Keycloak admin realm endpoint via the ALB; a successful 200/30x proves the DB is reachable
curl -fsS -o /dev/null -w '%{http_code}\n' "http://$ALB_DNS/realms/master/.well-known/openid-configuration"
# Expected: 200
```

### 5.3 Fail-closed: plaintext connection to the proxy is rejected
```bash
PROXY_ENDPOINT=$(aws rds describe-db-proxies --proxy-name keycloak-proxy \
  --query 'DBProxies[0].Endpoint' --output text)
# Attempt a plaintext (non-TLS) MySQL handshake - must be rejected because require_tls=true
timeout 5 bash -c "echo > /dev/tcp/$PROXY_ENDPOINT/3306" 2>/dev/null && \
  echo "WARN: TCP open (expected); now assert TLS is required at the protocol layer" || \
  echo "TCP closed"
# Authoritative check: a non-TLS mysql client login must fail with an SSL/TLS error.
# (Use the mariadb client with --skip-ssl; expect a handshake/TLS-required error, not auth success.)
```
Expected: any non-TLS login attempt fails; IAM auth tokens are never accepted over plaintext.

### 5.4 IAM token is scoped to the task role (negative case)
From a principal WITHOUT `rds-db:connect` on the proxy, token generation/connect must fail:
```bash
# Generate a token as the current (non-task) caller and attempt to connect - expect rejection
TOKEN=$(aws rds generate-db-auth-token \
  --hostname "$PROXY_ENDPOINT" --port 3306 --region "$AWS_REGION" \
  --username keycloak)
# Attempt login with this token using a mysql client; expect 'Access denied' because the
# caller lacks rds-db:connect on the proxy dbuser.
```
Expected: access denied (proves the policy is scoped, not open).

### 5.5 Rotation still works end-to-end in IAM mode
```bash
# Trigger a manual rotation and confirm the proxy picks up the new backend credential
SECRET_ARN=$(aws secretsmanager describe-secret --secret-id keycloak/database \
  --query 'ARN' --output text)
aws secretsmanager rotate-secret --secret-id "$SECRET_ARN" --rotation-lambda-arn \
  "$(aws lambda get-function --function-name ${NAME}-rotate-rds --query 'Configuration.FunctionArn' --output text)"
# Wait, then confirm Keycloak is still healthy (it should be - it uses IAM, not the rotated password)
sleep 60
curl -fsS "http://$ALB_DNS/realms/master/.well-known/openid-configuration" -o /dev/null -w '%{http_code}\n'
# Expected: 200 (Keycloak unaffected by backend password rotation)
```

---

## 6. Test Execution Checklist
- [ ] Section 1.1 (static checks: validate, fmt, checkov) passes
- [ ] Section 1.2 (password-mode plan is a no-op) verified
- [ ] Section 1.3 (IAM-mode plan change set matches the seven expected resources) verified
- [ ] Section 1.4 (validation rejects missing password in password mode) verified
- [ ] Section 1.5 (terraform console accepts the new variable) verified
- [ ] Section 2 (backwards compat: existing tfvars, defaults, rotation wiring, secret-presence-by-mode) verified
- [ ] Section 3 (UX: error message clarity, tfvars example; web UI N/A) verified
- [ ] Section 4.1 (docker-compose N/A) verified
- [ ] Section 4.2 (Terraform/ECS wiring greps) verified
- [ ] Section 4.3 (Helm N/A) verified
- [ ] Section 4.4 (deploy + post-apply assertions + force-new-deployment) verified
- [ ] Section 4.5 (rollback to password mode) verified
- [ ] Section 4.6 (wrapper JAR pinned + hashed) verified
- [ ] Section 5.1 (Keycloak healthy post-cutover) verified
- [ ] Section 5.2 (DB-backed Keycloak operation works via IAM) verified
- [ ] Section 5.3 (fail-closed: plaintext to proxy rejected) verified
- [ ] Section 5.4 (IAM token scoped to task role) verified
- [ ] Section 5.5 (rotation works in IAM mode, Keycloak unaffected) verified
- [ ] Unit tests: n/a (no Python/app code changed); add `terraform test` cases under `terraform/aws-ecs/tests/` for the variable validation and the conditional locals if the team adopts `terraform test`
- [ ] Integration tests: the E2E in Section 5 serves as the integration test
- [ ] `terraform validate` + `terraform plan` (both modes) pass with no regressions
