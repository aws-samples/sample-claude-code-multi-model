# Low-Level Design: Remove FAISS, standardize on DocumentDB native hybrid search

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
The registry ships two search engines:

1. **FAISS** (`registry/search/service.py`, `FaissService`) - an in-memory `faiss.IndexFlatIP` vector index used by the `file` storage backend. It is rebuilt from scratch on every boot, requires the native `faiss-cpu` C++ library, and pins `numpy` to a compatible range. The `faiss_service` module-level singleton is imported and called directly from `server_routes.py`, `agent_routes.py`, and `agent_batch_item_processor.py`, bypassing the `SearchRepositoryBase` abstraction.
2. **DocumentDB hybrid search** (`registry/repositories/documentdb/search_repository.py`, `DocumentDBSearchRepository`) - native `$search`/`vectorSearch` (HNSW, cosine) fused with lexical text boosting via Reciprocal Rank Fusion (RRF, k=60), with a client-side cosine fallback for MongoDB CE and a lexical-only fallback when the embedding model is unavailable. Embeddings persist in the `mcp_embeddings_<dim>` collection and survive restarts.

FAISS is redundant: DocumentDB already covers every search use case (semantic search, tag search, tag enumeration, server/agent/skill/virtual-server indexing, entity removal, re-indexing). FAISS exists only to give the `file` backend an in-process search path. Maintaining both engines doubles the testing surface, splits the indexing entry points, and forces a fragile native dependency on operators.

### Goals
- Delete FAISS (code, dependency, config, tests, docs, build/infra references).
- Make `DocumentDBSearchRepository` the sole search implementation, reached exclusively through `get_search_repository()`.
- Preserve externally observable search behavior: natural-language search and tag-based search return equivalent results.
- Remove the `faiss-cpu` native dependency and the boot-time full re-index.
- Keep `numpy` available (it is imported directly by `registry/embeddings/client.py`) by promoting it to an explicit dependency.

### Non-Goals
- Changing the DocumentDB ranking algorithm, RRF constants, lexical weights, or result formatting.
- Replacing `sentence-transformers` (it remains the embedding encoder for DocumentDB indexing).
- Migrating the `file` backend's non-search repositories (servers, agents, scopes, security scans, etc.) to DocumentDB.
- Tuning the HNSW index parameters (`m`, `efConstruction`, `efSearch`).
- Migrating any persisted FAISS data (FAISS indices are in-memory and rebuilt every boot; there is nothing to migrate).

## Codebase Analysis

### Key Files Reviewed

| File/Directory | Purpose | Relevance to This Change |
|----------------|---------|--------------------------|
| `registry/search/service.py` | `FaissService` and the `faiss_service` singleton (~1200 lines). In-memory IndexFlatIP, disk persistence to `service_index.faiss`/`service_index_metadata.json`, `add_or_update_service`/`add_or_update_agent`/`add_or_update_entity`/`remove_service`/`remove_entity`/`search_mixed`/`save_data`/`rebuild_index`/`initialize`. | Primary deletion target. |
| `registry/repositories/file/search_repository.py` | `FaissSearchRepository(SearchRepositoryBase)` - thin adapter that delegates to `faiss_service`. | Delete; its callers move to the factory. |
| `registry/repositories/documentdb/search_repository.py` | `DocumentDBSearchRepository` - full hybrid search. | The replacement; already implements the entire `SearchRepositoryBase` contract plus `find_missing_embeddings`/`reindex_paths`. |
| `registry/repositories/interfaces.py` | `SearchRepositoryBase` ABC defining `initialize`, `index_server`, `index_agent`, `index_skill`, `index_virtual_server`, `remove_entity`, `search`, `search_by_tags`, `get_all_tags`. | Add `skip_if_unchanged` to `index_server`/`index_agent`/`index_skill` (not `index_virtual_server`); otherwise unchanged. |
| `registry/repositories/factory.py` | `get_search_repository()` returns `DocumentDBSearchRepository` for MongoDB backends, else `FaissSearchRepository`. | Change `else` branch to always return `DocumentDBSearchRepository`. |
| `registry/api/server_routes.py` | 14 direct `faiss_service` call sites (register/update/disable/remove server, tool refresh, `save_data` fire-and-forget). | Migrate all to `get_search_repository()`. |
| `registry/api/agent_routes.py` | 4 direct `faiss_service` call sites (agent CRUD indexing/removal). | Migrate to `get_search_repository()`. |
| `registry/services/agent_batch_item_processor.py` | 2 direct `faiss_service` call sites (batch add/remove entity). | Migrate to `get_search_repository()`. |
| `registry/api/search_routes.py` | Already uses `get_search_repository()` correctly (`search_repo.search(...)`). | Reference pattern; no change beyond removing FAISS comments. |
| `registry/main.py` | Startup: `search_repo.initialize()`; for non-MongoDB backends, rebuilds the in-memory FAISS index by re-indexing every server. | Remove the re-index-on-boot block; DocumentDB persists embeddings. |
| `registry/core/config.py` | `storage_backend` (default `file`), `MONGODB_BACKENDS`, `ALLOWED_STORAGE_BACKENDS`, `faiss_index_path`/`faiss_metadata_path` properties, `vector_search_ef_search`, `search_fusion_method`, `embeddings_model_dimensions`. | Remove the two `faiss_*_path` properties. |
| `registry/core/schemas.py` | `FaissMetadata(BaseModel)` (id, text_for_embedding, full_server_info). | Delete; unused outside FaissService. |
| `registry/core/telemetry.py` | Heartbeat reports `search_backend = "documentdb" if ... else "faiss"`. | Generalize to a backend-agnostic label (always `documentdb`). |
| `registry/metrics/client.py` | `emit_tool_discovery_metric(..., faiss_search_time_ms=...)`. | Rename to `search_time_ms`. |
| `metrics-service/**` | `faiss_search_time_ms` column in SQLite schema, migrations, client, docs. | Rename column + field; see migration note. |
| `registry/embeddings/client.py` | `import numpy as np` (used for `np.array(..., dtype=np.float32)`). | Why `numpy` must become an explicit dependency. |
| `registry/servers/mcpgw.json` | `intelligent_tool_finder` tool description mentions FAISS. | Reword to "semantic/hybrid search". |
| `pyproject.toml` | `faiss-cpu>=1.7.4` in dependencies; `numpy` not listed explicitly. | Remove `faiss-cpu`; add `numpy`. |
| `uv.lock` | `faiss-cpu` package + two reverse-dependency edges. | Regenerate via `uv lock`. |
| `Dockerfile` / `docker-compose*.yml` / `build-config.yaml` / `build_and_run.sh` | Comments + `build_and_run.sh` FAISS index file lifecycle (create/verify/cleanup). | Remove references; drop the index-file verification block. |
| `terraform/aws-ecs/**` | `ecs-services.tf` comment, `service_mgmt.sh` `verify_faiss_metadata()`, `OPERATIONS.md` image-size line. | Remove FAISS mentions. |
| `cli/service_mgmt.sh` / `cli/agent_mgmt.py` | `verify_faiss_metadata()`; docstring mentioning FAISS search. | Remove/rewrite. |
| `tests/conftest.py` | Auto-injects `sys.modules["faiss"] = mock_faiss` at import time (lines ~147-149). | Remove the auto-mock. |
| `tests/fixtures/mocks/mock_faiss.py` | `MockFaissIndex` + `create_mock_faiss_module()`. | Delete. |
| `tests/unit/search/test_faiss_service.py` | 1131-line FaissService unit suite. | Delete; coverage transfers to DocumentDB search tests. |
| `scripts/migrate-file-to-mongodb.py` | Excludes `*.faiss` files from migration. | Remove the `.faiss` exclusion rule. |
| `docs/**` (20 files) | Architecture, configuration, database-design, embeddings, FAQ, testing, telemetry docs mention FAISS. | Rewrite to DocumentDB hybrid search. |

