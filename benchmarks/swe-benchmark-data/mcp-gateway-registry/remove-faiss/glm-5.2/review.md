# Expert Review: Remove FAISS, standardize on DocumentDB native hybrid search

*Reviewed artifact: `./lld.md`*
*Reviewer date: 2026-07-15*
*Reviewer model: glm-5.2*

This review evaluates the low-level design for removing FAISS and routing all search through the existing `DocumentDBSearchRepository`. Each reviewer assessed strengths, concerns, new dependencies, alternatives, and gave a verdict. Reviews are deliberately critical: the goal is to surface real implementation risk, not to ratify the design.

---

## Frontend Engineer - Pixel

**Focus:** UI/UX, components, state, API integration.

### Strengths
- The design correctly identifies that there is **no frontend change**: search results are produced by the backend and rendered by the existing frontend, which consumes the same grouped result dict (`{servers, tools, agents, skills, virtual_servers}`) regardless of engine. The `DocumentDBSearchRepository` already emits the same per-entry fields (`relevance_score`, `match_context`, `matching_tools`, `num_tools`, `is_enabled`) the FAISS path emitted.
- The `intelligent_tool_finder` tool description rewording (Step 12) is correctly scoped - it is a description-only change, not a schema change, so AI agents consuming the tool see no contract break.

### Concerns
- **Relevance-score distribution may shift visibly.** FAISS computed `relevance_score` from inner-product distance via `_distance_to_relevance`; DocumentDB computes it from RRF + normalization (`_normalize_scores`, floor 0.10). The numeric values and ordering can differ for the same query. If the frontend surfaces `relevance_score` to users (e.g., as a percentage or a sort badge), the numbers will change. The LLD claims "equivalent results" - that is true for *which* entities match, but not strictly for *scores*. Recommend the implementer capture before/after score samples for a few canonical queries and confirm the frontend does not render scores in a way that a delta would alarm users.
- **`matching_tools` extraction differs.** DocumentDB caps tool extraction via `_tool_extraction_limit` (soft cap 60% of `max_results`, min 3). FAISS had its own tool extraction (`_extract_matching_tools`). If the frontend lists matching tools per server, the count and selection of tools shown may change. Not a bug, but a UX delta to verify.

### New Libraries / Infra Dependencies
- None on the frontend.

### Better Alternatives Considered
- Add a short "behavioral deltas" subsection to the LLD listing the three places scores/tool-extraction can differ (relevance normalization, score floor, tool soft-cap), so QA knows where to look. The LLD currently says behavior is "preserved" without enumerating the score-path differences.

### Recommendations
- Verify the frontend does not display raw `relevance_score` numerics to end users; if it does, treat the score-scale change as a UX item.
- Add a before/after comparison fixture for 3-5 representative queries to the testing plan.

### Questions for Author
- Does the frontend render `relevance_score` anywhere, or is it used only for sorting?

### Verdict
**APPROVED WITH CHANGES** - Approve once the score/tool-extraction behavioral deltas are explicitly acknowledged in the LLD and the testing plan.

---

## Backend Engineer - Byte

**Focus:** API design, data models, business logic, performance.

### Strengths
- The method-signature mapping table (Step 3) is accurate and directly actionable. I verified each mapping against the source: `add_or_update_service` -> `index_server`, `add_or_update_entity(path, dict, "a2a_agent", enabled)` -> `index_agent(path, agent_card, enabled)`, `remove_service`/`remove_entity` -> `remove_entity`, `save_data()` -> deleted. This is correct.
- Correctly catches the **`AgentCard`-vs-dict** subtlety: `index_agent` takes an `AgentCard`, while `add_or_update_entity` took a dict and reconstructed the card internally. Flagging that call sites must pass the card object (not `card.model_dump()`) is a real correctness catch that would otherwise produce a `TypeError`.
- Correctly identifies that **skills and virtual servers already route through `search_repo`** (`skill_service.py:1350/1513/1560`, `virtual_server_service.py:582`, `main.py:539`), so the migration is limited to servers + agents. This keeps the blast radius honest.
- The `skip_if_unchanged` base-interface addition is the right call - it lets `main.py` request the DocumentDB startup optimization without `hasattr` checks, and there is no second implementation to maintain once the file repo is gone.

