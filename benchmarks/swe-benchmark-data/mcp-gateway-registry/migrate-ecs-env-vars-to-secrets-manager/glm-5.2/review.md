# Expert Review: Migrate ECS Environment Variables to AWS Secrets Manager

*Created: 2026-07-15*
*Reviewer panel: Pixel (Frontend), Byte (Backend), Circuit (SRE/DevOps), Cipher (Security), Sage (SMTS/Overall)*
*Related LLD: `./lld.md`*
*Related Issue: `./github-issue.md`*

This review evaluates the design in `lld.md`. Each reviewer captures strengths, concerns, new-dependency justification, alternatives, recommendations, open questions, and a verdict.

---

## Frontend Engineer - Pixel

**Focus:** UI/UX, components, state, API integration.

### Strengths
- The change is infrastructure- and config-loader-level, so there is no frontend surface to break. The frontend consumes the registry/auth APIs unchanged.
- The LLD correctly identifies that no new HTTP endpoints or CLI commands are introduced, so no frontend contract changes.

### Concerns
- None from a frontend perspective. The only user-facing risk is indirect: if a migrated secret fails to resolve at startup and the service fails to boot, the UI goes down. That is an SRE concern (see Circuit), not a frontend one.
- The GitHub PAT / GitHub App credentials are used by the registry to fetch `SKILL.md` content for display. If those secrets fail to resolve, server-rendered skill docs in the UI would degrade. Worth a note but not a blocker.

### New libraries / infra dependencies
- None affecting the frontend.

### Better alternatives considered
- N/A.

### Recommendations
- Add a one-line note in the LLD that GitHub-credential-dependent UI features (skill doc rendering) degrade gracefully if the secret is absent, so frontend is aware. Low priority.

### Questions for author
- Are there any frontend-configured secrets (e.g. tokens baked into the SPA) that should also move? (Quick answer from the codebase: no; the frontend has no server-side secrets.)

### Verdict
**APPROVED** - No frontend impact.

---

## Backend Engineer - Byte

**Focus:** API design, data models, business logic, performance.

### Strengths
- The resolver design is clean and minimal: env-var-first, ARN-fallback, `lru_cache` for one-fetch-per-process. This matches the codebase's existing config patterns and avoids hot-path AWS calls.
- Correctly identifies the three distinct config-loading patterns (Pydantic `BaseSettings` in registry, ad-hoc `os.environ.get` in auth-server, plain `os.getenv` class in metrics-service) and gives a concrete integration plan for each.
- The Pydantic `@model_validator(mode="after")` approach is the right hook: it fills secrets after env loading without fighting `BaseSettings` semantics, and uses `object.__setattr__` to bypass Pydantic's frozen-model guards where needed.
- No new third-party dependency (`boto3` already present) - good.

