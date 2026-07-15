# SWE Skill Judge Results

Judge scores for the `mcp-gateway-registry` SWE Skill benchmark at tag `1.24.4`.

Each task x model cell is the average of four design artifacts: `github-issue.md`, `lld.md`, `review.md`, and `testing.md`. Each artifact was scored on Completeness, Correctness, Specificity, and Risk Awareness, 0-25 each, for a 0-100 artifact total.

These scores were produced by manual rubric evaluation in the current judge session. The full per-artifact JSON breakdowns were returned in chat; this file records the aggregate task x model results and synthesis.

## Score Matrix

All cells are 0-100. Bold marks the top score in each task row.

| Task | Opus 4.8 | GLM-5.2 | Kimi | Devstral 123B | MiniMax M2.5 | Qwen Coder Next | Qwen 3.6 35B | Task avg |
|------|---------:|--------:|-----:|--------------:|-------------:|----------------:|-------------:|---------:|
| `remove-faiss` | 88.00 | **89.75** | 66.50 | 69.25 | 66.50 | 69.00 | 80.25 | 75.61 |
| `remove-efs-from-terraform-aws-ecs` | 87.00 | **89.00** | 74.50 | 73.00 | 63.00 | 59.25 | 82.75 | 75.50 |
| `ssrf-hardening-outbound-url-validation` | 87.75 | **88.00** | 52.25 | 61.50 | 69.75 | 75.75 | 79.00 | 73.43 |
| `migrate-ecs-env-vars-to-secrets-manager` | **92.25** | 73.50 | 80.25 | 64.25 | 64.25 | 64.50 | 84.00 | 74.71 |
| `replace-keycloak-db-password-with-rds-iam` | **90.50** | 84.50 | 79.50 | 55.75 | 63.00 | 55.75 | 68.50 | 71.07 |

## Per-Model Leaderboard

| Rank | Model | Avg score | Notes |
|-----:|-------|----------:|-------|
| 1 | Claude Opus 4.8 | 89.10 | Best overall; strongest on the two high-difficulty AWS tasks. |
| 2 | GLM-5.2 | 84.95 | Very strong on bounded cleanup and SSRF; weaker on ECS Secrets due to app-side secret-loader drift. |
| 3 | Qwen 3.6 35B | 78.90 | Stronger than expected on EFS and ECS Secrets; still fragile on Keycloak IAM. |
| 4 | Kimi | 70.60 | Good on the AWS migration tasks, but SSRF and FAISS scores were much lower in this judging pass. |
| 5 | MiniMax M2.5 | 65.30 | Middle of the pack, with better SSRF instincts than infrastructure depth. |
| 6 | Qwen Coder Next | 64.85 | Good SSRF cell, but weak on AWS-specific design details. |
| 7 | Mistral Devstral 2 123B | 64.75 | Consistent but rarely best; Keycloak IAM was the largest failure mode. |

## Per-Task Averages

| Task | Avg | Difficulty signal |
|------|----:|-------------------|
| `replace-keycloak-db-password-with-rds-iam` | 71.07 | Hardest. Most models struggled with RDS IAM token lifecycle, Keycloak compatibility, RDS Proxy semantics, and static-password removal. |
| `ssrf-hardening-outbound-url-validation` | 73.43 | Security-hardening edge cases separated models sharply: DNS rebinding, redirects, metadata IPs, and private/link-local ranges were often incomplete. |
| `migrate-ecs-env-vars-to-secrets-manager` | 74.71 | ECS `secrets` is familiar, but models lost points on Terraform state leakage, IAM scope, and non-idiomatic app-side secret loading. |
| `remove-efs-from-terraform-aws-ecs` | 75.50 | Bounded Terraform cleanup, but good designs still needed to handle ECS mounts, scopes-init scripts, outputs, variables, and state/destroy risk. |
| `remove-faiss` | 75.61 | Bounded removal task. Strong designs preserved embeddings while removing FAISS-specific service/repository/dependency paths. |

## Synthesis

Opus remains the most reliable overall model, especially on the high-difficulty AWS tasks where correctness depends on service-specific mechanics rather than broad code inventory. GLM-5.2 was the surprise: it topped three medium tasks and landed close to Opus overall, but its ECS Secrets design mixed idiomatic ECS `secrets` usage with non-idiomatic app-side secret loading. Qwen 3.6 35B was also stronger than the earlier 5-column matrix implied, particularly on bounded Terraform and ECS Secrets work.

The difficulty ordering was not purely based on implementation size. Keycloak IAM was hardest because a superficially plausible design can still fail on token expiry, Keycloak driver support, TLS, RDS Proxy auth mode, or retained backend credentials. SSRF was next because security correctness depends on enumerating bypasses. The budget and mid-tier models broke down most often on AWS-specific semantics: ECS `valueFrom` rules, Terraform secret-state exposure, RDS IAM auth lifecycle, and whether a proposed rollout could actually run in the existing container image.