### Concerns
- **`registry/search/` directory removal is under-specified.** I checked: `registry/search/` contains only `service.py` and an empty `__init__.py`, and **nothing else imports the `registry.search` package** (`grep` for non-`faiss_service` importers returns empty). So after deleting `service.py`, the entire `registry/search/` directory should be removed, including `__init__.py`. The LLD lists this as Open Question #4 and hedges ("check first") - the check is already done; the design should state definitively: delete `registry/search/` wholesale.
- **`index_virtual_server` does not accept `skip_if_unchanged`.** The LLD's Step 1 says to add `skip_if_unchanged` to `index_server`/`index_agent`/`index_skill`/`index_virtual_server` "for symmetry", but `DocumentDBSearchRepository.index_virtual_server` does **not** currently have that parameter (only `index_server`, `index_agent`, `index_skill` do). Adding it to the base interface without updating the DocumentDB implementation would make the impl signature diverge from the ABC. The implementer must either add `skip_if_unchanged=False` to `DocumentDBSearchRepository.index_virtual_server` too, or leave `index_virtual_server` out of the base-interface change. Recommend the latter (don't touch virtual_server) since no startup path calls it with the flag.
- **The `save_data()` removal needs a second look.** `server_routes.py:3808` fires `asyncio.create_task(faiss_service.save_data())` after registration. For FAISS this flushed the in-memory index+metadata to disk. DocumentDB persists on every `replace_one`, so deleting the call is correct - but the design should state explicitly that there is **no durability regression** because DocumentDB writes are synchronous per-index operation. The LLD says "DocumentDB persists on write" in the mapping table but does not call out the durability argument; an implementer skimming might worry. Minor.
- **`get_search_repository()` always returning DocumentDB breaks the `file` backend's standalone operation.** This is the core backend concern and the LLD does surface it, but the factory change in Step 2 silently makes `STORAGE_BACKEND=file` deployments depend on a DocumentDB endpoint for *search* (not for other file repos). If `documentdb_host` is unset, `DocumentDBSearchRepository.initialize()` will fail at startup when it tries `get_documentdb_client()`. The design does not specify whether startup should degrade gracefully (search disabled with a warning) or hard-fail. Recommend hard-fail with a clear message: "search requires a DocumentDB/MongoDB endpoint; set STORAGE_BACKEND to a mongodb variant or configure DOCUMENTDB_HOST". The current `get_documentdb_client()` error may be opaque.
- **Lazy import path consistency.** The rewired call sites currently do `from ..search.service import faiss_service` inside each function. The replacement `from ..repositories.factory import get_search_repository` is a top-level-safe import (no circular dep), so the implementer could hoist it to module level for clarity - but the LLD preserves the in-function style, which is also fine. No blocker, just a style note.

### New Libraries / Infra Dependencies
- `numpy` promoted to explicit dependency - correct and necessary; I confirmed `registry/embeddings/client.py:16` does `import numpy as np`. Without this, `faiss-cpu` removal could drop numpy from the resolved env. Good catch.
- No other backend deps.

### Better Alternatives Considered
- For the `file`-backend-search problem, the LLD could add a **`search_enabled` / `search_disabled` graceful-degradation path** so a `file` deployment without DocumentDB boots (search returns empty with a startup warning) rather than crashing. This is a better operator experience than a hard startup failure and does not reintroduce FAISS. Worth adding as a recommended refinement.

### Recommendations
1. State definitively that `registry/search/` is deleted wholesale (directory + `__init__.py`).
2. Do **not** add `skip_if_unchanged` to `index_virtual_server` in the base interface (the DocumentDB impl does not have it); limit the base-interface change to `index_server`/`index_agent`/`index_skill`, and update those DocumentDB signatures are already present so no impl change is needed.
3. Specify startup behavior when DocumentDB is unreachable on a `file` backend: hard-fail with a clear message, or degrade gracefully. Pick one.
4. Add a one-line durability note next to the `save_data()` removal.