### Concerns
1. **Synchronous boto3 in an async auth-server.** The auth-server is async (FastAPI/Starlette). The resolver calls `boto3.client("secretsmanager").get_secret_value` synchronously at module import time (config load). This is acceptable because it runs once at startup, not per-request, but it does block the event loop briefly during import. If the auth-server ever loads config lazily inside a request, this becomes a problem. Recommendation: keep resolver calls at import/startup only; document this constraint.
2. **Duplicated resolver across `registry/core/secrets_loader.py` and `auth_server/secrets_loader.py`.** The LLD acknowledges this (Alternative 4) and accepts duplication. The risk is drift: a bug fix in one copy is not applied to the other. Recommendation: add a comment header in both files pointing to each other and a unit test that asserts the two files are byte-identical (cheap guardrail).
3. **`lru_cache` on `_fetch_secret_value` is module-level and never invalidated.** That is intentional (process-lifetime cache), but it means a rotated secret is not picked up until a rolling restart. For ECS this is fine (new tasks get new secrets). For long-running non-ECS processes (local dev), a stale value could persist. Acceptable, but call it out.
4. **The `auth0_management_api_token` secret is count-gated on `var.auth0_enabled`, but the `secrets` block entry in ecs-services.tf references `aws_secretsmanager_secret.auth0_management_api_token[0].arn` only inside the existing `var.auth0_enabled ? [...] : []` conditional.** The LLD shows this correctly, but the implementer must keep both gates in sync - if the secret is gated but the `secrets` block entry is not (or vice versa), Terraform fails. Add an explicit note.
5. **Structured vs. flat secret strings.** The existing IdP secrets store JSON objects (e.g. `{"client_secret": "..."}`) and use the `:jsonkey::` stage. The LLD stores the migrated app secrets as flat `secret_string = var.x`. This is correct for single-value secrets (API tokens, PATs) but means `REGISTRY_API_KEYS` (described as "Multi-key static tokens JSON, Issue #779") is stored as a JSON string in a flat secret. That works (ECS injects the whole string as the env var, which the app parses as JSON), but confirm the app reads `REGISTRY_API_KEYS` as a raw string and parses it itself - which it does (`_REGISTRY_API_KEYS_RAW`). Fine.
6. **`get_secret` strips whitespace with `.strip()`.** For secrets that are PEM private keys (`GITHUB_APP_PRIVATE_KEY`), `.strip()` is safe (leading/trailing whitespace removal does not corrupt a PEM), but `.strip()` on a value that intentionally contains trailing newlines could matter. PEMs end with a newline; stripping it is usually harmless because the PEM parser tolerates it. Acceptable, but note it.

### New libraries / infra dependencies
- None (`boto3` already a dependency). Justified by the LLD.

### Better alternatives considered
- Extracting the resolver into a shared package (Alternative 4 in the LLD). Rejected for complexity. I agree, but add the byte-identical test guardrail above.

### Recommendations
- Document the "startup-only, not per-request" constraint for the resolver.
- Add a test asserting the two resolver copies are identical.
- Add an implementation note about keeping `count` gates on the secret resource and the `secrets`-block conditional in sync.

### Questions for author
- Is `REGISTRY_API_KEYS` ever expected to be a binary secret? (No - it is JSON text.)
- Should the resolver expose a `reload()` for non-ECS long-running processes, or is restart-only acceptable? (Restart-only is acceptable for this migration.)

### Verdict
**APPROVED WITH CHANGES** - Address the duplication guardrail, the sync-in-async note, and the count-gate sync note. No architectural blockers.

---

## SRE/DevOps Engineer - Circuit

**Focus:** Deployment, monitoring, scaling, infrastructure.

### Strengths
- Reuses the existing KMS key (`aws_kms_key.secrets`), the existing `ecs_secrets_access` policy, and the existing `secrets`-block pattern. No new infrastructure primitives - this is exactly the right altitude for an infra migration.
- Secrets are seeded from existing tfvars values, so `terraform apply` is non-disruptive: the secret is created with the current value, ECS starts injecting it, and the app sees the same env var. No coordinated cutover required.
- The `MCP_SECRETS_RESOLVER_ENABLED=false` escape hatch lets operators disable the app-side fetch if AWS API issues block startup - good operational safety.
- CloudTrail already records `GetSecretValue`, so the audit story is free.

