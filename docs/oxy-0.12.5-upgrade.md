# Oxy Open WebUI 0.12.5 upgrade

The approved candidate imports exact official
[Open WebUI 0.12.5](https://github.com/open-webui/open-webui/releases/tag/v0.12.5),
`f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`, while retaining the current Oxy
overlay from fork `origin/main` at
`2b9989cda3cf702ca5d9f31725acd29c30e402ae`. No post-release upstream changes
are included. The user approved this scope, dev validation, and a production
move at 6 PM America/Chicago on October 29, 2026, conditional on successful
checks. That production window begins at 23:00 UTC.

## Retained customizations

The source audit found no remaining current fork customization fully replicated
by 0.12.5. Both commit equivalence and source behavior were checked. Patches
retired during earlier upgrades remain retired.

| Files | Retained purpose |
| --- | --- |
| `.dockerignore`, `Dockerfile`, `corp-ca.crt` | Corporate trust, build-memory setting and Docker context exclusions. |
| `backend/open_webui/retrieval/skip_extraction.py`, `backend/open_webui/routers/retrieval.py` | Preserve original spreadsheet, structured, scientific and binary files without extraction, with permission checks and durable knowledge linking. |
| `backend/open_webui/routers/auths.py` | Credential-owner `/verify` endpoint and strict micro-app delegation lineage. |
| `backend/open_webui/tasks.py`, `backend/open_webui/main.py` | Local task cleanup before Redis cleanup and no-task response-stream cleanup. |
| `backend/open_webui/tools/builtin.py`, `backend/open_webui/utils/tools.py` | Scheduled runs cannot create or manage further automations. |
| `src/lib/components/chat/Chat.svelte` | Wait for model defaults at both submission boundaries and reject duplicate sends during that wait. |
| `src/lib/components/chat/MessageInput.svelte` | Compressed image uploads use the actual blob MIME type. |
| `src/lib/components/workspace/Models/ModelEditor.svelte` | Model Skills picker remains removed. |
| `scripts/clear_model_skills.py`, tests and `fakeredis` development dependency | Preserve manual maintenance capability and retained-fix regression coverage. The cleanup utility is not part of upgrade execution. |

Upstream rewrites model-default selection to preserve manual picks and combine
defaults across models. It still does not wait for those defaults at submission
boundaries; the custom asynchronous-send protections remain necessary. Its
Web Search toggle correction addresses a separate ordering problem.

Application integration commit
`769a737db5a0419c95420048a1fe481115635b92` imports 206 native changed paths.
All 200 changed paths without Oxy overlays match the official release exactly;
the existing custom inserted/deleted lines and the 26 overlay paths match the
preceding overlay. Existing tests and manual utilities are preserved.

## Upstream behavior and compatibility

Automatic changes include note pages and live presence, saved model switches,
merged model defaults, regeneration with current model selections, chat/knowledge
file display, and administrative session analytics and API-key controls.

There are no Alembic migration or backend requirement changes. Python 3.12 and
Node 22 remain suitable. The frontend dependency lock intentionally changes
`prosemirror-view` to 1.42.6 for the IME editing fix. New persisted configuration
defaults are `rag.mistral_ocr_params`, `rag.embedding_query_prefix`, and
`rag.embedding_content_prefix`; preserve existing settings and model metadata.

Knowledge mutation responses now return the affected files or an empty list,
rather than the entire collection. The inspected catalog, Drive, SharePoint and
Alation sync clients ignore successful mutation bodies and list membership
separately; the catalog uses the paginated files endpoint. OxyGPT ingestion and
hybrid-KB verification probes also list/read separately. No client code change
is indicated; disposable dev attachment/removal/list/retrieval remains a gate.

MCP sessions now run in separate tasks so a transport failure does not cancel
the entire reply. Remote fetches request uncompressed responses and reject
unexpected compressed bodies. Validate successful and failed MCP calls,
representative URL retrieval, and the corporate sign-in route in dev.

## Notes and rollback

Notes gain `pages[]`, per-page revisions/generations and inline attachments.
Startup does not bulk-convert rows, and API reads normalize only a deep copy.
Creation and all note updates persist normalized JSON. Opening an editable note
can itself persist conversion through its initial collaborative content snapshot.

Old 0.12.4 workers ignore pages when writing the legacy content field, so
concurrent old/new note writers can diverge. Existing WebSockets survive a
traffic switch. The notes feature flag alone cannot freeze existing socket
writers, and there is no native switch disabling pages independently.

Production gates therefore include recoverable note snapshots, active note
document sessions in websocket Redis, and pending document-save tasks. Do not
probe real production notes with the candidate before cutover. If old note
writers cannot be quiesced safely, hold the production move. Test legacy content,
attachments, new/nested pages, concurrent edits, reconnect, reload and recovery
with disposable dev notes.

After conversion, an image rollback alone is insufficient for note compatibility.
Preserve converted rows and subsequent edits, stop conflicting note writes, and
reconcile against recovery snapshots before any targeted restore. Do not restore
the whole database and lose unrelated work.

## Captured deployment baseline

At preparation, all four services were stable and all OWUI containers matched
the maintained image `microapp-auth-20261028-2b9989cd`, digest
`sha256:d23cede06d11a1952db07f8cf1778869cf635708d1d24e110743839833bd6177`.

| Environment | Service | Task definition | Running capacity |
| --- | --- | --- | --- |
| Dev | Serving | `oxygpt-open-webui-task:449` | 2 |
| Dev | Ingestion | `oxygpt-open-webui-ingest-task:25` | 1 |
| Production | Serving | `oxygpt-open-webui-task:450` | 12 |
| Production | Ingestion | `oxygpt-open-webui-ingest-task:28` | 2 |

Startup database migrations are explicitly disabled. Preserve sidecars, service
configuration and full capacity. Retain current production definitions 450/28
and their image as the rollback baseline; historical upgrade definitions are
not the current baseline. Native readiness is `/ready`; unknown health-like
paths can return frontend HTML with HTTP 200.

## Validation and delivery gates

1. Run isolated backend regressions, frontend tests, encryption verification,
   production frontend build and applicable format/logic checks on the committed
   candidate. Add focused disposable notes and model-default probes where needed.
2. Synchronize origin immediately before building the native maintained
   Dockerfile, with full CPU runtime and the exact source revision in BUILD_HASH.
   Record build arguments, source and immutable image digest. Run regressions,
   dependency/trust checks and isolated startup probes in the actual image.
3. Recheck origin before publication and deployment; rebuild if relevant source
   advanced. Verify the published config/layers match the tested artifact.
4. Validate dev serving and ingestion: authentication/delegation, visible chats
   and tools, model switches/immediate sends/manual picks/multiple models,
   duplicate-send prevention, original CSV/XLSX/DXP preservation, notes, dedicated
   ingestion, knowledge linking/download/retrieval, and config/model fingerprints.
5. Schedule only reviewed orchestration that verifies approval, dev results,
   source/image provenance, production baseline, note safety and the production
   time. Preserve active chats, uploads, scheduled work and subagent families;
   verify routing and actual container exits before retiring old fleets.
6. Update the separate OxyGPT repository's dev/prod serving/ingestion Terraform
   pins and compatibility Dockerfile, preserve unrelated component changes,
   complete PR checks and merge the approved release. Record final live evidence.
7. Remove verified temporary test/deployment resources through the owning
   workflow. Retain release images and required recovery evidence. Report exact
   cleanup results and deliberately retained resources.

Build, dev and production results will be appended as their gates complete.
