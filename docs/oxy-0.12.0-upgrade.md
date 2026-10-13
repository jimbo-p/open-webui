# Oxy Open WebUI 0.12.0 upgrade

Reviewed October 12, 2026. Target: dev only; production promotion requires the user's dev acceptance.

The release branch starts at official `v0.12.0` (`15818d88fd7bde1b73de33590f50d15d21e8d7c1`). The previous fork is preserved on `0.11.4` (`da98b95ce`). The application image is built from committed source revision `fa9a30a22f3465c224b3abd776f8b7e9e09180bd`; later documentation commits do not change that application build. Builds use a 49 MiB Git archive; Docker also now excludes nested virtual environments, old build output, and local test/lint caches.

## Fork audit

The audit compares the effective `v0.11.4..0.11.4` diff, not just commit subjects. Historical experiments already removed by `7e0f34ef9` and `c7bbf5e1b` are not resurrected.

| Existing change | 0.12.0 decision |
| --- | --- |
| PRs #31431, #31439, #31424, #31315, #31405, #31502, #31501, #31641, #31593, #31895, #31892, #32029 | All twelve are incorporated upstream. Use the native implementations, including subsequent upstream fixes. |
| Per-room Redis channels (`ac562219e`, later repairs) | Native upstream #28818 replaces the fork module and environment definition. Preserve the deployed `WEBSOCKET_REDIS_ROOM_CHANNELS=true` setting. |
| Atomic Redis writes, HSCAN session cleanup, per-worker model pool cache, concurrent chat-write serialization | Already covered by the native implementation; no duplicate overlay. |
| HEIC/HEIF recognition (`c34e71b7f`) | Native #31649 handles MIME/extension detection and conversion. Retain only the compressed blob's actual MIME type when uploading it. |
| Pending knowledge sync (`9952b1fb4`) | Native `9202c7f100` replaces the local helper and its duplicate tests. Native handling also makes abandoned pending uploads retryable. |
| Corporate CA and frontend build heap (`c0d5a9cda`) | Retain. Both image stages trust the supplied corporate CA bundle; Node build heap is 8 GiB. |
| File extraction exclusions (`9952b1fb4`, `e1bdbe641`, `1d56dc057`) | Retain. Spreadsheets, CSV/TSV, GIS, CAD, scientific arrays, DXP/PBIX/PBIT stay available as original files without Tika/embedding work. Normal documents remain extractable. |
| Config snapshot cache | Retain with a repair: 0.12.0 accepts a caller-owned database transaction. Invalidate after its actual commit, including outer commits following savepoints; rollback must not publish uncommitted values. Default TTL remains one second. |
| Incremental Redis response-stream persistence (`2eccf2a3c`) | Retain. Tests cover Unicode/escaping, append recovery after eviction, tool output transitions, expiry, and local cleanup when Redis fails. |
| Incremental tool UI events / stream CPU patches (`9ea994ee9`) | Retain. Preserve separate tool calls/results and absolute output positions; completion events retain previous tool rounds. Keep base64 model input images out of these incremental UI events. |
| Seed collaborative notes from stored JSON | Retain. Native 0.12.0 still does not initialize empty Yjs history from externally authored note content. Existing content-derived IDs make identical seeds idempotent. |
| Terminal spec retry (`e0c760b7d`) | Retain. Rebuild once after a cached failed lookup before declaring a terminal unavailable. |
| Automation recursion guard (`c840deadd`) | Retain. Scheduled runs do not receive automation management tools, and `create_automation` rejects automation metadata defensively. |
| Parameterized URL / first-send initialization (`e81c2dfbd`) | Retain and adapt to the 0.12.0 typed submission/voice bridge contract. Sending waits for tool defaults; overlapping submits return a rejected bridge result. |
| Regenerate with selected model; knowledge view pagination; document MIME editor; inline markdown hardening | Retain the narrow differences still absent upstream. |
| Initial title task model (`9fbe9b4ad`) | Retain the explicit model in its response context. |
| Deployment-only OpenAPI required-field ordering patch in `oxygpt` | Move into fork source: sort deduplicated `required` fields. Native 0.12.0 still uses `list(set(...))`, which varies across Python workers and damages prompt caching. The old v0.11.3 string patch is obsolete. |

### Your authored upstream PRs

GitHub lists these PRs as closed without a merge. Their intended behavior must therefore be checked in the current code rather than inferred from PR state:

