# GitHub Issue: Remove FAISS and standardize on DocumentDB native hybrid search

## Title
Remove FAISS dependency; route all search through DocumentDB native hybrid (text + vector) search

## Labels
- refactor
- enhancement
- tech-debt
- infra

## Description

### Problem Statement
The registry currently uses `faiss-cpu` for vector similarity search on the `file` storage backend. FAISS is an in-memory index that is rebuilt from scratch on every boot, requires native C++ libraries, and forces `numpy` version pinning that complicates deployments across CPU architectures and base images. The project already ships a complete, production-grade `DocumentDBSearchRepository` that performs native hybrid search (text + vector with Reciprocal Rank Fusion) on Amazon DocumentDB and any MongoDB-compatible backend. Because DocumentDB now handles vector search natively (HNSW `vectorSearch` with client-side cosine fallback for MongoDB CE), FAISS is a redundant second search engine that exists only to serve the `file` backend's in-process indexing path.

Maintaining two search engines means two code paths to test, two result-shape contracts to keep aligned, two sets of indexing entry points (the `faiss_service` singleton is called directly from `server_routes.py`, `agent_routes.py`, and `agent_batch_item_processor.py`, bypassing the `SearchRepositoryBase` abstraction that `search_routes.py` already uses correctly), and a native dependency that operators must install and keep compatible. End users see no benefit from FAISS: search functionality is identical whether served by FAISS or DocumentDB.

### Proposed Solution
Remove FAISS entirely and make DocumentDB hybrid search the sole search engine:

1. Delete the `FaissService` (`registry/search/service.py`) and the file-backend `FaissSearchRepository` (`registry/repositories/file/search_repository.py`).
2. Migrate every direct `faiss_service` call site in `server_routes.py`, `agent_routes.py`, and `agent_batch_item_processor.py` to the existing `get_search_repository()` factory abstraction, so all backends route indexing and search through `SearchRepositoryBase`.
3. Remove the `faiss-cpu` dependency from `pyproject.toml` and `uv.lock`; add `numpy` as an explicit dependency (it is currently only available transitively through `faiss-cpu` and `sentence-transformers`, yet `registry/embeddings/client.py` imports it directly).
4. Remove FAISS-specific configuration (`faiss_index_path`, `faiss_metadata_path`), the `FaissMetadata` schema, the in-memory re-index-on-boot path in `main.py`, the `faiss` module auto-mock in `tests/conftest.py`, the `mock_faiss.py` fixture, and the `test_faiss_service.py` unit suite.
5. Update Docker, Terraform/ECS, build scripts, CLI helpers, telemetry labels, the `mcpgw.json` tool description, and documentation to remove all FAISS references.
6. Decide the fate of the `file` storage backend's search capability: either the `file` backend routes search to DocumentDB (search becomes a DocumentDB-backed capability regardless of storage backend), or `file` is deprecated for search. This issue assumes search becomes DocumentDB-only; a follow-up tracks whether `STORAGE_BACKEND=file` remains a supported deployment mode at all.

### User Stories
- As an operator deploying the registry, I want to stop installing FAISS native libraries and pinning numpy to FAISS-compatible versions, so that container images build reliably across architectures and upgrades are painless.
- As an operator, I want search to be powered by DocumentDB native hybrid search, so that indexes persist across restarts and I do not pay a full re-index on every boot.
- As a developer, I want a single search code path, so that I do not have to keep two result-shape contracts and two indexing entry points in sync.
- As an end user of the gateway, I want search results to be unchanged, so that my natural-language tool discovery and tag-based lookups keep working exactly as before.

### Acceptance Criteria
- [ ] No source file under `registry/`, `cli/`, `api/`, or `metrics-service/` imports `faiss` or references the `faiss_service` singleton.
- [ ] `registry/search/service.py` (FaissService) and `registry/repositories/file/search_repository.py` (FaissSearchRepository) are deleted.
- [ ] All indexing call sites in `server_routes.py`, `agent_routes.py`, and `agent_batch_item_processor.py` use `get_search_repository()` instead of importing `faiss_service` directly.
- [ ] `faiss-cpu` is removed from `pyproject.toml` and `uv.lock`; `numpy` is added as an explicit dependency so `registry/embeddings/client.py` continues to import successfully.
- [ ] `faiss_index_path`, `faiss_metadata_path`, and the `FaissMetadata` Pydantic model are removed.
- [ ] The in-memory re-index-on-boot block in `registry/main.py` is removed or generalized so DocumentDB (which persists embeddings) does not re-index on every boot.
- [ ] `tests/conftest.py` no longer auto-mocks the `faiss` module; `tests/fixtures/mocks/mock_faiss.py` and `tests/unit/search/test_faiss_service.py` are deleted; all remaining FAISS references in tests are removed or rewritten against `SearchRepositoryBase`.
- [ ] `Dockerfile`, `docker-compose*.yml`, `build-config.yaml`, `build_and_run.sh`, `terraform/aws-ecs/**`, and `cli/service_mgmt.sh`/`cli/agent_mgmt.py` contain no FAISS references.
- [ ] Telemetry (`registry/core/telemetry.py`, `registry/metrics/client.py`, `metrics-service/**`) no longer reports a `faiss` search backend or `faiss_search_time_ms`; labels are generalized to a backend-agnostic `search_backend` / `search_time_ms`.
- [ ] `registry/servers/mcpgw.json` tool descriptions no longer mention FAISS.
- [ ] All `docs/` references to FAISS are updated to describe DocumentDB hybrid search.
- [ ] `grep -ri faiss` against the repository (excluding `.git/`) returns zero hits.
- [ ] Existing search behavior is preserved: natural-language search and tag-based search return equivalent results via DocumentDB, verified by the existing search integration and unit tests passing.

### Out of Scope
- Changing the DocumentDB hybrid search ranking algorithm, RRF constants, or result formatting (those are already production-grade and unchanged).
- Removing or rewriting the `sentence-transformers` embedding model integration (it remains the embedding source for DocumentDB indexing).
- Migrating the `file` storage backend's non-search repositories (servers, agents, scopes, etc.) to DocumentDB. Only the search repository is in scope; other file-backend repositories are untouched.
- Backfilling or migrating data out of existing on-disk `service_index.faiss` / `service_index_metadata.json` files (FAISS indices are in-memory and rebuilt every boot, so there is no persistent data to migrate).
- Performance tuning of the DocumentDB vector index (HNSW `m`, `efConstruction`, `efSearch`).

### Dependencies
- The `DocumentDBSearchRepository` must be confirmed working end-to-end (indexing + search + tag search + removal) before FAISS is deleted, per the user's constraint that search functionality must not break.
- An Amazon DocumentDB (or MongoDB-compatible) instance must be available in every deployment target, since search will no longer have an in-process fallback. This is a deployment prerequisite, not a code dependency.

### Related Issues
- #646 (historical FAISS search initialization fix) - context only; this issue supersedes the FAISS code path that fix touched.
