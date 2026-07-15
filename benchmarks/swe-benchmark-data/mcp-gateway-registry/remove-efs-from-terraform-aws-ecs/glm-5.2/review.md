# Expert Review: Remove Amazon EFS from the Terraform AWS ECS deployment

*Created: 2026-07-15*
*Reviewer panel: Pixel (Frontend), Byte (Backend), Circuit (SRE/DevOps), Cipher (Security), Sage (SMTS)*
*Artifact under review: `./lld.md`*

## Frontend Engineer - Pixel

**Focus:** UI/UX, components, state, API integration.

**Strengths**
- The change has no frontend surface, and the LLD correctly scopes itself to Terraform, scripts, and docs. No frontend reviewer would be blocked.
- The `SCOPES_CONFIG_PATH` repoint is a single string change in an environment variable, which is the least invasive way to decouple the auth-server from EFS.

**Concerns**
- None directly. The frontend does not read from EFS or from `SCOPES_CONFIG_PATH`.
- Minor: if any frontend build or dev tooling references the scopes-init image (e.g., a local dev script that runs `Dockerfile.scopes-init`), deleting it could break a local workflow. The LLD does not mention a dev/docs check for this.

**New libraries / infra dependencies**
- None.

**Better alternatives considered**
- None from a frontend perspective.

**Recommendations**
- Add a one-line check to the testing plan confirming no developer-facing script (under `scripts/` or `Makefile`) invokes the deleted `run-scopes-init-task.sh` or builds `Dockerfile.scopes-init`.

**Questions for author**
- Is the scopes-init image used by any local-dev or CI workflow outside `terraform/aws-ecs/scripts/`?

**Verdict:** APPROVED

---

## Backend Engineer - Byte

**Focus:** API design, data models, business logic, performance.

**Strengths**
- The design correctly identifies that EFS is contained inside the `mcp-gateway` sub-module and that the root passes no EFS variables, so the blast radius is small and well-understood.
- Reusing the existing registry-service pattern (`volume = {}`, `mountPoints = []` plus a comment) for auth-server and mcpgw is the right call: it is consistent, minimal, and an entry-level developer can copy it.
- Choosing to image-bake `scopes.yml` for the auth-server is the minimal change that preserves the existing file-based scopes loader behavior without rewriting Python code. This respects the issue scope.
- Making DocumentDB the only scopes backend (fail-fast in `_initialize_scopes`) surfaces misconfiguration immediately instead of silently falling back to a removed backend.

**Concerns**
- **mcpgw `/app/data` is the real risk and the LLD acknowledges it but does not resolve it.** The volume mounted at `/app/data` strongly implies stateful data. The LLD says "use ephemeral Fargate storage" and then flags durability as an open question. If mcpgw persists anything there (sessions, cached tool output, uploaded files), this change silently turns it into per-task ephemeral state and will cause data loss or inconsistency across tasks. The verdict cannot be a clean APPROVE while this is unresolved.
- **`SCOPES_CONFIG_PATH` value correctness.** The LLD proposes `/app/scopes.yml` and a `COPY auth_server/scopes.yml /app/scopes.yml`, but it does not confirm the auth-server image's working directory or whether `/app` is writable/expected. The original path was `/efs/auth_config/auth_config/scopes.yml` (note the doubled `auth_config`), which suggests the scopes loader reads a specific path; the implementer must verify the loader does not also expect a sibling directory structure. The LLD lists this under Open Questions, which is honest, but it weakens the "implementer can just do it" claim.
- **No mention of the `agents` access point.** `storage.tf` defines six access points (`servers`, `models`, `logs`, `agents`, `auth_config`, `mcpgw_data`), but the outputs only expose four (`servers`, `models`, `logs`, `auth_config`). The `agents` and `mcpgw_data` access points are not in the outputs. This is fine because the whole module is deleted, but the LLD's "Key Files Reviewed" table says "six access points" while the outputs section implies four; a careful implementer might wonder if some access point is consumed elsewhere. Worth a one-line note that all access points disappear with the module.
- **`build-and-push-scopes-init.sh` is referenced but absent.** The LLD notes this and says "delete if present." Good, but it hints the EFS flow may already be partially dead, which the issue description could acknowledge to set implementer expectations.