### Questions for Author
- Should `file`-backend deployments without DocumentDB hard-fail at startup or boot with search disabled?

### Verdict
**APPROVED WITH CHANGES** - Solid mapping and good catches on `AgentCard` and `numpy`. Resolve the `index_virtual_server` signature mismatch, the `registry/search/` directory deletion, and the file-backend startup-failure behavior before implementation.

---

## SRE / DevOps Engineer - Circuit

**Focus:** Deployment, monitoring, scaling, infrastructure.

### Strengths
- Correctly frames this as a **net simplification for operators**: no more native `faiss-cpu` C++ library, no numpy pinning to faiss-compatible ranges, no boot-time full re-index (O(N) re-embedding of every server on every restart). Container images get smaller and more portable across architectures.
- The deployment-surface checklist is thorough - it enumerates Dockerfile, all three docker-compose files, build-config, build_and_run.sh, Terraform ECS, CLI scripts, and the metrics-service. This is the right inventory.
- Correctly notes the **stateless-replica scaling win**: with FAISS gone, every registry replica shares the DocumentDB search index instead of maintaining a divergent in-memory copy. This is a genuine horizontal-scaling improvement.

### Concerns
- **The default `STORAGE_BACKEND=file` now implies a DocumentDB dependency for search, and there is no infrastructure guidance for this.** The LLD's rollout plan mentions it in a sentence, but SRE needs concrete guidance: every deployment that uses search must provision a DocumentDB cluster (or MongoDB-compatible instance) and set `DOCUMENTDB_HOST`/credentials/TLS. For local dev and CI, the conftest already uses `mongodb-ce` on localhost - good - but production `file`-backend deployments (if any exist) need a migration playbook. The design should link the DocumentDB provisioning steps (the repo already has `docs/faq/configuring-mongodb-atlas-backend.md` and DocumentDB TLS config in `config.py`).
- **Image-size claim in `OPERATIONS.md` will change.** Line 136 says the registry image is "~4.6GB" with "FAISS, ML models". Removing `faiss-cpu` shrinks the image (the native lib + its numpy constraint). The release note should publish the new image size; SRE should verify the image actually builds smaller and that no other layer pulls faiss transitively.
- **`build_and_run.sh` FAISS-index verification block removal (Step 11) is operationally visible.** That script currently checks for `service_index.faiss` existence and prints a banner. Removing it is correct, but operators who grep startup logs for "FAISS index created" as a readiness signal will lose that signal. The replacement readiness signal is "Initializing DocumentDB search service" + successful HNSW index creation. Update any dashboards/runbooks that keyed off the old log line.
- **Metrics schema migration is under-specified for production.** The LLD says "additive migration" for `faiss_search_time_ms` -> `search_time_ms` in SQLite, but does not specify the migration ordering or how the metrics-service handles a mixed-version fleet (old pods writing `faiss_search_time_ms`, new pods writing `search_time_ms`). Since metrics-service reads its own SQLite, this is per-instance, but the collector Lambda (`terraform/telemetry-collector/lambda/collector/schemas.py`) also references the field - that file is **not** in the LLD's modified-files list. The implementer must update the Lambda schema too, or the collector will drop/miss the renamed field.
- **No rollback plan.** The rollout plan has phases but no rollback procedure. If DocumentDB search regresses in production, the rollback is "revert the commit" - which re-adds FAISS. That is acceptable for a removal, but SRE should confirm the DocumentDB index (`mcp_embeddings_<dim>`) remains populated during the revert window (it will - embeddings persist), so a rollback does not lose search capability. Worth stating.
- **`uv.lock` regeneration must happen in the same PR.** If `pyproject.toml` drops `faiss-cpu` but `uv.lock` is not regenerated, the lockfile still pins `faiss-cpu` and the build still installs it. The LLD says "regenerate via `uv lock`" - good - but CI must enforce that the lockfile is in sync (`uv lock --check`).

### New Libraries / Infra Dependencies
- `numpy` explicit - fine, already transitively present, no new infra.
- DocumentDB/MongoDB becomes a **hard dependency for search** in every environment. This is the headline infra change.

