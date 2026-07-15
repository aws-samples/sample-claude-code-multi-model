# Expert Review: Replace Keycloak DB password with RDS IAM authentication

*Created: 2026-07-15*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

Reviewers:

| Role | Reviewer | Focus |
|------|----------|-------|
| Frontend Engineer | Pixel | UI/UX, components, state, API integration |
| Backend Engineer | Byte | API design, data models, business logic, performance |
| SRE/DevOps Engineer | Circuit | Deployment, monitoring, scaling, infrastructure |
| Security Engineer | Cipher | AuthN/AuthZ, validation, OWASP, data protection |
| SMTS (Overall) | Sage | Architecture, code quality, maintainability |

---

## Frontend Engineer - Pixel

### Strengths
- There is no frontend surface in this change, which is correct: the IAM auth cutover is entirely Terraform + image. No web UI, CLI output, or error-message changes are introduced that a user-facing surface would need to absorb.
- The optional `keycloak_db_auth_mode` Terraform output gives operators a clear, queryable signal of the active mode without reading logs.

### Concerns
- None from a UI/UX standpoint.

### New libraries / infra dependencies
- None on the frontend.

### Better alternatives considered
- N/A.

### Recommendations
- If the team later wants visibility in the admin console, surface the DB auth mode in the existing observability dashboard rather than the Keycloak admin UI. Out of scope for this issue.

### Questions for author
- None.

### Verdict: APPROVED
No frontend impact.

---

## Backend Engineer - Byte

### Strengths
- The design correctly identifies that a one-shot password (env var or sidecar file) cannot satisfy the 15-minute token expiry because Keycloak's connection pool acquires connections over time. Choosing a driver-level plugin (AWS Advanced JDBC Driver `iam`) is the only correct enabler, and the LLD states this explicitly.
- Reusing the existing `keycloak/database` secret shape (`{username,password}`) means the rotation Lambda and its 4-step protocol need zero changes. Good minimisation of blast radius.
- The conditional locals (`keycloak_iam_driver_env`, `keycloak_password_secrets`) mirror the existing `concat` + `count` pattern used for DocumentDB gating, so the code stays idiomatic to this repo.

### Concerns
1. **`KC_DB_URL_PROPERTIES` with `user=` is fragile.** Putting the DB username into a URL query property works for the wrapper, but it leaks the username into the SSM parameter value and into CloudTrail/SSM GetParameter logs (not the password, but still). More importantly, the wrapper's `iam` plugin expects the username to match the IAM `dbuser` mapping. If `var.keycloak_database_username` ever changes, both the secret's `username` and the `user=` property must change in lockstep, and the LLD does not make that coupling explicit. Consider deriving the connection user from a single local rather than restating `var.keycloak_database_username` in two places.
2. **Master password cutover is disruptive.** Step 3 changes `master_password` on `aws_rds_cluster.keycloak` during `terraform apply`. This is a one-time rotation that briefly interrupts the cluster. The LLD mentions a maintenance window in Rollout but the Implementation Details step itself reads as a routine edit. The implementer could miss the disruption. Recommend a loud `# NOTE:` on that line and a `terraform plan` review step in testing.md that asserts the master password change appears in the plan.
3. **`KC_DB_URL_DRIVER` may not be a real Keycloak env var.** Keycloak 25 (Quarkus) configures the datasource driver via `quarkus.datasource."keycloak".db-kind` / `jdbc.driver`, not a `KC_DB_URL_DRIVER` env in all builds. The LLD should verify the exact Keycloak/Quarkus property name (`quarkus.datasource.jdbc.driver` -> env `KC_DATASOURCE_DRIVER`? or via `KC_DB_URL_DRIVER` which IS documented). This is the single highest-risk implementation detail and is marked as an open question only loosely. The reviewer flags it as a blocker to confident implementation.
4. **Non-optimized vs optimized image divergence.** The custom image runs `kc.sh build` (optimized) with `KC_DB=mysql` baked; the public prebuilt image runs `start` (non-optimized) and reads `KC_DB=mysql` from env. The wrapper JAR in `/opt/keycloak/providers/` is picked up in both, but the optimized build caches driver resolution at build time. If the optimized image is used, the driver must be present at `build` time (it is, via COPY) and the `KC_DB_URL_DRIVER` must also be baked or provided at runtime. The LLD should state which image the IAM path targets first (the custom built image, since the public prebuilt image is not rebuilt by this repo).