**New libraries / infra dependencies**
- None introduced. The `terraform-aws-modules/efs/aws` module dependency is removed from the effective graph.

**Better alternatives considered**
- Alt 3 (load scopes from DocumentDB) is correctly rejected for scope creep. Agreed: the auth-server loader rewrite is a separate, larger change.

**Recommendations**
- Block the production apply on a written confirmation from the mcpgw service owners that `/app/data` is transient. Until then, the mcpgw volume removal (Step 4) should be gated. Consider splitting the change into two PRs: (1) remove EFS module/outputs/vars/scripts/docs and the auth-server mounts; (2) remove the mcpgw mount once durability is confirmed. This keeps the low-risk cleanup unblocked.
- In Step 3, require the implementer to verify the auth-server scopes loader's expected path and directory layout before choosing `/app/scopes.yml`, and to update the loader if it expects a different structure.

**Questions for author**
- Has anyone confirmed whether mcpgw writes durable state to `/app/data`?
- Why does the original `SCOPES_CONFIG_PATH` contain the doubled `auth_config/auth_config` segment, and will the image-baked path need to replicate that?

**Verdict:** APPROVED WITH CHANGES

---

## SRE/DevOps Engineer - Circuit

**Focus:** Deployment, monitoring, scaling, infrastructure.

**Strengths**
- The destroy plan is clearly described: EFS file system, six access points, mount targets in every private subnet, and the NFS security group plus its manual egress rule all go away. This is a clean cost and operational footprint reduction.
- The post-deploy script change is operationally sound: fail-fast on a missing DocumentDB endpoint is better than a silent fallback to a removed backend, and it uses the existing log helpers.
- The deployment-surface checklist is thorough and ticks off every file an operator would need to touch.
- Keeping `data.aws_vpc.vpc` (because `ecs-services.tf` still uses it) shows the author actually traced the data-source consumers rather than blanket-deleting.

**Concerns**
- **State migration / destroy ordering.** Removing `module "efs"` while ECS services still reference it in state could produce a plan error if Terraform tries to destroy the EFS access points before the task definitions that mount them are updated. The LLD lists the steps in an order that edits `ecs-services.tf` (Steps 2 and 4) before deleting `storage.tf` (Step 1 is listed first but the implementer should apply the ECS edits first, or apply in one plan). The LLD should explicitly state: apply all `.tf` edits in a single `terraform plan/apply` so references and resources are removed together, avoiding an intermediate state where a task definition still mounts a destroyed file system.
- **`terraform init` after removing the module.** Once `module "efs"` is gone, `terraform init` will no longer download `terraform-aws-modules/efs/aws`. The `.terraform/modules/` cache and lock file (`.terraform.lock.hcl`) may still reference it. The testing plan should include a `terraform init -upgrade` or at least `terraform init` followed by a check that the EFS module is gone from the lock file, so a stale lock does not cause CI drift.
- **Rollback path.** The LLD says reversibility is "Medium (re-add module)" but does not describe the rollback procedure. If the apply breaks auth-server (e.g., scopes path wrong), the operator needs to know how to roll back: `terraform apply` the previous revision and redeploy the previous task definition. A one-line rollback note would help.
- **DRY_RUN path.** The new fail-fast branch returns early on DRY_RUN only when the endpoint is present; when the endpoint is absent it fails before the DRY_RUN check. That is fine (dry-run should still fail on missing prerequisites), but the LLD should note that `--dry-run` will now exit non-zero on a non-DocumentDB deployment, which may surprise CI that expects dry-run to always succeed.

**New libraries / infra dependencies**
- None. Removes the `terraform-aws-modules/efs/aws` module dependency.

**Better alternatives considered**
- Alt 1 (keep the module, stop mounting) is correctly rejected for leaving the cost and dead code in place.

**Recommendations**
- Reorder the implementation steps (or add a note) so all Terraform edits land in one plan; do not delete `storage.tf` while `ecs-services.tf` still references `module.efs`.
- Add `terraform init` + lock-file verification to the testing plan.
- Add a rollback note and document the DRY_RUN behavior change.

