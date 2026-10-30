# Oxy Open WebUI 0.12.5 upgrade

The approved release imports exact official
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

| Files                                                                                        | Retained purpose                                                                                                                                 |
| -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `.dockerignore`, `Dockerfile`, `corp-ca.crt`                                                 | Corporate trust, build-memory setting and Docker context exclusions.                                                                             |
| `backend/open_webui/retrieval/skip_extraction.py`, `backend/open_webui/routers/retrieval.py` | Preserve original spreadsheet, structured, scientific and binary files without extraction, with permission checks and durable knowledge linking. |
| `backend/open_webui/routers/auths.py`                                                        | Credential-owner `/verify` endpoint and strict micro-app delegation lineage.                                                                     |
| `backend/open_webui/tasks.py`, `backend/open_webui/main.py`                                  | Local task cleanup before Redis cleanup and no-task response-stream cleanup.                                                                     |
| `backend/open_webui/tools/builtin.py`, `backend/open_webui/utils/tools.py`                   | Scheduled runs cannot create or manage further automations.                                                                                      |
| `src/lib/components/chat/Chat.svelte`                                                        | Wait for model defaults at both submission boundaries and reject duplicate sends during that wait.                                               |
| `src/lib/components/chat/MessageInput.svelte`                                                | Compressed image uploads use the actual blob MIME type.                                                                                          |
| `src/lib/components/workspace/Models/ModelEditor.svelte`                                     | Model Skills picker remains removed.                                                                                                             |
| `scripts/clear_model_skills.py`, tests and `fakeredis` development dependency                | Preserve manual maintenance capability and retained-fix regression coverage. The cleanup utility is not part of upgrade execution.               |

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

The production plan uses the established full-capacity fleet handoff: warm the
candidate, redirect new traffic, let captured work finish, then retire legacy
containers. For the 6 PM after-hours window, the user explicitly accepts the
low likelihood of overlapping note editing and prefers this normal handoff over
a maintenance outage or additional application patches. Observe and record note
sessions and pending saves at the window, preserve captured pending work, and
retain recovery evidence. This is acceptance of the native rolling-upgrade risk,
not evidence that mixed-version note writes are compatible.

After conversion, an image rollback alone is insufficient for note compatibility.
Preserve converted rows and subsequent edits, stop conflicting note writes, and
reconcile against recovery snapshots before any targeted restore. Do not restore
the whole database and lose unrelated work.

## Captured deployment baseline

At preparation, all four services were stable and all OWUI containers matched
the maintained image `microapp-auth-20261028-2b9989cd`, digest
`sha256:d23cede06d11a1952db07f8cf1778869cf635708d1d24e110743839833bd6177`.

| Environment | Service   | Task definition                    | Running capacity |
| ----------- | --------- | ---------------------------------- | ---------------- |
| Dev         | Serving   | `oxygpt-open-webui-task:449`       | 2                |
| Dev         | Ingestion | `oxygpt-open-webui-ingest-task:25` | 1                |
| Production  | Serving   | `oxygpt-open-webui-task:450`       | 12               |
| Production  | Ingestion | `oxygpt-open-webui-ingest-task:28` | 2                |

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
   source/image provenance, production baseline, the accepted note risk and the production
   time. Preserve active chats, uploads, scheduled work and subagent families;
   verify routing and actual container exits before retiring old fleets.
6. Update the separate OxyGPT repository's dev/prod serving/ingestion Terraform
   pins and compatibility Dockerfile, preserve unrelated component changes,
   complete PR checks and merge the approved release. Record final live evidence.
7. Remove verified temporary test/deployment resources through the owning
   workflow. Retain release images and required recovery evidence. Report exact
   cleanup results and deliberately retained resources.

Source, image, dev and production evidence is recorded below.

## Source validation

On application commit `769a737db5a0419c95420048a1fe481115635b92`, all 102
backend regressions and six frontend tests pass. Six extracted submission,
default-selection and MIME checks pass. A real disposable SQLite probe verifies
note conversion, attachment/version preservation, nested page edits and reload,
and rejection of stale page updates. Ruff formatting passes for 318 files;
CI F-rule logic checks, full Prettier and the native encryption checksum pass.
The i18n parser produces no source changes.

These checks used Python 3.12.15, Node 22.23.4, isolated SQLite/static/data
locations and fake Redis with Lua support. They did not write to shared
application databases. The manual skill cleanup utility was not executed.
Build commit `11e39ad56c301cf452f16eb35eeb5738a5eeb751` adds only AGENTS/release
documentation to that validated application tree; all application, build,
dependency and workflow blobs are identical. Subsequent evidence/formatting
commits update documentation only.

## Tested and published image

The native maintained Dockerfile was built directly from the committed archive
at `11e39ad56c301cf452f16eb35eeb5738a5eeb751`, with full CPU runtime,
`USE_SLIM=false`, `USE_CUDA=false`, `USE_OLLAMA=false`, `BUILD_CHANNEL=main`,
and that exact revision in `BUILD_HASH` and the OCI revision label. The archive
SHA256 is `4b17de34a55dfadabb3096849e9431d71abd17bb42a307e6dd79c4a8d620a6b4`.

- ECR tag: `open-webui:v0.12.5-oxy1`.
- Immutable registry digest: `sha256:83f92c311fb924bec6085e8be7992ebad2a44cbdc80022abc13459c7b492f522`.
- Image config digest: `sha256:396c292fec659992f060b474722749568fbdc61a1505e6445defd684eab52192`.
- Runtime Python: 3.12.16, from the native Python 3.12 image baseline.