### Better Alternatives Considered
- Ship behind a `SEARCH_BACKEND` env var (`documentdb` default, `none` for file-only-without-search) so operators can opt into the degraded mode explicitly rather than discovering a startup failure. This is more operator-friendly than an implicit hard-fail and aligns with Byte's graceful-degradation suggestion.

### Recommendations
1. Add a DocumentDB provisioning pointer to the rollout plan (link existing `docs/faq/configuring-mongodb-atlas-backend.md` and the DocumentDB TLS/IAM config).
2. Update `terraform/telemetry-collector/lambda/collector/schemas.py` - add it to the modified-files list and the metrics migration step.
3. Add CI enforcement: `uv lock --check` and a `grep -ri faiss` gate that fails the build if any reference remains.
4. Publish the new image size and update readiness/log-line runbooks.
5. Add a one-line rollback note: reverting re-enables FAISS; DocumentDB embeddings persist, so no data loss.

### Questions for Author
- Are there existing production `STORAGE_BACKEND=file` deployments that use search? If so, they need a coordinated DocumentDB provisioning migration before this ships.
- Does the telemetry collector Lambda need a coordinated deploy alongside the metrics-service rename?

### Verdict
**APPROVED WITH CHANGES** - Operationally sound and a net win, but the metrics Lambda schema omission, the missing DocumentDB provisioning guidance, the readiness-signal change, and CI lockfile/grep gates must be addressed before ship.

---

## Security Engineer - Cipher

**Focus:** AuthN/AuthZ, validation, OWASP, data protection.

### Strengths
- The change **reduces attack surface**: removing a native C++ library (`faiss-cpu`) removes a class of native-memory and supply-chain risk. Fewer native deps = fewer CVEs to track and a smaller SBOM.
- DocumentDB search already enforces the lifecycle status and enabled filters (`_build_status_filter`) consistently across servers, agents, skills, and virtual servers. The FAISS path's `search_by_tags` and `search_mixed` did their own filtering; consolidating on the DocumentDB path means filtering is enforced in one well-audited place. This is a security improvement.
- No new secrets, no new network ingress. DocumentDB connections already exist and are already TLS-configured (`documentdb_use_tls`, `documentdb_tls_ca_file`) with IAM auth support (`documentdb_use_iam`). Search reusing that connection introduces no new trust boundary.

### Concerns
- **`STORAGE_BACKEND=file` deployments gaining a DocumentDB connection is a new data path.** If a `file`-backend deployment previously had no database connectivity (intentionally air-gapped or isolated), this change silently introduces one for search. The design should require that operators explicitly opt into DocumentDB connectivity rather than have `get_search_repository()` open a connection to a default `localhost:27017` that may not exist or may point at an unintended instance. `documentdb_host` defaults to `localhost` - in a misconfigured `file` deployment, the registry could attempt to connect to localhost Mongo and fail, or worse, connect to an unintended local instance. Recommend failing fast at startup if `storage_backend == "file"` and no `DOCUMENTDB_HOST` is explicitly set, with a message telling the operator search requires DocumentDB.
- **Regex injection in lexical search is unchanged but now the only path.** `DocumentDBSearchRepository` builds `token_regex` from user query tokens via `re.escape` (good) and passes it to MongoDB `$regex`. This is existing behavior, not introduced by this change, but since FAISS removal makes DocumentDB the sole path, any latent ReDoS or `$regex` performance risk in the lexical stage becomes the only path. No action required beyond confirming `re.escape` is always applied before regex construction (it is, at `escaped_tokens = [re.escape(token) for token in query_tokens]`).
- **No new input validation needed**, but the implementer should confirm the rewired call sites pass the same `is_enabled` / `status` values the FAISS path did, so the status filter continues to exclude disabled/draft/deprecated entities correctly. A regression here would leak disabled assets into search. The existing search integration tests cover this; ensure they run against DocumentDB.
- **Telemetry field rename is a non-security change**, but confirm no PII is newly exposed by the `search_backend = "documentdb"` label. It is not - it is a static backend label.

### New Libraries / Infra Dependencies
- None. DocumentDB connectivity is pre-existing.