- [#26247](https://github.com/open-webui/open-webui/pull/26247): native automation requests now carry a session token; tool and terminal session-auth paths consume it. Use native handling.
- [#25718](https://github.com/open-webui/open-webui/pull/25718): native migration `b8f2c6d91a04` adds `file_pending_knowledge_idx`. Use that migration, and verify the index exists because upstream permits a failed index creation to be recorded as skipped.
- [#22249](https://github.com/open-webui/open-webui/pull/22249): native `query_knowledge_files` contains the explicit `knowledge_ids` search path.
- [#21144](https://github.com/open-webui/open-webui/pull/21144): the old `bugfix/full-context-mode-for-model-attachments` branch contains seven commits absent from the deployed 0.11.4 overlay. It forced entire selected knowledge files into prompts. Native 0.12.0 still injects model knowledge only for legacy calling; native tool mode accesses it through knowledge tools. Preserve the deployed behavior; this archived branch is outside the upgrade and requires no decision for this rollout.

Other archived branch tips were checked as well: older 0.11.1 branches duplicate earlier versions of the active overlay or already retired experiments; `dev` has an older tool-ID association fix; `upgrade/owui-0.10.2-no-pgvector-reindex` holds old index suppression, corporate CA, and initialization-wait patches. Preserve their branches for reference. Keep the active equivalents where needed and use current pgvector migrations/configuration rather than reviving obsolete index suppression.

## Post-release fixes selected

Reviewed upstream `dev` through `0309c9ad1` and all eleven open PRs returned at review time. Cherry-picks record their source SHA with `-x`.

| PR | State at review | Reason |
| --- | --- | --- |
| [#32229](https://github.com/open-webui/open-webui/pull/32229) | Merged into dev | MCP connection failures reach normal error logs. |
| [#32226](https://github.com/open-webui/open-webui/pull/32226) | Merged into dev | Cross-origin Rich UI embeds do not throw when injecting arguments. |
| [#32232](https://github.com/open-webui/open-webui/pull/32232) | Merged into dev | External knowledge citations keep their page title. |
| [#32225](https://github.com/open-webui/open-webui/pull/32225) | Merged into dev | Correct Top K / reranker help text. |
| [#32224](https://github.com/open-webui/open-webui/pull/32224) | Merged into dev | Correct model editor behavior when switching languages. |
| [#32235](https://github.com/open-webui/open-webui/pull/32235) | Open | New-chat tool, web-search, and skill selections survive navigation/reload. |
| [#32233](https://github.com/open-webui/open-webui/pull/32233) | Open | External citations no longer point to nonexistent uploaded files. |
| [#32239](https://github.com/open-webui/open-webui/pull/32239) | Open | Skill editors and related ID creation work on dev's plain HTTP address. |

Not selected: Redis Sentinel fixes #32234/#32241 because this deployment uses direct ElastiCache endpoints; #27145 because it has conflicts and predates the current direct-tool implementation; broad reindexing, Docling, OneDrive, PDF export, and template-variable feature PRs. The remaining dev refactors principally change voice/avatar and interface behavior and were not required for this release candidate. Open PR cherry-picks remain local carry patches until upstream accepts or replaces them.

## Environment decisions

No new optional feature is enabled by this rollout. Dev already explicitly enables orjson and room channels. `ENABLE_PLUGINS` is not set false in the deployed task, so its new master-switch behavior will not disable the corporate integrations. `WEBUI_SECRET_KEY` is already injected from Secrets Manager.

| Decision | Settings and defaults | Recommendation |
| --- | --- | --- |
| Pilot tool discovery | `ENABLE_TOOL_SEARCH=false`; `TOOL_SEARCH_DEFER_THRESHOLD=400` characters; `TOOL_SEARCH_ALWAYS_LOADED` empty; `TOOL_SEARCH_DEFER_BUILTIN_TOOLS=true` | Highest-value dev experiment for many MCP tools. Compare success, first-token time and cache hits before enabling broadly. Pick critical always-loaded tool names/patterns deliberately. Requires native function calling. |
| Require additional OWUI MFA | `ENABLE_MFA=false`; `MFA_ALLOW_OAUTH_BYPASS=false`; `MFA_ALLOW_TRUSTED_HEADER_BYPASS=false` | Decide whether the identity provider's MFA is sufficient, and whether break-glass/local accounts should enroll in OWUI MFA. Do not impose a second login factor by surprise. |
| Realtime voice | `AUDIO_REALTIME_ENABLED=false`; endpoint/key/model/voice/transcription settings; `REALTIME_CALL_PROMPT_TEMPLATE`, `REALTIME_TTS_PROMPT_TEMPLATE` | Keep disabled until an approved endpoint, credential, model, cost policy, and recording/transcription policy are selected. The default endpoint is public OpenAI, not the existing LiteLLM route. |
| In-process plugins vs remote tools | New `ENABLE_TOOLS=true`, `ENABLE_FUNCTIONS=true`, `ENABLE_TOOL_SERVERS=true`; existing `ENABLE_PLUGINS=true` is now their master switch | Preserve current behavior. If reducing in-process plugin use later, disable Tools/Functions individually and leave tool servers on. Check existing filters/functions before changing this. |
| Tavily depth | New `TAVILY_SEARCH_DEPTH=basic` | Keep basic unless deeper searches justify added latency and provider cost. |
| Arena availability | Existing `ENABLE_EVALUATION_ARENA_MODELS` default changes to false | Existing persisted choices win; confirm only if users depend on Arena. |

`ENABLE_PERSISTENT_CONFIG=true`: for database-backed options, changing a task environment variable does not necessarily overwrite an existing saved value. Use the corresponding Admin setting or a narrowly scoped config update; do not reset all persistent config. Keep `ENABLE_DB_MIGRATIONS=false` in serving tasks. No embedding model, vector dimensions, or pgvector storage settings are changed and no reindex is requested.

The full set of newly read environment names between upstream 0.11.4 and 0.12.0 is the sixteen configuration settings above plus `ENABLE_TOOLS`, `ENABLE_FUNCTIONS`, `ENABLE_TOOL_SERVERS`, and native `WEBSOCKET_REDIS_ROOM_CHANNELS`. `ENABLE_ORJSON` changes default to true, which is already explicit in this deployment. The retained fork-only `CONFIG_CACHE_TTL` remains one second.

## Twenty improvements most relevant to corporate users

These are selected from the [official 0.12.0 release](https://github.com/open-webui/open-webui/releases/tag/v0.12.0) and the shipped [changelog](../CHANGELOG.md). Optional features require configuration; this list does not claim they are all enabled or benchmarked here. The twelve previously carried fixes are excluded as new benefits of the upgrade.

1. **Tool Search:** load detailed tool schemas on demand for large MCP catalogs, reducing prompt size when enabled.
2. **Compaction between tool rounds:** long investigations can compact during an active tool loop, keeping tool calls and results together.
3. **Structured MCP results:** the model receives structured result data alongside text rather than silently losing it.
4. **MCP image results:** images returned by tools can reach the model for analysis.
5. **More reliable corporate OAuth:** coordinated refresh across workers and current tokens on tool calls reduce failures in long sessions and headless runs.
6. **Long MCP tool names:** stable shortened names work with providers enforcing the 64-character limit.
7. **Better MCP OAuth compatibility:** scope discovery, refresh tokens, and long scope lists work across more providers.
8. **Visible MCP tool inventories:** users can expand a selected server to inspect its tool descriptions/count and reconnect when needed.
9. **Graceful unavailable-server handling:** a failed MCP server is shown in chat status while available tools can still be used.
10. **More stable terminal prompts:** working-directory and shell-tool controls help preserve prompt-cache prefixes.
11. **Multi-file skills and history:** teams can package reusable instructions with support files and restore previous versions.
12. **Model version history:** restore a reviewed assistant configuration after a bad edit.
13. **Tool/function version history:** inspect and roll back plugin changes without making duplicate tools.
14. **Knowledge file browser:** browse original files and indexed text together, preview documents, and edit extracted content.
15. **Faster PostgreSQL knowledge pages:** the pending-file index avoids scanning a large file table when opening knowledge bases.
16. **Named model controls:** expose understandable choices such as reasoning levels instead of raw parameter fields.
17. **Nested groups:** model departmental access and inherited permissions through a group hierarchy.
18. **Group-specific default models:** give each team a sensible starting assistant without changing the global default.
19. **Shared-chat replies and forks:** collaborate in writable shared conversations or branch a shared conversation into personal work.
20. **Office document generation:** bundled Python libraries let the code interpreter generate Word documents and PowerPoint decks.

## Validation and dev acceptance

- Backend fork regression suite: 32 passing tests; covers cache transaction semantics, Redis stream recovery, and extraction exclusions.
- Frontend: seven passing tests, including three tests for tool/result identity, sparse event positions, and preserving previous tool rounds.
- All Python modules compile. Differential Ruff F checks introduce no findings relative to upstream 0.12.0.
- Stored-note seeding smoke check preserves one copy of the content when the same CRDT update is applied twice.
- Full frontend type check is not clean upstream: 6,040 errors / 189 warnings on untouched 0.12.0 versus 6,032 errors / 189 warnings on this fork, using the same installed dependencies. A diagnostic comparison found no introduced errors and eight removed errors. This does not substitute for a clean upstream type check.
- The standard amd64 container builds successfully and imports the tool converter, MCP client and config model under its Python 3.11 runtime. Final image digest and deployment checks are recorded in the deployment report in `oxygpt`.

Before production: test corporate sign-in/token renewal; new-chat tool selection after navigation; multiple MCP calls and rich results; approval/continue/cancel; a long run crossing the compaction threshold; scheduled session-auth tools; full-context model knowledge; skipped spreadsheet/GIS uploads; edited knowledge search; externally created notes; and cross-replica live updates. Tool Search should get a separate enabled/disabled comparison if approved for a pilot.

## Migration and rollback constraints

Use the existing `oxygpt/tools_and_diagnostics/owui_migration/Invoke-OwuiOfflineMigration.ps1` against **dev**, with the new image and the dev Aurora writer. Review with `verify`, then apply `upgrade,check` outside the load balancer. Six native revisions follow `d4c1a8e37b62`: MFA/session stamps, group hierarchy, skill history, model history, tool/function histories, and the PostgreSQL pending-file index.

The pre-upgrade dev service is `oxygpt-open-webui-task:411`, image `v0.11.4-oxy2`. Record the new digest and task revision in the deployment report. Production must stay on its existing task definition. Aurora has a seven-day restore window; an application image rollback does not undo schema migrations. Do not downgrade or re-embed the database as an automatic rollback step. The existing and replacement builds both use compatible per-room Redis delivery with the setting enabled; an actual toggle would require a coordinated restart across that fleet.