**Questions for author**
- Is there a CI pipeline that runs `post-deployment-setup.sh --dry-run` and expects exit 0? If so, it needs updating.
- Will the EFS destroy require any AWS-side retries (mount targets can take time to delete)?

**Verdict:** APPROVED WITH CHANGES

---

## Security Engineer - Cipher

**Focus:** AuthN/AuthZ, validation, OWASP, data protection.

**Strengths**
- Removing EFS reduces the attack surface: one fewer NFS service exposed inside the VPC, one fewer security group with an ingress rule on port 2049, and one fewer `elasticfilesystem:*` wildcard in the documented IAM policy. This is a net security improvement.
- Removing `elasticfilesystem:*` from the example IAM policy in `terraform/aws-ecs/README.md` aligns the documented least-privilege policy with the actual deployment needs.
- Image-baking `scopes.yml` removes a runtime write path (the scopes-init task that wrote to `/mnt` on EFS), which means one fewer path to tamper with scopes at runtime.

**Concerns**
- **The `elasticfilesystem:*` removal is docs-only.** The LLD correctly notes that no `.tf` IAM policy grants `elasticfilesystem:*` (verified by grep). But this raises a question: if the running tasks could mount EFS, what IAM principal was authorizing `elasticfilesystem:ClientMount`/`ClientWrite`? Either (a) a broad AWS-managed policy like `AmazonECSTaskExecutionRolePolicy` or a customer-managed policy applied out-of-band grants it, or (b) the EFS access points had an identity-based policy. The LLD flags out-of-band IAM under Open Questions, which is correct, but the security reviewer wants that confirmed before sign-off: removing the README line does not revoke any actual permission. If a broad policy is still attached, the EFS permissions linger as latent privilege even after EFS is gone (harmless once EFS is destroyed, but worth noting for hygiene).
- **Scopes file in the image.** Baking `scopes.yml` into the image means scope changes require an image rebuild and redeploy, which is fine operationally but means the scopes file is now in the image layer. If `scopes.yml` contains any sensitive mapping (it should not, but verify), it would be baked into an image that may be pushed to ECR. Confirm `scopes.yml` contains no secrets before image-baking. The file is in-repo at `auth_server/scopes.yml` (10 KB), so it is already in version control; image-baking does not change its exposure, but it is worth a one-line confirmation.
- **No new auth surface.** The change does not touch AuthN/AuthZ (Keycloak, Auth0, Okta paths are untouched). Good.

**New libraries / infra dependencies**
- None.

**Better alternatives considered**
- None from a security perspective; the chosen approach reduces surface.

**Recommendations**
- Add a confirmation step to the testing plan: after apply, verify the ECS task role and execution role no longer require `elasticfilesystem` permissions (check attached customer-managed policies), and note any out-of-band policy that still grants them.
- Add a one-line check that `auth_server/scopes.yml` contains no secrets (e.g., run the repo's existing secret scanner / `.secrets.baseline`) before image-baking.

**Questions for author**
- Which IAM principal currently authorizes EFS `ClientMount`/`ClientWrite` for the auth-server and mcpgw tasks, and is it in-repo or out-of-band?

**Verdict:** APPROVED WITH CHANGES

---

## SMTS (Overall) - Sage

**Focus:** Architecture, code quality, maintainability.

**Strengths**
- The design is well-scoped and follows the codebase's own precedent (the registry service's EFS removal). Finishing an in-flight migration by extending an existing pattern is exactly the right architectural instinct.
- The codebase analysis is grounded in real file/line references, the integration points are traced (including the shared `data.aws_vpc.vpc` data source), and the constraints section is honest about what is and is not in scope.
- The alternatives matrix is reasonable and the rejected alternatives are dismissed for the right reasons (scope creep, over-engineering, half-finishing).
- Documentation updates are enumerated file-by-file with line numbers, which makes the PR reviewable.