### New libraries / infra dependencies
- `aws-advanced-jdbc-wrapper` JAR - justified; it is the only way to get transparent token refresh without custom Java SPI code.

### Better alternatives considered
- A custom Quarkus `CredentialsProvider` (Alt 2) was correctly rejected as higher-maintenance.

### Recommendations
- Make the connection username a single local (e.g. `local.keycloak_db_user`) used in both the secret and the URL property.
- Add an explicit assertion in testing.md that `terraform plan` with the flag on shows exactly one `master_password` update and that the implementer schedules it.
- Resolve open question on the exact Keycloak driver property name before implementation; treat it as a design verification task, not a deferred question.

### Questions for author
- Which Keycloak image (custom built vs public prebuilt) is the IAM path rolled out on first?
- Is `KC_DB_URL_DRIVER` the documented Keycloak 25 env, or should this be `quarkus.datasource.jdbc.driver`?

### Verdict: APPROVED WITH CHANGES
The `KC_DB_URL_DRIVER` property-name correctness (concern 3) and the username coupling (concern 1) must be resolved before an implementer can proceed confidently.

---

## SRE/DevOps Engineer - Circuit

### Strengths
- The design turns an orphaned, already-paid-for resource (the RDS Proxy) into the security boundary. No new long-lived infra to operate; the proxy just gets wired in.
- The backwards-compatible flag with a default of `false` means a `terraform plan` with the flag off is a no-op. That is the right rollback story: revert the flag, apply, done.
- Reusing the existing rotation Lambda and 30-day schedule means no new Lambda to maintain and no new failure mode in the rotation path.
- The observability section adds a concrete CloudWatch alarm on `IAMAuthenticationFailures` for the rollout week. Good.

### Concerns
1. **`require_tls = true` flips only with the flag, but the proxy is shared by... nothing else.** Confirm no other consumer connects to this proxy today. Since the proxy endpoint is currently unwired, nothing does. Fine, but worth a one-line assertion in testing.md (grep for proxy endpoint references == 0 pre-change).
2. **Proxy endpoint in SSM is a breaking change for any cached value.** Tasks already running will hold the old `KC_DB_URL` (cluster endpoint) until the task definition is redeployed. `terraform apply` updates the SSM parameter and the task definition, but running tasks are not restarted automatically. The rollout must force a new deployment of the Keycloak ECS service. The LLD does not mention `aws_ecs_service` deployment updates or a forced new deployment. Add a step: after apply, run `aws ecs update-service --force-new-deployment` for the keycloak service, or add `force_new_deployment = true` / trigger via `triggers` on the task definition.
3. **No mention of `terraform state` for the `random_password`.** When toggling the flag off then on again, `random_password` with `count` will be destroyed and recreated, generating a new master password each toggle-on. That rotates the Aurora master password on every off->on transition. Acceptable but should be documented so operators do not toggle casually.
4. **Checkov `CKV_AWS_162` skip rewrite.** The skip is on the cluster, but IAM auth is enforced on the proxy, not the cluster. A reviewer/tooling may still flag the cluster as "IAM DB auth not enabled." The rewritten comment explains the proxy enforces it, but checkov's rule `CKV_AWS_162` specifically checks the cluster's `enable_iam_database_authentication`. Since the cluster does NOT have it enabled (by design - the proxy handles IAM), the skip remains legitimately required. The LLD handles this correctly; this is confirmation, not a concern.
5. **KMS key policy for the task role.** The task exec role already has `kms:Decrypt`. The task role (runtime) generating the IAM token does not need KMS or Secrets access (the wrapper calls STS/RDS `generate-db-auth-token`, not Secrets). Confirm the new `rds-db:connect` policy is the only addition to the task role. The LLD is correct here.