All 102 backend regressions pass inside this actual full image. Its 291 backend
Python files match committed source, all 117 pinned requirements are satisfied,
`pip check` passes, all eight corporate certificates are trusted, and the native
encryption checksum matches. An isolated boot with disposable SQLite and no
network passes homepage, health, readiness, database health and version probes.

Publication used the established native ECR API path. Manifest readback and a
fresh registry pull match the tested config and all 21 root filesystem layers.
Origin was synchronized before the build and rechecked before publication and
dev rollout; later commits changed documentation only. The temporary 4.4 GB
image export and registry-login directory were removed after pull verification.

## Dev validation

Serving revision `451` and ingestion revision `29` run the immutable image above
at the unchanged two-plus-one capacity. Only the application image changed;
sidecars, service settings, environment values and secret references are
preserved. All three captured previous tasks and their containers stopped with
exit code zero.

All 449 pre-existing configuration values and all 55 model metadata/parameter
fingerprints are unchanged. Only the three expected native defaults were added,
for 452 total values. Schema head remains `0bd252b206fb`. All five original
notes retain their exact initial fingerprints; the aggregate SHA256 is
`a4b3dcdcdbad0b4de8324ac9d5a80cc2f0153c4bcaf362a952fbfd36fe072490`.

Live browser/API checks pass:

- A visible chat reply, regeneration with the same model, and a real timestamp
  tool call with linked call/result IDs and a visible final answer.
- An immediate model switch followed by double Enter sends exactly one
  completion to the new model. The selected alternative had no default tools;
  nonempty default-tool selection is covered by the isolated function checks.
- API identity/delegation checks and a real read-only managed MCP listing.
- Composer and API CSV, XLSX and DXP uploads preserve original bytes and skip
  extraction. Dedicated ingestion, processing, knowledge linking, affected-only
  API responses, paginated membership, source download and vector retrieval work.
- Both held-open 0.12.4 note windows automatically reload into 0.12.5. Legacy
  attachments and versions survive conversion. Two independent browser sessions
  retain concurrent edits, catch up after offline/reconnect, and save again.
  Nested/page creation, edits, reload and stale-edit rejection pass.
- No JavaScript errors appeared. The release dialog was dismissed locally;
  saved user settings were not changed. A full corporate SSO roundtrip was not
  part of these checks.

All 20 task-owned application objects were deleted: two chats, eleven files,
one knowledge base and six notes. Final database/vector checks found no owned
objects, normalized messages, links or grants. Ten captured S3 paths returned
HEAD 404; the remaining legacy file had API-confirmed native storage deletion.
Private harness artifacts remain only until sanitized evidence is archived.

## Production handoff

The release began in the approved October 29, 2026, 6 PM America/Chicago
window. New serving and ingestion traffic reached the tested image at
23:06:34 UTC. The permanent services were updated to serving definition `452`
and ingestion definition `30`, preserving twelve serving tasks, two ingestion
tasks, all environment/secret references, sidecars and service settings.

Traffic returned to the permanent services at 06:00:59 UTC on October 30.
All fourteen preceding tasks and all 42 containers actually stopped with exit
code zero by 05:53:44 UTC. All fourteen temporary tasks and their 42 containers
stopped with exit code zero by 06:20:19 UTC. No chat was cancelled or marked
complete to force this handoff; active work and its continuations drained.
Twelve pre-existing processing records and their stored files were preserved
for retry under the user's direction to finish the release. Their exact row
fingerprints and transaction versions were checked before retirement; they were
not reported as successfully indexed.

Production browser/API checks passed for visible chat, a linked timestamp tool
call, API-key identity, original CSV preservation, dedicated ingestion,
knowledge linking, download and vector retrieval. Full corporate SSO was not
retested. All six synthetic production objects were deleted, three stored files
returned HEAD 404, and independent database/vector/link checks returned zero.
The temporary browser certificate alias was removed. The 23:23 UTC validation
snapshot retained all existing configuration, model and original-note
fingerprints; only the three expected native defaults were added. The schema
head remains `0bd252b206fb`. All twelve retained original objects subsequently
returned HEAD 200 with lengths matching their stored metadata.

The four-hour observer recorded 28,692 requests, including fifteen ALB-generated
502 responses on ingestion health and no serving readiness/version failures.
This is not a zero-error claim. The ingestion proxy's 30-second keepalive is
shorter than the shared ALB's 1,200-second idle timeout, a configuration pattern
covered by AWS's connection-close troubleshooting guidance. An infrastructure
follow-up in OxyGPT PR 1307 pins the existing nginx binary/packages and changes
only the proxy keepalive to 1,260 seconds; the OWUI application image remains
the tested digest above. Its syntax, route/authentication/limit checks and a
same-connection 35-second idle probe passed in isolation and in dev.

By 06:30:28 UTC, the two owned temporary services were inactive and both
temporary target groups and holding rules were absent. AWS accepted deletion
of the two dedicated temporary task definitions; their metadata remains
`DELETE_IN_PROGRESS` while an independent verifier checks for final absence.
Current release images, prior production definitions `450`/`28`, the preceding
image, and encrypted Aurora recovery history are retained. The separate
infrastructure release report tracks final proxy rollout and metadata cleanup.
The manual model-skills cleanup utility was never run.