### Concerns
1. **`recovery_window_in_days = 0` on all secrets means a `terraform destroy` or a rename deletes the secret immediately with no recovery window.** The existing secrets already do this, so it is consistent, but for application secrets that operators may not have backed up elsewhere, a 7- or 30-day recovery window is safer. Recommendation: consider `recovery_window_in_days = 7` for the new app secrets, or at minimum document the data-loss risk. This deviates from the existing convention, so flag it for the maintainer.
2. **Terraform state still contains the seed value.** Because `secret_string = var.registry_api_token`, the plaintext value still lands in Terraform state (and in tfvars). This is an improvement over the status quo (where it was in state AND in the ECS task definition AND in plan diffs), but it is not a full removal from state. A future step should seed the secret once (e.g. via a one-shot `terraform import` + `ignore_changes`) and then remove the value from tfvars. The LLD should call this out as a known limitation of Phase 1, not as "solved."
3. **Rolling deployment ordering.** When the `environment` entry is removed and the `secrets` entry is added in the same apply, ECS creates a new task definition revision. Running tasks are replaced. If the new task's exec role lacks `secretsmanager:GetSecretValue` on a new secret (IAM eventually-consistent propagation), the task fails to start with a startup error. Recommendation: apply IAM changes first, wait for propagation, then apply the task-definition changes; or use `depends_on` between the policy and the service. The LLD does not mention ordering. Add it.
4. **No drift detection for the duplicated resolver.** (Same as Byte's concern 2.)
5. **The metrics-service step is a "verify and maybe no-op."** That is honest, but the acceptance criteria say "every sensitive env var... is backed by a Secrets Manager secret." If the metrics-service has zero plaintext secrets, the criterion is vacuously true for it; make the testing.md explicit that metrics-service was audited and found clean (or list what was migrated).
6. **Cross-account access is mentioned as a benefit but not wired.** The LLD says cross-account is "kept open as an option." That is fine for this scope, but the issue acceptance criteria should not imply cross-account works today. (The issue text is careful here - good.)

### New libraries / infra dependencies
- None.

### Better alternatives considered
- Using `terraform import` to adopt externally-created secrets and avoid seeding from tfvars (removes value from state). Better for security but adds migration friction. Recommend as a Phase-2 follow-up, not now.

### Recommendations
- Add a deployment-ordering note (IAM before task def, or `depends_on`).
- Decide on `recovery_window_in_days` for new secrets and document the tradeoff.
- Explicitly state in the LLD that the seed value remains in Terraform state in Phase 1, with a follow-up to remove it.
- Make the metrics-service audit explicit in testing.md.

### Questions for author
- What is the desired recovery window for application secrets: 0 (match existing), 7, or 30?
- Is there a CI pipeline that runs `terraform plan` on PRs? If so, the plan-diff assertion in testing.md is automatable; if not, it is manual.

### Verdict
**APPROVED WITH CHANGES** - Deployment ordering, recovery-window decision, and the "value still in state" caveat must be addressed before implementation.

---

## Security Engineer - Cipher

**Focus:** AuthN/AuthZ, validation, OWASP, data protection.

### Strengths
- Moving plaintext secrets out of the ECS task definition and Terraform plan diffs is a clear security win. Task definitions are readable by anyone with `ecs:DescribeTaskDefinition`; Secrets Manager secrets are not.
- KMS encryption at rest with a dedicated key (`aws_kms_key.secrets`) and key rotation enabled (`enable_key_rotation = true`) is correct.
- The IAM policy uses an explicit ARN allowlist rather than `Resource: "*"`, which is the right posture (least-privilege-ish; see concern below).
- The resolver never logs secret values - only ARNs and counts. Good.
- The `MCP_SECRETS_RESOLVER_ENABLED` escape hatch is a safety control, not a security hole (it only disables fetches, never bypasses auth).

### Concerns
1. **Shared IAM policy = over-broad access.** `ecs_secrets_access` is attached to every mcp-gateway service's task role AND task-execution role. This means the registry task can read `FEDERATION_ENCRYPTION_KEY` even though only the auth-server and registry both use it - and the mcpgw task can read all of them even though mcpgw uses none of the migrated secrets. This is a lateral-movement risk: a compromise of the mcpgw container yields access to every application secret. The LLD acknowledges this (Alternative 3) and defers it. For a security-focused migration, this is the single most important follow-up. Recommendation: at minimum, do NOT add mcpgw to the new secret ARNs (mcpgw has `secrets = []` today and consumes none of these); better, split the policy per service in a follow-up. The LLD should explicitly state that mcpgw is excluded from the new ARNs even though it attaches `ecs_secrets_access`.
2. **No `secretsmanager:DescribeSecret`/`ResourceTag` conditions.** The policy grants only `GetSecretValue` (good), but does not constrain by `aws:ResourceTag`. Given the shared policy, a tag-based condition (`aws:ResourceTag/Component = ...`) would add defense in depth. Optional but recommended.
3. **Plaintext fallback weakens the guarantee.** The Q6 fallback means a plaintext env var, if present, overrides the Secrets Manager value. This is desired for migration, but it also means an attacker who can set an env var on a surface (e.g. a compromised CI runner) can inject a credential that the app trusts over Secrets Manager. Acceptable during migration; the follow-up to remove the fallback is security-critical. Recommendation: log which source was used per secret (counts are in the LLD; good), and prioritize the fallback-removal follow-up.
4. **`GITHUB_APP_PRIVATE_KEY` is a PEM.** Storing a multi-line PEM as a `SecretString` is fine, but the `.strip()` in the resolver (concern raised by Byte) and any JSON wrapping must preserve the exact bytes. Recommendation: do NOT JSON-wrap the PEM secret; store it as a raw `secret_string` and inject via a flat `valueFrom` (no `:jsonkey::`). The LLD does this correctly.
5. **`recovery_window_in_days = 0` is a data-availability risk, not a confidentiality risk.** From a security view, immediate deletion is fine (reduces lingering secret exposure); from an ops view it is risky (Circuit's concern). No security objection.
6. **No mention of secret rotation for the new secrets.** The issue is correctly scoped to exclude rotation, but Checkov `CKV2_AWS_57` will flag every new secret. The LLD correctly prescribes the `#checkov:skip` justification. Confirm the justification text is accurate per-secret (the IdP secrets say "managed in external dashboard"; the app secrets should say "rotation requires coordinated consumer update"). The LLD's example justification is correct.

### New libraries / infra dependencies
- None.

### Better alternatives considered
- Per-service IAM (Alternative 3). Strongly recommended as a fast-follow, not deferred indefinitely.

### Recommendations
- Explicitly exclude mcpgw from the new secret ARNs in the IAM policy, or split the policy.
- Add a tag-based `Condition` to the IAM policy for defense in depth (optional).
- Prioritize the fallback-removal follow-up; track it as a security issue, not just a cleanup.
- Confirm per-secret Checkov justifications are accurate.

### Questions for author
- Can mcpgw be removed from `ecs_secrets_access` entirely (it has `secrets = []`), or does it share the role for another reason? (Audit needed.)
- Is there a plan to rotate `REGISTRY_API_TOKEN` / `FEDERATION_ENCRYPTION_KEY`? Rotating the federation encryption key is non-trivial (it encrypts federated tokens); flag this as a rotation-design problem for the follow-up.

### Verdict
**APPROVED WITH CHANGES** - The mcpgw over-grant and the fallback-removal priority are the key items. No confidentiality regression vs. the status quo; this is a net improvement.

---

## SMTS (Overall) - Sage

**Focus:** Architecture, code quality, maintainability.

### Strengths
- The design is well-scoped to the task (Medium: Terraform + app loader, no Helm/EKS). It resists scope creep (rotation, SSM, Helm all explicitly excluded).
- It follows every relevant existing pattern in the codebase rather than inventing a new one - the strongest signal that an entry-level developer can implement and maintain it.
- The LLD is unusually concrete: exact file paths, line numbers, resource names, code sketches, and a deployment-surface checklist. An implementer can execute this without further design.
- The alternatives matrix is honest, including where the chosen approach is weaker (least-privilege IAM).
- No emojis, no em-dashes, follows the CLAUDE.md conventions (Pydantic, modern type hints, logging, modularity).

### Concerns
1. **The "value still in Terraform state" limitation (Circuit #2) is understated in the issue.** The issue's acceptance criterion says "no plaintext secret values in the diff," which the design meets, but a casual reader could conclude secrets are fully out of Terraform. They are out of the task definition and plan diff, but still in state and tfvars. Recommend the issue and LLD state this explicitly so the security win is not overstated.
2. **Two copies of the resolver (Byte #2, Circuit #4).** Acceptable for now, but the byte-identical test guardrail must land in the same PR or drift is inevitable.
3. **The `*_SECRET_ARN` variables add 12 new env vars to every service.** This is configuration-surface growth. The `MCP_SECRETS_RESOLVER_ENABLED` master switch mitigates it. Long-term, once the fallback is removed, all 12 `*_SECRET_ARN` vars and the resolver go away - make sure the follow-up issue captures that cleanup, not just "remove the fallback."
4. **Open questions are real, not filler.** The count-gating question and the "is `REGISTRATION_WEBHOOK_AUTH_HEADER` a credential" question need maintainer answers before implementation. Good that they are surfaced.
5. **Estimate (~580 LOC) is plausible** but the Terraform side could be larger if count-gating is applied broadly (each gated secret needs conditional logic in three places: resource, IAM, secrets block). The implementer should budget for that.

### New libraries / infra dependencies
- None. Justified.

### Better alternatives considered
- Shared resolver package (Alternative 4). Agreed to defer. Fine.

### Recommendations
- State the "still in state" limitation explicitly in both the issue and the LLD.
- Land the byte-identical resolver test in the same PR.
- Ensure the follow-up issue covers removing the `*_SECRET_ARN` vars and the resolver, not just the plaintext fallback.
- Resolve the open questions with the maintainer before implementation begins.

### Questions for author
- Who owns the follow-up to remove the fallback and the ARN vars? (Assign at issue-creation time.)
- Is there a documented runbook for rotating `FEDERATION_ENCRYPTION_KEY`? If not, the follow-up should include one.

### Verdict
**APPROVED WITH CHANGES** - Address the "still in state" wording, land the duplication guardrail, and scope the follow-up cleanup correctly. Architecture is sound.

---

## Review Summary

| Reviewer | Verdict | Blockers | Key Recommendations |
|----------|---------|----------|---------------------|
| Pixel (Frontend) | APPROVED | 0 | Note graceful degradation for GitHub-credential-dependent UI features |
| Byte (Backend) | APPROVED WITH CHANGES | 0 | Duplication guardrail; sync-in-async note; count-gate sync note |
| Circuit (SRE) | APPROVED WITH CHANGES | 0 | Deployment ordering; recovery-window decision; "still in state" caveat; metrics-service audit |
| Cipher (Security) | APPROVED WITH CHANGES | 0 | Exclude mcpgw from new ARNs; prioritize fallback-removal; per-secret Checkov justifications |
| Sage (SMTS) | APPROVED WITH CHANGES | 0 | "Still in state" wording; duplication test in same PR; scope follow-up cleanup |

**Overall: APPROVED WITH CHANGES.** No blockers. The design is implementable as written once the "WITH CHANGES" items are folded into the LLD/issue. The most important items, in priority order:

1. (Security) Explicitly exclude mcpgw from the new secret ARNs in `ecs_secrets_access`, or commit to a per-service IAM split as a fast-follow.
2. (SRE) Add deployment-ordering guidance (IAM before task definition; use `depends_on`).
3. (All) State explicitly that the seed value remains in Terraform state in Phase 1; do not overstate the security win.
4. (Backend) Land a byte-identical test for the duplicated resolver in the same PR.
5. (Security) Track the plaintext-fallback removal as a security-prioritized follow-up that also removes the `*_SECRET_ARN` vars and the resolver.

## Next Steps
- Fold the five priority items above into `lld.md` and `github-issue.md`.
- Resolve the three open questions with the maintainer (count-gating, `REGISTRATION_WEBHOOK_AUTH_HEADER` migration, resolver shared-package decision).
- Hand the package to an implementer; the design is ready for Phase 1 implementation after the edits.