### New libraries / infra dependencies
- AWS Advanced JDBC Driver wrapper JAR vendored into the image. Adds a supply-chain artifact to track (version, source, SRI/hash). Recommend pinning and recording the SHA in the Dockerfile comment.

### Better alternatives considered
- Direct cluster IAM auth (Alt 1) would have required an init SQL task and IAM DB user lifecycle - more to operate. Proxy approach is operationally simpler.

### Recommendations
- Add an explicit ECS force-new-deployment step to the rollout and testing plan (concern 2).
- Pin and record the wrapper JAR hash (concern: supply chain).
- Document the off->on toggle master-password rotation side effect (concern 3).

### Questions for author
- How is the Keycloak ECS service redeployed after the task definition changes today? Is there an existing `force_new_deployment` mechanism, or does the team run `aws ecs update-service` manually?
- Is there a CI pipeline that runs `terraform plan` on PRs, and will the `random_password`/master-password change trigger a noisy plan in CI for every PR while the flag is being staged?

### Verdict: APPROVED WITH CHANGES
The ECS force-new-deployment gap (concern 2) is an operational correctness issue for the cutover and must be addressed in the LLD/testing plan.

---

## Security Engineer - Cipher

### Strengths
- The core security property is sound: in IAM mode, no long-lived DB password exists in operator config (`terraform.tfvars`), in the ECS task environment, or in any value Keycloak reads. The credential Keycloak holds is a 15-minute token bound to the task role, revocable via IAM.
- Scoping `rds-db:connect` to the specific proxy `dbuser` resource (`arn:aws:rds-db:...:dbuser:<proxy>/<user>`) follows least privilege. Good.
- The master credential remains in Secrets Manager (KMS-encrypted, rotated every 30 days) as the proxy's backend and break-glass. This is the correct place for it.
- Removing `KC_DB_PASSWORD` from the container environment eliminates a whole class of secret-exfiltration risk (describe-task, env dump, core dump).

### Concerns
1. **TLS requirement.** IAM auth tokens are only valid over TLS. The LLD sets `require_tls = true` on the proxy in IAM mode, which is correct, but the Keycloak->proxy leg must actually negotiate TLS. If the wrapper's default `sslMode` is `DISABLED`, the connection will fail closed (good) but the failure may be misdiagnosed. The LLD addresses this in Open Questions but security-wise it must be fail-closed and verified. Recommend the testing plan explicitly assert that a plaintext connection to the proxy is rejected.
2. **The `user=` in `KC_DB_URL_PROPERTIES` is not secret, but it does reveal the DB username in SSM/CloudTrail.** The username is low-sensitivity (default `keycloak`) but the principle of keeping DB identifiers out of plaintext stores is worth noting. Acceptable trade-off; flag for awareness.
3. **Token in connection logs.** The IAM auth token is a SigV4 pre-signed URL used as the password. If Keycloak or the wrapper logs the JDBC URL or connection properties at INFO, the token could land in CloudWatch. The LLD sets `wrapperLoggerLevel=INFO` initially and suggests downgrading to `SEVERE`. Verify the wrapper does not log the password/token at INFO. Keycloak itself should not log `KC_DB_PASSWORD` (it is absent now), but the wrapper's own logging must be checked. Recommend starting at `SEVERE` and only raising verbosity for active debugging, opposite to the LLD's suggestion.
4. **Secrets Manager secret still contains the password.** The task says "remove the static password from config/env vars entirely." The LLD keeps the password in Secrets Manager (proxy backend). This satisfies the literal requirement (config/env vars), but the reviewer wants to confirm the intent: the password is removed from operator config and the container env, but it is NOT removed from the AWS account. That is by design (proxy needs a backend credential). Document this explicitly in the issue so it is not misread as "no password exists anywhere."
5. **`rds-db:connect` on the task role - confirm no broadening.** The new policy grants `rds-db:connect` only on the keycloak proxy dbuser. Confirm it is not accidentally attached to the task-execution role (which has Secrets/KMS perms and should not also have DB connect). The LLD attaches it to `keycloak_task_role` (runtime), which is correct.
6. **Break-glass path.** With IAM auth required on the proxy, an operator who needs direct DB access for incident response must either use the master password against the cluster endpoint (bypassing the proxy) or assume a role with `rds-db:connect`. The cluster endpoint is still reachable (the SG allows ECS->cluster? Actually the SG `keycloak_db` is attached to the cluster, and the ECS egress rule targets `keycloak_db` SG on 3306, so ECS can reach the cluster endpoint directly too). Document the break-glass procedure (bastion + master password from Secrets) in OPERATIONS.md.