**Concerns**
- **The mcpgw `/app/data` question is a genuine blocker and is deferred rather than resolved.** Byte flagged this too. A design that says "remove the mount, but maybe don't if data is durable" is not a complete design for that component. The LLD should either (a) split the change so the mcpgw mount removal is a separate, gated step, or (b) state the assumed disposition (ephemeral) explicitly as a design decision with a rollback if wrong. Right now it reads as "do this, unless you shouldn't," which is the kind of ambiguity CLAUDE.md warns against.
- **Step ordering vs. single-plan application.** Circuit flagged this. The LLD lists Step 1 (delete storage.tf) before Steps 2 and 4 (edit ecs-services.tf). If applied literally as separate edits, an intermediate `terraform plan` would error on dangling `module.efs` references. The LLD must state that all `.tf` edits are one atomic plan.
- **The estimated LOC table is optimistic on the "deleted" side and vague on tests.** ~260 deleted lines is plausible for `storage.tf` (182) + `run-scopes-init-task.sh` (488) + `Dockerfile.scopes-init` + outputs/vars, but the testing estimate (~40) is thin given the deployment-surface checks required. Not a blocker, but tighten it.
- **No mention of the `agents` access point consumer.** Minor (Byte also noted). All access points vanish with the module, which is correct, but a sentence confirming no consumer outside `storage.tf`/`outputs.tf`/`ecs-services.tf` references `module.efs.access_points["agents"]` would close the loop.

**New libraries / infra dependencies**
- None. Net reduction (removes the EFS Terraform module and the scopes-init Docker image).

**Better alternatives considered**
- Agreed with the rejection of Alt 1 (keep module) and Alt 3 (scopes from DocumentDB). Alt 2 (S3 Mountpoint) is correctly deferred to a separate decision if mcpgw needs durability.

**Recommendations**
- Split the mcpgw mount removal into a gated step or make the ephemeral assumption an explicit design decision with a documented rollback.
- Add the "single atomic plan" instruction to the implementation steps.
- Confirm no `agents` access point consumer exists outside the deleted files.
- Tighten the testing LOC estimate and ensure the testing plan covers `terraform init` lock-file hygiene and the DRY_RUN behavior change.

**Questions for author**
- Can the mcpgw durability question be answered before this PR merges, or should the mcpgw mount removal be a follow-up?
- Is there any consumer of `module.efs.access_points["agents"]` outside the files being deleted?

**Verdict:** APPROVED WITH CHANGES

---

## Review Summary

| Reviewer | Verdict | Blockers | Key Recommendations |
|----------|---------|----------|---------------------|
| Frontend (Pixel) | APPROVED | 0 | Confirm no dev/CI script invokes the deleted scripts/image. |
| Backend (Byte) | APPROVED WITH CHANGES | 1 | Gate mcpgw `/app/data` removal on durability confirmation; verify scopes loader path. |
| SRE (Circuit) | APPROVED WITH CHANGES | 0 | Apply all TF edits in one plan; add `terraform init` lock-file check; document rollback and DRY_RUN change. |
| Security (Cipher) | APPROVED WITH CHANGES | 0 | Confirm out-of-band IAM no longer needs `elasticfilesystem`; verify `scopes.yml` has no secrets. |
| SMTS (Sage) | APPROVED WITH CHANGES | 1 | Resolve or gate the mcpgw durability question; add single-plan instruction; confirm `agents` access point has no external consumer. |

**Overall disposition:** APPROVED WITH CHANGES. The design is sound and well-grounded in the codebase, but two blockers must be addressed before implementation: (1) the mcpgw `/app/data` durability question must be resolved or the mcpgw mount removal must be split into a gated follow-up, and (2) the implementation steps must instruct a single atomic `terraform plan/apply` so dangling `module.efs` references cannot occur. The remaining items are hardening (lock-file hygiene, rollback notes, DRY_RUN behavior, out-of-band IAM confirmation, scopes.yml secret check) and should be folded into the LLD and testing plan.

## Next Steps
1. Author addresses the two blockers: split/gate the mcpgw mount removal and add the single-plan instruction.
2. Author folds in the hardening recommendations (terraform init lock-file check, rollback note, DRY_RUN behavior change, out-of-band IAM confirmation, scopes.yml secret scan).
3. Author confirms the `agents` access point has no external consumer and updates the LOC/test estimates.
4. Re-review the updated LLD and testing plan before implementation begins.