### Better Alternatives Considered
- Require explicit `SEARCH_ENABLED` / `DOCUMENTDB_HOST` opt-in for `file` backends rather than defaulting to `localhost:27017`. This prevents accidental connections to unintended local Mongo instances and makes the new data path intentional.

### Recommendations
1. Fail fast at startup when `storage_backend == "file"` and `DOCUMENTDB_HOST` is unset, rather than defaulting to localhost.
2. Confirm the disabled/draft/deprecated status filter regression tests run against the DocumentDB path (they should, since conftest sets `mongodb-ce`).
3. No supply-chain action beyond the standard `uv lock` review for the `numpy` pin.

### Questions for Author
- Are there air-gapped `file` deployments where introducing a DocumentDB connection (even to localhost) is a policy violation?

### Verdict
**APPROVED WITH CHANGES** - Net security positive (smaller surface, consolidated filtering). Address the `file`-backend default-localhost-connection concern with explicit opt-in/fail-fast before ship.

---

## SMTS (Overall) - Sage

**Focus:** Architecture, code quality, maintainability.

### Strengths
- The central architectural decision is correct and well-justified: FAISS is genuinely redundant given a complete `DocumentDBSearchRepository`, and removing it eliminates a native dependency, a dual code path, and a divergent indexing entry point. The design's insistence on routing everything through `get_search_repository()` (the abstraction `search_routes.py` already uses) is the right end state.
- The codebase analysis is unusually thorough and accurate: the method-mapping table, the `AgentCard`-vs-dict catch, the `numpy` hidden-dependency catch, and the observation that skills/virtual-servers already use the abstraction are all verified-correct and materially de-risk the implementation.
- Alternatives analysis is honest - it does not strawman the "keep FAISS" option and correctly rejects alternatives that reintroduce dual engines or expand blast radius.