### New libraries / infra dependencies
- AWS Advanced JDBC Driver wrapper - a third-party-ish (AWS-published) JAR added to the image. Perform a quick dependency review (CVE scan on the pinned version) before vendoring. Justified by being the AWS-recommended enabler.

### Better alternatives considered
- Direct cluster IAM auth (Alt 1) would eliminate the proxy's backend password entirely, but introduces IAM DB user lifecycle. The proxy approach keeps a password in Secrets, which is a residual risk but a managed, rotated one. Trade-off is acceptable and well-reasoned.

### Recommendations
- Start `wrapperLoggerLevel` at `SEVERE`, not `INFO` (concern 3). Use INFO only for active debugging with a rollback.
- Add an explicit fail-closed TLS test (concern 1) and a break-glass doc (concern 6).
- Clarify in the issue that the password is removed from config/env, not from the AWS account (concern 4).

### Questions for author
- Does the AWS Advanced JDBC Driver log the connection URL (including token) at INFO? (Verify against the wrapper's docs/source before rollout.)
- What is the documented break-glass DB access procedure today, and does it need updating for IAM mode?

### Verdict: APPROVED WITH CHANGES
The logging-level default (concern 3), explicit fail-closed TLS assertion (concern 1), and break-glass documentation (concern 6) are required before rollout.

---

## SMTS (Overall) - Sage

### Strengths
- The architecture is well-reasoned: it reuses existing infrastructure (the orphaned proxy, the rotation Lambda, the secret, the SGs), introduces exactly one new enabling dependency (the JDBC wrapper), and keeps the change fully reversible behind a flag. This is the right shape for a medium-scope security hardening.
- The codebase analysis is thorough: the orphaned-proxy finding, the issue #1026 / #955 pattern reuse, the optimized-vs-non-optimized image distinction, and the checkov skip lineage are all grounded in actual code. An implementer can follow this.
- The alternatives analysis is honest, including rejecting the sidecar approach as fundamentally broken for 15-minute tokens.

### Concerns
1. **The highest-risk implementation detail is under-specified.** The exact Keycloak/Quarkus property for overriding the JDBC driver (`KC_DB_URL_DRIVER` vs `quarkus.datasource.jdbc.driver`) is the load-bearing piece of the whole design, and it is delegated to an open question. Byte flagged this; Sage elevates it: the LLD should either (a) verify and state the correct property with a citation, or (b) explicitly mark this as a spike that must precede implementation, with a fallback (custom credentials provider) if the wrapper cannot be wired via env alone. Without this, an implementer may stall.
2. **Cutover sequencing is incomplete.** Circuit's force-new-deployment gap is real: changing the SSM parameter and task definition does not restart running tasks. Combined with the one-time master password rotation, the cutover has two sequencing hazards (running tasks hold stale URL; master password change window) that the LLD sequences implicitly. Add an explicit ordered cutover runbook: (1) apply with flag on, (2) force new deployment, (3) verify health, (4) monitor alarm. This belongs in the LLD Rollout section, not just testing.md.
3. **Cross-surface consistency.** The docker-compose path uses PostgreSQL and is correctly left untouched, but a reader of the repo will find `KC_DB_PASSWORD` still referenced in three docker-compose files and `.env.example`. Add a one-line note in the issue/LLD that these are intentionally out of scope (non-AWS local dev path) so a future implementer does not "helpfully" edit them.
4. **Testability of the IAM path end-to-end.** The E2E test in testing.md will require a real ECS exec + a real RDS proxy + IAM. The LLD should call out that the IAM-mode E2E cannot be meaningfully unit-tested; it requires an applied stack in an AWS account. Set expectations so the testing plan is not judged against a unit-test bar it cannot meet.
5. **`random_password` provider.** Confirm the `hashicorp/random` provider is declared. Minor, but an implementer following the steps literally will hit a provider error if it is not. The LLD lists this as an open question; it should be a verification step, not a question.

### New libraries / infra dependencies
- AWS Advanced JDBC Driver wrapper - justified, with a pinning + hash recommendation carried from Circuit's review.

### Better alternatives considered
- All three alternatives are correctly characterized. The chosen approach is the best balance of reuse, correctness, and operability for this repo's constraints.

### Recommendations
- Resolve or spike the driver property question (concern 1) before declaring the LLD implementation-ready.
- Add an explicit ordered cutover runbook to the LLD Rollout section (concern 2).
- Note docker-compose/.env.example are intentionally out of scope (concern 3).
- Set the E2E test expectation (concern 4).
- Convert the `random` provider "open question" to a verification step (concern 5).

### Questions for author
- Is there appetite for a small spike to confirm the wrapper driver wiring before committing the full LLD, given it is the single load-bearing unknown?
- Who owns the cutover runbook - the platform team or the on-call SRE?

### Verdict: APPROVED WITH CHANGES
The design is sound and well-grounded. The driver-property spike (concern 1) and the cutover runbook (concern 2) are the two changes that move this from "good design" to "implementation-ready." The rest are polish.

---

## Review Summary

| Reviewer | Verdict | Blockers | Key Recommendations |
|----------|---------|----------|---------------------|
| Frontend (Pixel) | APPROVED | 0 | None (no frontend impact) |
| Backend (Byte) | APPROVED WITH CHANGES | 2 | Resolve `KC_DB_URL_DRIVER` property name; decouple username into a single local |
| SRE (Circuit) | APPROVED WITH CHANGES | 1 | Add ECS force-new-deployment to cutover; pin+hash wrapper JAR |
| Security (Cipher) | APPROVED WITH CHANGES | 3 | Default `wrapperLoggerLevel=SEVERE`; fail-closed TLS test; break-glass doc |
| SMTS (Sage) | APPROVED WITH CHANGES | 2 | Spike driver-property wiring; add ordered cutover runbook |

### Cross-cutting blockers (must resolve before implementation)
1. **Driver property wiring** (Byte #3, Sage #1): confirm the exact Keycloak 25 / Quarkus property for overriding the JDBC driver to the AWS wrapper, or run a spike. This is load-bearing.
2. **Cutover sequencing** (Circuit #2, Sage #2): add an explicit ordered cutover runbook including an ECS force-new-deployment step.
3. **Token logging** (Cipher #3): default the wrapper logger to `SEVERE`, not `INFO`.

### Next Steps
1. Author spikes/confirms the driver property wiring and updates the LLD Implementation Details Step 6/8 with the verified property name and a fallback.
2. Adds the ordered cutover runbook to the LLD Rollout section and a matching force-new-deployment assertion to testing.md.
3. Flips the wrapper logger default to `SEVERE` and adds the fail-closed TLS test + break-glass doc to testing.md / OPERATIONS.md.
4. Re-words the issue's "remove the static password" language to clarify: removed from operator config and the container env; retained (rotated) in Secrets Manager as the proxy backend.
5. Once the above are in, the design is implementation-ready for a future implementer to execute against the Terraform/ECS surface only.