### Existing Patterns Identified
1. **Repository abstraction over direct service singletons**: `search_routes.py` already calls `get_search_repository()` and programs to `SearchRepositoryBase`. This is the target pattern for every indexing call site.
   - Files: `registry/api/search_routes.py`, `registry/repositories/factory.py`.
   - How a future implementer should follow this: replace `from ..search.service import faiss_service` + `await faiss_service.X(...)` with `from ..repositories.factory import get_search_repository` + `await get_search_repository().Y(...)`, using the method mapping in [Implementation Details](#implementation-details).

2. **Lazy imports to avoid circular dependencies**: Route files import `faiss_service` lazily inside function bodies (`from ..search.service import faiss_service`). The factory already handles DocumentDB imports lazily inside `get_search_repository()`; preserve this style.

3. **DocumentDB persists embeddings; FAISS does not**: `DocumentDBSearchRepository.index_server` supports `skip_if_unchanged=True` to avoid re-embedding on boot when the content hash matches. `FaissSearchRepository` has no such flag because it re-indexes everything every boot. The startup path should adopt `skip_if_unchanged=True` for DocumentDB.

4. **Graceful embedding degradation**: DocumentDB search falls back to lexical-only when the embedding model is unavailable (`_embedding_unavailable` latch). FAISS logs an error and silently drops the document. The DocumentDB behavior is strictly better and is the post-removal behavior for all backends.

### Integration Points

| Component | Integration Type | Details |
|-----------|------------------|---------|
| `get_search_repository()` | Modified | `else` (file) branch returns `DocumentDBSearchRepository` instead of `FaissSearchRepository`. |
| `server_routes.py` | Rewired | 14 `faiss_service` calls -> `search_repo.index_server` / `search_repo.remove_entity`; `faiss_service.save_data()` task removed. |
| `agent_routes.py` | Rewired | 4 `faiss_service.add_or_update_entity(path, dict, "a2a_agent", is_enabled)` -> `search_repo.index_agent(path, agent_card, is_enabled)`; `remove_entity` -> `search_repo.remove_entity`. |
| `agent_batch_item_processor.py` | Rewired | 2 calls; note `index_agent` takes an `AgentCard`, not a dict - pass the card object, not `card.model_dump()`. |
| `registry/main.py` startup | Simplified | Remove the `if settings.storage_backend not in MONGODB_BACKENDS:` re-index block; DocumentDB needs only `search_repo.initialize()`. Optionally call `find_missing_embeddings()`/`reindex_paths()` for self-healing. |
| `pyproject.toml` / `uv.lock` | Modified | Drop `faiss-cpu`; add `numpy` (explicit). |
| `metrics-service` schema | Modified | Rename `faiss_search_time_ms` -> `search_time_ms` (additive migration; see [Data Models](#data-models)). |
| Telemetry heartbeat | Modified | `search_backend` always `documentdb`. |

### Constraints and Limitations Discovered
- **`numpy` is a hidden direct dependency.** `registry/embeddings/client.py` does `import numpy as np`, but `numpy` is not listed in `pyproject.toml` - it arrives only transitively via `faiss-cpu` and `sentence-transformers`. Removing `faiss-cpu` without adding `numpy` explicitly will break `embeddings/client.py` at import time if the transitive resolution changes. **Mitigation: add `numpy` as an explicit dependency in the same change.**
- **The `file` storage backend is the default** (`storage_backend: str = Field(default="file")`). Once `FaissSearchRepository` is deleted, `get_search_repository()` must still return a working repository for `file` deployments. The design routes `file`-backend search to DocumentDB (search becomes a DocumentDB-backed capability). This means `STORAGE_BACKEND=file` deployments that use search now require a DocumentDB/MongoDB endpoint. This is the central behavioral trade-off and is called out in [Alternatives Considered](#alternatives-considered) and [Open Questions](#open-questions).
- **`skip_if_unchanged` is a DocumentDB-only parameter** not present on `SearchRepositoryBase.index_server`. Callers that want the startup optimization must either add it to the base interface (with a default `False` and a no-op file implementation - but the file repo is being deleted) or call it only after an `isinstance`/`hasattr` check. Recommended: add `skip_if_unchanged: bool = False` to the base `index_server`/`index_agent`/`index_skill` signatures so the startup path can use it uniformly; DocumentDB honors it, and since the file search repo is gone there is no second implementation to maintain.
- **`index_agent` expects an `AgentCard`, not a dict.** The current `faiss_service.add_or_update_entity` accepted a dict and reconstructed the `AgentCard` internally. Migrating call sites must pass the `AgentCard` object directly.
- **`metrics-service` stores `faiss_search_time_ms` in SQLite.** Renaming the column is a schema change; existing telemetry databases must be migrated (additive: add `search_time_ms`, backfill from `faiss_search_time_ms`, then drop the old column in a later release).

## Architecture

### System Context Diagram (after removal)

```
                      +-----------------------------+
                      |        registry API         |
                      |  (FastAPI routes)           |
                      |  search_routes.py           |
                      |  server_routes.py           |
                      |  agent_routes.py            |
                      +--------------+--------------+
                                     |
                                     | get_search_repository()
                                     v
                      +-----------------------------+
                      |   SearchRepositoryBase      |  (ABC, interfaces.py)
                      |   index_server / index_agent|
                      |   index_skill / index_vs    |
                      |   search / search_by_tags   |
                      |   get_all_tags / remove     |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      | DocumentDBSearchRepository  |  (sole impl)
                      | - HNSW vectorSearch (cosine)|
                      | - lexical text_boost        |
                      | - RRF fusion (k=60)         |
                      | - client-side cosine fallback (MongoDB CE)
                      | - lexical-only fallback (no embeddings)
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |  Amazon DocumentDB / Mongo  |
                      |  mcp_embeddings_<dim>       |
                      |  (embeddings persist)       |
                      +-----------------------------+

   [DELETED] registry/search/service.py (FaissService)
   [DELETED] registry/repositories/file/search_repository.py
   [DELETED] tests/fixtures/mocks/mock_faiss.py
```

### Sequence Diagram: server registration + indexing (after removal)

```
Client                server_routes.register_server        get_search_repository()      DocumentDBSearchRepository        DocumentDB
  |   POST /servers          |                                     |                              |                            |
  |------------------------->|                                     |                              |                            |
  |                          | persist server (server_repository)  |                              |                            |
  |                          |------------------------------------>|                              |                            |
  |                          | get_search_repository()             |                              |                            |
  |                          |------------------------------------>| DocumentDBSearchRepository() |                            |
  |                          | search_repo.index_server(path, info, is_enabled)                  |                            |
  |                          |------------------------------------------------------------------>|                            |
  |                          |                                     |                              | embed text (sentence-transformers)
  |                          |                                     |                              | replace_one({_id:path}, doc, upsert=True)
  |                          |                                     |                              |--------------------------->|
  |                          |                                     |                              |<---------------------------|
  |                          |<------------------------------------------------------------------|                            |
  |<-------------------------| (201 Created)                       |                              |                            |
```

### Component Diagram

```
registry/
  api/
    search_routes.py   -- get_search_repository() -->  repositories/factory.py
    server_routes.py   -- get_search_repository() -->  repositories/factory.py   (rewired; was faiss_service)
    agent_routes.py    -- get_search_repository() -->  repositories/factory.py   (rewired; was faiss_service)
  services/
    agent_batch_item_processor.py -- get_search_repository() --> repositories/factory.py  (rewired)
  repositories/
    factory.py         -- get_search_repository() --> DocumentDBSearchRepository  (always)
    documentdb/search_repository.py  (sole SearchRepositoryBase impl)
    interfaces.py      (SearchRepositoryBase; add skip_if_unchanged)
  core/
    config.py          (remove faiss_index_path / faiss_metadata_path)
    schemas.py         (remove FaissMetadata)
    telemetry.py       (search_backend = "documentdb")
  metrics/client.py    (search_time_ms)
  main.py              (remove re-index-on-boot block)
```

## Data Models

### New Models
None.

### Model Changes
- **Delete `registry/core/schemas.py::FaissMetadata`** (and its `ServerInfo` companion import if now unused). `FaissMetadata` is referenced only inside `FaissService`; removing the service removes the only consumer.

### DocumentDB Document Shape (unchanged, for reference)
The `mcp_embeddings_<dim>` document shape produced by `DocumentDBSearchRepository.index_server` is unchanged:

```python
doc = {
    "_id": path,
    "entity_type": "mcp_server",
    "path": path,
    "name": ...,
    "description": ...,
    "tags": [...],
    "metadata_text": ...,
    "is_enabled": is_enabled,
    "status": "active",
    "text_for_embedding": ...,
    "content_hash": <sha256[:16]>,
    "embedding": [...],
    "embedding_metadata": {...},
    "tools": [...],
    "metadata": server_info,
    "indexed_at": ...,
}
```

### Metrics Schema Change (metrics-service SQLite)
- `metrics-service/app/storage/database.py` and `migrations.py` define `faiss_search_time_ms REAL`.
- Rename to `search_time_ms REAL`. Because SQLite has limited `ALTER TABLE` support, ship this as an **additive migration**: add the new column, keep the old column for one release (read from old if new is null), then drop the old column in a subsequent release. The metrics client field `faiss_search_time_ms` becomes `search_time_ms`.

## API / CLI Design

No new HTTP endpoints or CLI commands are introduced. Existing endpoints and their externally observable contracts are preserved.

### Modified Behavior (no API shape change)
**`POST /api/search/semantic` (and the `intelligent_tool_finder` tool):**
- **Before:** routed to `FaissSearchRepository` (file backend) or `DocumentDBSearchRepository` (MongoDB backend).
- **After:** always routes to `DocumentDBSearchRepository`.
- **Request/Response shape:** unchanged. The grouped result dict (`{servers, tools, agents, skills, virtual_servers}`) and per-entry fields are identical - `DocumentDBSearchRepository` already produces the same fields the FAISS path produced (`relevance_score`, `match_context`, `matching_tools`, `num_tools`, `is_enabled`, etc.).

**Server/agent CRUD endpoints (`POST /servers`, `PATCH /servers/{path}`, `DELETE /servers/{path}`, agent equivalents):**
- **Before:** called `faiss_service.add_or_update_service` / `remove_service` / `add_or_update_entity` / `remove_entity` directly.
- **After:** call `get_search_repository().index_server` / `index_agent` / `remove_entity`. The HTTP response shapes are unchanged; only the indexing backend changes.

### Error Cases (unchanged)
- Search still returns `{"servers": [], "tools": [], ...}` (empty groups) on backend failure rather than a 500, matching current DocumentDB behavior.
- Indexing failures are logged and non-fatal (the CRUD operation still succeeds), matching current `FaissService` behavior.

## Configuration Parameters

### Removed Environment Variables / Settings

| Variable Name | Type | Default | Removed Because |
|---------------|------|---------|-----------------|
| `faiss_index_path` (computed property) | `Path` | `<servers_dir>/service_index.faiss` | FAISS deleted; no on-disk index. |
| `faiss_metadata_path` (computed property) | `Path` | `<servers_dir>/service_index_metadata.json` | FAISS deleted. |

These are derived `@property` methods on `Settings`, not env vars, so no `.env.example` keys are removed. Search the env example for `FAISS`/`faiss` references and remove any.

### Settings / Config Class Updates
In `registry/core/config.py`, delete:
```python
@property
def faiss_index_path(self) -> Path:
    return self.servers_dir / "service_index.faiss"

@property
def faiss_metadata_path(self) -> Path:
    return self.servers_dir / "service_index_metadata.json"
```

No new settings are required. `vector_search_ef_search` (default 100) and `search_fusion_method` (default `rrf`) remain and now apply to all search.

### Deployment Surface Checklist
- [ ] `pyproject.toml` - remove `faiss-cpu>=1.7.4`; add `numpy` (pin to a range compatible with `sentence-transformers`, e.g. `numpy>=1.26`).
- [ ] `uv.lock` - regenerate with `uv lock` after editing `pyproject.toml`.
- [ ] `.env.example` - remove any `FAISS_*` keys (grep first; the index path is computed, so likely none).
- [ ] `Dockerfile` - already has no FAISS install line (confirmed: `grep faiss Dockerfile` is empty); verify no build arg references FAISS.
- [ ] `docker-compose.yml`, `docker-compose.prebuilt.yml`, `docker-compose.podman.yml` - remove the "includes ... FAISS ..." comment lines.
- [ ] `build-config.yaml` - reword the service description that says "with nginx, FAISS, models".
- [ ] `build_and_run.sh` - remove the FAISS index-file existence/verify/cleanup block (lines ~242-278 and ~620-634).
- [ ] `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` - remove the "FAISS" comment on the registry service.
- [ ] `terraform/aws-ecs/scripts/service_mgmt.sh` and `cli/service_mgmt.sh` - remove `verify_faiss_metadata()` and its two call sites.
- [ ] `terraform/aws-ecs/OPERATIONS.md` - update the image-size/description row that mentions FAISS.
- [ ] `cli/agent_mgmt.py` - update the docstring that says search uses FAISS.
- [ ] `metrics-service` - schema migration for `faiss_search_time_ms` -> `search_time_ms`.

## New Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `numpy` | `>=1.26` | Promoted from transitive to explicit. `registry/embeddings/client.py` imports it directly (`np.array(..., dtype=np.float32)`). Without this, removing `faiss-cpu` could remove the only thing pulling `numpy` into the resolved environment. |

### Removed Dependencies

| Package | Reason |
|---------|--------|
| `faiss-cpu>=1.7.4` | FAISS engine deleted; native C++ dependency no longer needed. |

No other new dependencies are required. The change uses only existing dependencies (`motor`, `pymongo`, `sentence-transformers`, `numpy`, `pydantic`).

## Implementation Details

### Step-by-Step Plan (for a future implementer)

#### Step 0: Confirm DocumentDB search works (prerequisite, per user constraint)
Before deleting anything, run the existing search integration tests against a DocumentDB/MongoDB backend and confirm `index_server`, `index_agent`, `search`, `search_by_tags`, `get_all_tags`, and `remove_entity` all pass. Do not proceed to Step 1 until this is green.

#### Step 1: Add `skip_if_unchanged` to the base interface
**File:** `registry/repositories/interfaces.py`
**Lines:** ~1010-1050 (`index_server`, `index_agent`, `index_skill`)

Add `skip_if_unchanged: bool = False` to the `index_server`, `index_agent`, and `index_skill` abstract method signatures so the startup path can request the no-re-embed optimization without `hasattr` checks. DocumentDB already accepts these three; with the file repo deleted there is no second implementation to update.

> Do **not** add `skip_if_unchanged` to `index_virtual_server`: the `DocumentDBSearchRepository.index_virtual_server` implementation does not currently accept that parameter, so adding it to the ABC would make the implementation's signature diverge from the base class. No startup path calls `index_virtual_server` with the flag, so leave it untouched.

```python
@abstractmethod
async def index_server(
    self,
    path: str,
    server_info: dict[str, Any],
    is_enabled: bool = False,
    skip_if_unchanged: bool = False,
) -> None:
    """Index a server for search.

    Args:
        skip_if_unchanged: When True, implementations may skip embedding
            generation if the indexed content is unchanged. Default False.
    """
    pass
```

#### Step 2: Rewire `get_search_repository()` to always return DocumentDB
**File:** `registry/repositories/factory.py`
**Lines:** ~136-153 (`get_search_repository`)

```python
def get_search_repository() -> SearchRepositoryBase:
    """Get search repository singleton.

    Search is DocumentDB-backed for all storage backends. FAISS has been
    removed; the file backend's non-search repositories are unaffected.
    """
    global _search_repo
    if _search_repo is not None:
        return _search_repo
    from .documentdb.search_repository import DocumentDBSearchRepository
    _search_repo = DocumentDBSearchRepository()
    logger.info("Creating search repository: DocumentDB hybrid search")
    return _search_repo
```

#### Step 3: Migrate `server_routes.py` call sites
**File:** `registry/api/server_routes.py`
**Lines:** 774, 847, 1113, 1343, 1413, 1626-1643, 1716-1752, 1769-1827, 1876-1949, 2100-2386, 2502-2630, 2675-2760, 3504-3808, 3989-4041, 4100-4178

For each function, replace:
```python
from ..search.service import faiss_service
...
await faiss_service.add_or_update_service(service_path, server_info, is_enabled)
await faiss_service.remove_service(service_path)
asyncio.create_task(faiss_service.save_data())
```
with:
```python
from ..repositories.factory import get_search_repository
...
search_repo = get_search_repository()
await search_repo.index_server(service_path, server_info, is_enabled)
await search_repo.remove_entity(service_path)
# faiss_service.save_data() removed - DocumentDB persists on write
```

Use the exact mapping table:

| Old call | New call |
|----------|----------|
| `faiss_service.add_or_update_service(path, info, enabled)` | `get_search_repository().index_server(path, info, enabled)` |
| `faiss_service.add_or_update_entity(path, dict, "a2a_agent", enabled)` | `get_search_repository().index_agent(path, agent_card, enabled)` |
| `faiss_service.remove_service(path)` | `get_search_repository().remove_entity(path)` |
| `faiss_service.remove_entity(path)` | `get_search_repository().remove_entity(path)` |
| `faiss_service.save_data()` | (delete the `asyncio.create_task(...)` line) |
| `faiss_service.rebuild_index()` | `get_search_repository().reindex_paths(...)` or remove |

#### Step 4: Migrate `agent_routes.py` call sites
**File:** `registry/api/agent_routes.py`
**Lines:** 628-631, 1150-1152, 1598-1601, 1853-1855

Same mapping as Step 3. **Important:** `index_agent` takes an `AgentCard`, so where the old code passed `card.model_dump()` to `add_or_update_entity`, pass the `AgentCard` object directly to `index_agent`.

```python
# Before
from ..search.service import faiss_service
await faiss_service.add_or_update_entity(path, card.model_dump(), "a2a_agent", is_enabled)

# After
from ..repositories.factory import get_search_repository
await get_search_repository().index_agent(path, card, is_enabled)
```

#### Step 5: Migrate `agent_batch_item_processor.py` call sites
**File:** `registry/services/agent_batch_item_processor.py`
**Lines:** 225-228, 338-340

Same mapping as Step 4. Pass the `AgentCard` object, not `card.model_dump()`.

#### Step 6: Simplify `registry/main.py` startup
**File:** `registry/main.py`
**Lines:** ~494-540

Remove the backend-conditional re-index block. DocumentDB persists embeddings, so startup only needs `initialize()` (which creates the HNSW index if absent). Optionally add a self-healing step that re-indexes anything missing:

```python
search_repo = get_search_repository()
logger.info("Initializing DocumentDB search service...")
await search_repo.initialize()

# DocumentDB persists embeddings across restarts - no full re-index needed.
# Optional self-heal: re-index any source documents missing an embedding.
# (Left as an opt-in follow-up; not required for parity.)
```

Delete the `backend_name = "DocumentDB" if ... else "FAISS"` line and the `if settings.storage_backend not in MONGODB_BACKENDS:` re-index loop. If the agent-loading block was nested inside that `if`, lift it out so agents still load on every backend.

#### Step 7: Delete FAISS source files
```bash
rm registry/search/service.py
rm registry/search/__init__.py
rmdir registry/search
rm registry/repositories/file/search_repository.py
```
`registry/search/` contains only `service.py` and an empty `__init__.py`, and **nothing else imports the `registry.search` package** (verified: `grep -rn "from registry.search\|from ..search\|from ...search" registry/` returns only the `faiss_service` imports being removed in Steps 3-5). Delete the directory wholesale - do not leave a dangling empty package.

#### Step 8: Remove FAISS config and schema
**File:** `registry/core/config.py` - delete `faiss_index_path` and `faiss_metadata_path` properties (lines ~996-1002).
**File:** `registry/core/schemas.py` - delete `FaissMetadata` class (lines ~505-510); verify no remaining importers.

#### Step 9: Update telemetry and metrics
**File:** `registry/core/telemetry.py` (line ~731):
```python
# Before
search_backend = "documentdb" if settings.storage_backend in MONGODB_BACKENDS else "faiss"
# After
search_backend = "documentdb"
```
**File:** `registry/metrics/client.py` (line ~111): rename parameter `faiss_search_time_ms` -> `search_time_ms` and the metadata key it writes.
**File:** `metrics-service/metrics_client.py`, `metrics-service/app/storage/database.py`, `metrics-service/app/storage/migrations.py`, `metrics-service/tests/test_database.py`, `metrics-service/docs/*`: rename `faiss_search_time_ms` -> `search_time_ms` with the additive SQLite migration described in [Data Models](#data-models).

#### Step 10: Update dependencies
**File:** `pyproject.toml` (line 23):
```toml
# Remove
"faiss-cpu>=1.7.4",
# Add (in the dependencies list)
"numpy>=1.26",
```
Then run `uv lock` to regenerate `uv.lock` (removes the `faiss-cpu` package and its two reverse-dependency edges at lines 1608 and 1710).

#### Step 11: Update build, Docker, Terraform, CLI
- `build_and_run.sh`: delete the FAISS index-file block (lines ~242-278, ~620-634).
- `build-config.yaml`: reword lines 25 and 30.
- `docker-compose*.yml`: remove the "FAISS" comment lines (71, 14, 4).
- `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf`: remove comment at line 587.
- `terraform/aws-ecs/scripts/service_mgmt.sh` and `cli/service_mgmt.sh`: delete `verify_faiss_metadata()` and its callers (lines ~166-186, ~613, ~705 in cli; ~184-203, ~631, ~723 in terraform).
- `terraform/aws-ecs/OPERATIONS.md`: update line 136.
- `cli/agent_mgmt.py`: update docstring at line 34.
- `scripts/migrate-file-to-mongodb.py`: remove the `.faiss` exclusion at line 220.

#### Step 12: Update the tool description and docs
- `registry/servers/mcpgw.json`: reword `intelligent_tool_finder` description/args/raises (lines ~197, 199, 226) from "FAISS search" to "hybrid search".
- `docs/**` (20 files): replace FAISS references with DocumentDB hybrid search descriptions. Focus on `docs/database-design.md`, `docs/embeddings.md`, `docs/configuration.md`, `docs/design/storage-architecture-mongodb-documentdb.md`, `docs/testing/*`.
- `registry/embeddings/README.md`: remove the "Integration with FAISS Service" section (lines ~252-260).
- `release-notes/`: add a new release note documenting the removal (do not edit the historical v1.0.17 note).

#### Step 13: Update tests
- Delete `tests/fixtures/mocks/mock_faiss.py` and `tests/unit/search/test_faiss_service.py`.
- `tests/conftest.py`: remove the `faiss` auto-mock (lines ~147-149) and the `create_mock_faiss_module` import (line ~51).
- Audit every other test file that references FAISS (see File Changes) and either delete the FAISS-specific assertions or rewrite them against `SearchRepositoryBase` / `DocumentDBSearchRepository` using the existing test patterns in `tests/unit/api/test_search_routes.py` and `tests/integration/test_search_integration.py`.

### Error Handling
- Indexing failures remain non-fatal: wrap each `search_repo.index_*` / `remove_entity` call in the same try/except the route already uses, log at `error` with `exc_info=True`, and let the CRUD operation succeed. This matches the current `FaissService` behavior (it logged and returned).
- Search failures return empty result groups, not 500s, matching current `DocumentDBSearchRepository.search` exception handling.
- If `numpy` is somehow absent at runtime, `registry/embeddings/client.py` fails fast at import - the explicit `numpy` dependency in Step 10 prevents this.

### Logging
- Replace log messages that say "FAISS" with "DocumentDB search" / "search index" at the same levels (`info` for indexing milestones, `warning` for degraded fallbacks, `error` for failures with `exc_info=True`).
- Keep the existing DocumentDB search logging (per-type candidate counts, RRF scores, keyword merge counts) unchanged.

## Observability

### Tracing / Metrics / Logging Points
- **Heartbeat telemetry** (`registry/core/telemetry.py`): `search_backend` now always reports `documentdb`. No new metric.
- **Tool discovery metric** (`registry/metrics/client.py`): `search_time_ms` (renamed from `faiss_search_time_ms`) records the search-engine elapsed time, recorded in the metrics-service SQLite store.
- **Indexing logs**: every `index_server`/`index_agent` emits `logger.info(f"Indexed server '...' for search")` (already present in DocumentDB repo). No change.
- **Search fallback logs**: `_lexical_only_search` and `_client_side_search` already emit `logger.warning` when vector search is unsupported or embeddings are unavailable. These become the only fallback paths.

## Scaling Considerations
- **Current load assumptions**: FAISS was in-memory and rebuilt on every boot - an O(N) re-embedding of every server on startup. Removing it eliminates that boot-time spike. DocumentDB search is O(log N) per query via HNSW and persists embeddings, so restarts are cheap.
- **Horizontal scaling**: with FAISS gone, every registry replica shares the same DocumentDB search index (no per-replica in-memory index to diverge). This is a scaling improvement: replicas are now stateless with respect to search.
- **Bottlenecks**: search latency now depends on DocumentDB `$search`/`vectorSearch` performance. The existing `vector_search_ef_search` (default 100) and per-type `k_per_type = max(max_results * 2, 30)` candidate limits bound the work. Tuning these is out of scope.
- **Caching strategy**: unchanged. No new caching is introduced.

## File Changes

### New Files

| File Path | Description |
|-----------|-------------|
| `release-notes/v<next>.md` | Release note documenting FAISS removal and the `file`-backend-now-needs-DocumentDB-for-search behavioral change. |

### Deleted Files

| File Path | Description |
|-----------|-------------|
| `registry/search/service.py` | `FaissService` + `faiss_service` singleton (entire file). |
| `registry/repositories/file/search_repository.py` | `FaissSearchRepository` (entire file). |
| `tests/fixtures/mocks/mock_faiss.py` | FAISS test mock. |
| `tests/unit/search/test_faiss_service.py` | FAISS unit test suite (1131 lines). |

### Modified Files

| File Path | Lines | Change Description |
|-----------|-------|--------------------|
| `registry/repositories/factory.py` | ~136-153 | `get_search_repository()` always returns `DocumentDBSearchRepository`. |
| `registry/repositories/interfaces.py` | ~1010-1070 | Add `skip_if_unchanged: bool = False` to `index_server`/`index_agent`/`index_skill` (not `index_virtual_server`). |
| `registry/api/server_routes.py` | 14 call sites | Replace `faiss_service` with `get_search_repository()`; remove `save_data()` task. |
| `registry/api/agent_routes.py` | 4 call sites | Replace `faiss_service.add_or_update_entity` with `index_agent` (pass `AgentCard`). |
| `registry/services/agent_batch_item_processor.py` | 2 call sites | Same as agent_routes. |
| `registry/api/search_routes.py` | ~385, 440, 510 | Remove FAISS comments; behavior unchanged. |
| `registry/main.py` | ~494-540 | Remove re-index-on-boot block; keep `search_repo.initialize()`. |
| `registry/core/config.py` | ~996-1002 | Delete `faiss_index_path`/`faiss_metadata_path` properties. |
| `registry/core/schemas.py` | ~505-510 | Delete `FaissMetadata`. |
| `registry/core/telemetry.py` | ~731 | `search_backend = "documentdb"`. |
| `registry/metrics/client.py` | ~111, 126 | Rename `faiss_search_time_ms` -> `search_time_ms`. |
| `metrics-service/metrics_client.py` | ~193, 208 | Rename field. |
| `metrics-service/app/storage/database.py` | ~172, 306, 319 | Rename column + insert. |
| `metrics-service/app/storage/migrations.py` | ~120 | Additive migration: add `search_time_ms`, backfill, defer drop. |
| `metrics-service/tests/test_database.py` | ~142 | Update test metadata key. |
| `metrics-service/docs/*.md` | ~207, 440, 666 | Rename field in docs. |
| `pyproject.toml` | 23 | Remove `faiss-cpu`; add `numpy>=1.26`. |
| `uv.lock` | 693, 1608, 1710 | Regenerate via `uv lock`. |
| `build_and_run.sh` | ~242-278, ~620-634 | Remove FAISS index-file lifecycle block. |
| `build-config.yaml` | 25, 30 | Reword service description. |
| `docker-compose.yml` | 71 | Remove FAISS comment. |
| `docker-compose.prebuilt.yml` | 14 | Remove FAISS comment. |
| `docker-compose.podman.yml` | 4 | Remove FAISS comment. |
| `terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf` | 587 | Remove FAISS comment. |
| `terraform/aws-ecs/scripts/service_mgmt.sh` | ~184-203, 631, 723 | Remove `verify_faiss_metadata`. |
| `terraform/aws-ecs/OPERATIONS.md` | 136 | Update image description. |
| `cli/service_mgmt.sh` | ~166-186, 613, 705 | Remove `verify_faiss_metadata`. |
| `cli/agent_mgmt.py` | 34 | Update docstring. |
| `scripts/migrate-file-to-mongodb.py` | 220 | Remove `.faiss` exclusion. |
| `registry/servers/mcpgw.json` | 197, 199, 226 | Reword tool description. |
| `registry/embeddings/README.md` | ~13, 252-260 | Remove FAISS integration section. |
| `tests/conftest.py` | 51, 147-149 | Remove FAISS auto-mock + import. |
| `docs/**` (20 files) | various | Replace FAISS references with DocumentDB hybrid search. |
| ~25 test files | various | Remove FAISS mocks/assertions or rewrite against `SearchRepositoryBase`. |

### Estimated Lines of Code

| Category | Lines |
|----------|-------|
| Deleted code (FaissService + FaissSearchRepository + mock + tests) | ~2,600 |
| Modified code (rewiring, config, telemetry, metrics, build, docs) | ~600 |
| New code (release note, interface additions, migration) | ~120 |
| **Net change** | **~-1,880** |

## Testing Strategy
The full executable test plan lives in `./testing.md`. Summary:
- **Functional**: confirm `POST /api/search/semantic` and `intelligent_tool_finder` return equivalent results via DocumentDB; confirm server/agent CRUD still triggers indexing through `get_search_repository()`.
- **Backwards compatibility**: pre-change search request/response shapes still accepted; `STORAGE_BACKEND=file` deployments still boot (search routes to DocumentDB).
- **Deployment surface**: `grep -ri faiss` returns zero hits; `uv lock` resolves without `faiss-cpu`; `numpy` imports succeed in a clean venv; Docker/Terraform build without FAISS references.
- **E2E**: register a server, search for it by natural language and by tag, disable/remove it, confirm it leaves the search index.

## Alternatives Considered

### Alternative 1: Keep FAISS as an optional fallback for the file backend
**Description:** Leave `FaissService` in place but unused by default, so `STORAGE_BACKEND=file` deployments without DocumentDB can still search.
**Pros / Cons:** Pro: no behavioral change for file-backend-only deployments; no DocumentDB requirement. Con: keeps the native dependency, the dual code path, and the dual test surface - it does not actually achieve the task goal ("remove FAISS"). The whole point of the change is to eliminate the native-lib/numpy-pinning headache.
**Why Rejected:** Contradicts the task. The user explicitly wants FAISS removed because it complicates deployment.

### Alternative 2: Replace FAISS with a pure-Python in-process vector index (e.g., a numpy cosine search) for the file backend
**Description:** Swap `faiss-cpu` for a small numpy-based cosine similarity implementation so the file backend keeps an in-process search without native libs.
**Pros / Cons:** Pro: file backend stays standalone. Con: re-implements ranking, tag search, RRF, and tool extraction that DocumentDB already has; creates a third search engine to maintain; worse relevance than DocumentDB hybrid search; still re-indexes on every boot.
**Why Rejected:** Reintroduces a dual-engine maintenance burden and worse search quality. DocumentDB already does this better.

### Alternative 3: Deprecate the `file` storage backend entirely
**Description:** Remove `file` from `ALLOWED_STORAGE_BACKENDS` so every deployment must use DocumentDB/MongoDB.
**Pros / Cons:** Pro: simplest factory (`get_search_repository` always DocumentDB; no file-backend caveats). Con: far larger blast radius - touches every file-backend repository (servers, agents, scopes, security scans), breaks local-dev workflows that rely on `STORAGE_BACKEND=file`, and is well beyond "remove FAISS".
**Why Rejected:** Out of scope. Only the search repository is in scope; other file-backend repositories are untouched.

### Comparison Matrix

| Criteria | Chosen (DocumentDB-only search) | Alt 1 (keep FAISS) | Alt 2 (numpy cosine) | Alt 3 (drop file backend) |
|----------|---------------------------------|--------------------|----------------------|---------------------------|
| Achieves "remove FAISS" | Yes | No | Partially | Yes |
| Native dependency removed | Yes | No | Yes | Yes |
| Search quality | High (hybrid + RRF) | High | Lower (vector-only) | High |
| Maintenance surface | Single engine | Dual engine | Triple engine | Single engine |
| Blast radius | Medium (search only) | Low | Medium | Very high |
| File-backend standalone search | Lost (needs DocumentDB) | Kept | Kept | N/A (file removed) |

## Rollout Plan
- **Phase 0 (prerequisite):** Confirm `DocumentDBSearchRepository` passes the existing search integration tests end-to-end. (User constraint: search must not break.)
- **Phase 1 (implementation, out of scope for this skill):** Execute Steps 1-13. Land behind no feature flag - the change is a clean removal, not a toggle.
- **Phase 2 (testing):** Run the `testing.md` plan: unit, integration, backwards-compat, deployment-surface greps, E2E.
- **Phase 3 (deployment):** Ship in a minor release. Document in release notes that `STORAGE_BACKEND=file` deployments now require a DocumentDB/MongoDB endpoint for search. Operators on `file` who rely on search must provision DocumentDB; operators already on a MongoDB backend see no change.
- **Phase 4 (follow-up, separate issue):** Decide whether `STORAGE_BACKEND=file` remains a supported mode at all, and whether to add a startup self-heal that calls `find_missing_embeddings()`/`reindex_paths()`.

## Open Questions
1. **File-backend search requirement:** Is it acceptable that `STORAGE_BACKEND=file` deployments now require a DocumentDB/MongoDB endpoint for search? (The design assumes yes, since DocumentDB is the stated replacement; if no, Alternative 2 is the fallback but is not recommended.) This is the single biggest decision for the implementer to confirm with the maintainer.
2. **Startup self-heal:** Should startup call `find_missing_embeddings()` + `reindex_paths()` to repair any documents missing embeddings, or keep startup as `initialize()` only? (Recommended: ship `initialize()` only for parity; add self-heal as a follow-up.)
3. **`metrics-service` column migration timing:** Drop `faiss_search_time_ms` in this release or keep it for one release as a backfill source? (Recommended: keep for one release, drop in the next.)
4. **`registry/search/` directory:** Resolved - delete wholesale (verified: only `service.py` + empty `__init__.py`, no other importers).

## References
- Existing DocumentDB hybrid search: `registry/repositories/documentdb/search_repository.py`
- Existing correct abstraction usage: `registry/api/search_routes.py:16,31,429`
- Historical FAISS fix context: `release-notes/v1.0.17.md` (#646)