### Concerns
- **The single biggest unresolved decision is the `file`-backend story, and the LLD leaves it as Open Question #1.** Three of four reviewers (Byte, Circuit, Cipher) independently flagged that making `file`-backend search depend on DocumentDB is a behavioral change with operational, infrastructure, and security dimensions. The design should not ship this as an open question - it should make a recommendation and specify the startup-failure / graceful-degradation behavior. My recommendation: introduce an explicit `SEARCH_BACKEND` setting (default `documentdb`) with a `disabled` option, so `file` deployments without DocumentDB can boot with search explicitly off. This resolves Byte's, Circuit's, and Cipher's concerns in one move and is a cleaner abstraction than an implicit `localhost` default.
- **The LLD's "equivalent results" claim is slightly overstated** (Pixel's point). The *set* of matching entities is equivalent; the *scores* and *tool extraction* differ. The design should say "equivalent matching, DocumentDB-quality ranking" and enumerate the deltas rather than implying bit-identical output. This matters for QA and for any downstream consumer persisting scores.
- **Test coverage gap for the `file` backend.** The conftest runs `STORAGE_BACKEND=mongodb-ce`, so the default suite never exercises the `file` search path today. After removal, the `file` path no longer exists for search, so that gap becomes moot - but it means there are **no existing tests proving the FAISS path and DocumentDB path returned equivalent results**. The "preserve behavior" acceptance criterion therefore cannot be verified by running old vs new tests; it must be verified by a manual before/after comparison on a representative query set. The LLD's testing strategy should call this out explicitly (Step 0 / Phase 0 does this implicitly; make it explicit).
- **Scope creep risk in the metrics rename.** Renaming `faiss_search_time_ms` -> `search_time_ms` touches the metrics-service SQLite schema, the collector Lambda schema, client code, tests, and docs. This is a cross-service change that could be a separate PR. The LLD bundles it in. Consider splitting: ship the FAISS removal first (telemetry just keeps writing `faiss_search_time_ms` as a now-misnamed-but-functional field), then rename in a follow-up. This shrinks the blast radius of the main change. Counter-argument: leaving a field named `faiss_search_time_ms` that measures DocumentDB time is confusing, so a same-PR rename is defensible - but then the Lambda schema (Circuit's find) must be in the same PR.
- **The `skip_if_unchanged` interface change has a subtle ABC-conformance bug** (Byte's find): `index_virtual_server` on DocumentDB lacks the parameter the LLD proposes adding to the base interface. This is a concrete defect in the design as written and must be fixed.
- **No feature flag, by design** - the LLD says "land behind no feature flag - the change is a clean removal." I agree a flag is not warranted for a removal, but given the `file`-backend behavioral change, a one-release deprecation notice in release notes (Phase 3 does mention this) is the minimum. Ensure the release note is prominent.

### New Libraries / Infra Dependencies
- `numpy` explicit (endorsed).
- DocumentDB as a hard search dependency (the headline change).

### Better Alternatives Considered
- The `SEARCH_BACKEND` explicit setting (described above) is the strongest refinement. It converts the implicit, surprising `file`-backend behavioral change into an explicit, operator-controlled one, and gives a clean graceful-degradation path. I recommend the author adopt it.

### Recommendations
1. **Resolve the `file`-backend decision in the LLD, not as an open question.** Recommend an explicit `SEARCH_BACKEND` setting (`documentdb` default, `disabled` allowed) and specify startup behavior for each.
2. Fix the `index_virtual_server` / `skip_if_unchanged` ABC-conformance defect (drop `index_virtual_server` from the base-interface change).
3. Soften "equivalent results" to "equivalent matching, DocumentDB ranking" and enumerate score/tool-extraction deltas.
4. Make the before/after query comparison an explicit, named testing step (not implicit in Phase 0).
5. Decide: bundle the metrics rename (and include the Lambda schema) or split it into a follow-up. Do not leave it half-done.
6. State definitively that `registry/search/` is deleted wholesale.

### Questions for Author
- Will you adopt an explicit `SEARCH_BACKEND` setting to make the `file`-backend change intentional?
- Bundle or split the metrics rename?

### Verdict
**NEEDS REVISION** - The architecture is sound and the analysis is strong, but the design ships with a concrete interface-conformance defect (`index_virtual_server`), an unresolved core decision (file-backend behavior) that three reviewers independently flagged, and an overstated equivalence claim. Resolve these and the design is implementable with confidence.

---

## Review Summary

| Reviewer | Verdict | Blockers | Key Recommendation |
|----------|---------|----------|--------------------|
| Frontend (Pixel) | APPROVED WITH CHANGES | 0 | Acknowledge score/tool-extraction behavioral deltas; add before/after query fixtures. |
| Backend (Byte) | APPROVED WITH CHANGES | 3 | Fix `index_virtual_server` ABC mismatch; delete `registry/search/` wholesale; specify file-backend startup behavior. |
| SRE (Circuit) | APPROVED WITH CHANGES | 4 | Add metrics Lambda schema to scope; DocumentDB provisioning guidance; CI `uv lock --check` + grep gate; readiness-signal runbook update. |
| Security (Cipher) | APPROVED WITH CHANGES | 1 | Fail-fast on `file` backend without explicit `DOCUMENTDB_HOST` (no silent localhost default). |
| SMTS (Sage) | NEEDS REVISION | 3 | Resolve file-backend decision in-design (recommend `SEARCH_BACKEND` setting); fix `index_virtual_server` defect; split or fully-scope metrics rename. |

### Next Steps
1. **Author revises LLD** to address the NEEDS REVISION blockers:
   - Adopt an explicit `SEARCH_BACKEND` setting (or equivalent) and specify `file`-backend startup behavior; remove Open Question #1.
   - Fix the `index_virtual_server` / `skip_if_unchanged` ABC-conformance defect.
   - Add `terraform/telemetry-collector/lambda/collector/schemas.py` to the modified-files list and the metrics migration step (or explicitly split the rename into a follow-up issue).
   - State that `registry/search/` is deleted wholesale.
   - Soften "equivalent results" and enumerate score/tool-extraction deltas.
2. **Author adds the testing-plan before/after query comparison** as an explicit step.
3. **Author adds CI gates** (`uv lock --check`, `grep -ri faiss` must be empty) to the rollout plan.
4. After revision, re-review by Sage; Byte/Circuit/Cipher can sign off on their specific items.
5. Only then proceed to implementation (out of scope for this skill).
