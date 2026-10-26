# Oxy Open WebUI 0.12.4 overlay reduction

Prepared from official Open WebUI `v0.12.4`, commit
`33c1978d5bad52a31df75ea55143025dfad18d16`. The preceding Oxy source is
`origin/main` at `e17315b82565496626e3af356087bb8bf43a8144`.

The user approved the specific patch removals and retentions below after an
audit of the released upstream implementation, then authorized building,
publishing, testing in dev, and promoting to production after successful checks
and review for major automatic UI changes. Historical rollout details remain in
the earlier upgrade documents.

## Removed in favor of upstream behavior

- Configuration snapshot caching and its TTL setting; settings now use native
  database reads and native batched reads.
- Custom collaborative-note seeding; use the native client/server initialization
  and document locking introduced in 0.12.4.
- Custom OpenAPI required-parameter sorting; use upstream's deterministic order.
- Terminal discovery retry after a cached failure.
- Extra tool-progress events, their private image filter, and their associated
  structured-output reducer changes. Native tool progress and image filtering
  remain. Tests for synthetic shared IDs and missing output slots are retired.
- Redundant selected-model regeneration and provider-error handling.
- Redundant knowledge-filter pagination reset and document extraction media-type
  editor workaround.
- Markdown fade-condition refactors, unused initial background model context,
  and the extra parameterized-prompt rewrite.

## Retained Oxy behavior

- Corporate certificate trust, configurable frontend build memory, and Docker
  context exclusions.
- Original-file handling for spreadsheets, CSV/TSV, GIS/CAD, scientific formats,
  and Spotfire/Power BI files. These exclusions are separate from the removed
  document-settings editor workaround and are unchanged.
- `/api/v1/auths/verify` for the MCP proxy's credential-owner identity checks.
- Local task cleanup before Redis cleanup, plus the existing no-task stream
  cleanup fallback.
- The policy preventing scheduled runs from creating/managing other automations.
- Waiting for model defaults at both submission boundaries and preventing a
  second submission while that custom wait is pending.
- Correct MIME type for compressed image uploads.
- Existing model Skills-picker removal and the manual backed-up skill cleanup
  utility. This change does not enable lazy skill loading or restore selections.

## Upstream integration

The original-file exclusions now run inside native `index_file()`. They store
the original-file stub and skip metadata without prematurely marking the upload
complete; native callers finish knowledge linking before reporting completion.
The batch route marks excluded files complete itself. Excluded files still
bypass extraction and embeddings, and existing permission checks precede those
updates.

The native Docker baseline moves to Python 3.12 and Debian 13. Upstream package
and lockfile versions are preserved; the only extra Python development dependency
is the existing `fakeredis` dependency used by stream regressions. The retained
Docker build-memory environment setting does not override 0.12.4's explicit
12 GiB Node heap argument in its build script.

## Validation

- All 75 backend regressions pass with Python 3.12 and the exact native slim
  requirements, using disposable SQLite, fake Redis, mocked external services,
  and separate test static/data directories. These include all 20 identity
  cases, original-file upload and batch processing, durable knowledge linking,
  access rejection, native streaming, and local cleanup during Redis failure.
- All six frontend tests pass, including native dispatcher updates and retention
  of earlier tool rounds. The retired synthetic event tests are not carried.
- The production frontend build and encryption-artifact verification pass on
  Node 22.23.4. Native Svelte/browser-compatibility warnings remain.
- Extracted submission checks cover delayed defaults, duplicate sends during the
  custom wait, native upload protection, later submissions, and MIME preservation.
- The changed frontend files pass Prettier checks. Ruff formatting passes for
  all 299 Python files in the backend/test/scripts check, and the CI F-rule
  logic checks pass. The exact upstream baseline passes those checks too.
- Independent scope review confirmed ten retired-patch files match native
  0.12.4 byte-for-byte, with original-file exclusion definitions unchanged.

No shared application database was used for the isolated regression tests. The
manual skill cleanup utility was not executed; its formatting changes preserve
its AST. Historical upgrade tables receive formatting-only corrections for the
repository's frontend CI; no application code changes after the built revision.

## Published image

- Application revision: `67c3b9f7da451350eb60626078b89126270381fb`.
- Release overlay: [fork PR #8](https://github.com/jimbo-p/open-webui/pull/8).
- Main promotion: [fork PR #9](https://github.com/jimbo-p/open-webui/pull/9).
- ECR tag: `open-webui:v0.12.4-oxy1`.
- Immutable digest: `sha256:acdf343c42d42f8d0d1c78d3c99558ac157c2b2a2ceb52ba5def7b7a29d500d7`.

The maintained Dockerfile was built directly from an LF-preserving committed
archive, with the full CPU runtime, Python 3.12.15, and the exact revision in
`BUILD_HASH`. All 75 backend regressions also pass inside this actual image.
Checks verified the committed backend source, 117 pinned requirements,
dependency consistency, corporate trust certificates, and native encryption
artifact. An isolated application boot using disposable SQLite passed homepage,
health, database-health, and version checks.

Publication used native ECR APIs through the existing permitted API endpoint;
the jumpbox's Docker registry endpoint allows pulls only. No IAM or endpoint
policy was changed. Manifest readback and a fresh pull matched the tested
image's config, revision, and all 21 filesystem-layer identities.

## Dev validation

Dev serving revision `441` and ingestion revision `19` run the immutable image
above, with one task each. The update changed only the application image;
service settings, capacities, secret references, and sidecars were preserved.
Both ECS deployments completed and all old containers exited successfully.

- Every candidate task passed health, readiness, database connectivity, and
  version checks. Schema head remains `0bd252b206fb`; startup migrations remain
  disabled. The release adds no migration files.
- All 446 existing configuration values and all 55 model metadata/parameter
  fingerprints were unchanged. Native startup added only
  `skills.lazy_loading.enable=false`, `ui.visibility={}`, and
  `ldap.server.default_email_domain=''`.
- Browser checks passed keyboard submission, a visibly rendered reply,
  regeneration using the same model, and a real built-in timestamp call with
  matching call/result identifiers and a visible final answer. There were no
  JavaScript errors or duplicate completion requests.
- Composer uploads of synthetic CSV, XLSX, and DXP files completed with
  extraction skipped and downloaded bytes identical to the originals. This
  verifies original-file preservation, not parsing by Excel or Spotfire.
- An API-created note retained its content on first editor open and reload.
- A fresh browser profile with navigator locale `en-US` and no stored locale
  selected English and rendered a reply without errors. This also avoided the
  empty-locale initialization error seen in the 0.12.3 baseline's fresh test profile.
- API-key identity verification and a real read-only MCP app-listing call
  succeeded. Dedicated ingestion routing, upload, background processing,
  knowledge attachment, source download through the web service, and vector
  retrieval all passed. Each test deleted only its own temporary resources.
- Startup logs retained the same two pre-existing unavailable tool endpoints
  (`localhost:8052` and audio transcription) seen in the prior dev revision;
  no new startup failure was found.

## Automatic UI changes

The tested chat layout, composer, model selector, and response controls remain
familiar. Automatic upstream changes include smaller control/menu adjustments,
upload feedback, compaction markers, profile cards, and sidebar Shift-selection.
The upgrade does not require enabling optional features for these small changes
to appear. Lazy skill loading and other existing feature switches are preserved.

Upstream now enforces disabled personal direct integrations. Production already
has that setting disabled. Four enabled saved connections across three users
pointed to localhost; all 6,644 stored function calls reviewed for those users
over 30 days matched native or managed tools, with no distinct personal-only
operation or saved default direct-tool selection. All personal terminal entries
were disabled. This does not prove the saved connections are never used: name
collisions and browser-only selections cannot be excluded. Managed tool servers
are unaffected, and the existing production setting is preserved.

## Production verification and cutover

New production traffic moved to the verified image at 23:52:54 UTC on
2026-10-25. A temporary full-capacity fleet of 12 serving and two ingestion tasks
allowed existing chats and file processing to continue on the preceding image
while new requests used 0.12.4. Both public hostnames, all three resolved ALB
addresses, and nginx log attribution confirmed the new routes.

Serving keeps its existing sticky-session behavior. Separate nonmatching holding
rules keep both target groups associated with the ALB during the traffic switch;
ingestion uses nonsticky weighted forwarding. The permanent service settings and
all existing target-group attributes are preserved. Temporary workers alone use
the existing `TIMER_POLL_INTERVAL=86400` environment setting after their first
poll; permanent workers continue normal scheduling. This prevents temporary
workers from continually acquiring new scheduled work during their later drain.

The drain tracks exact task, assistant-message, and file IDs, checks terminal
outcomes, and extends the captured set after a scheduled-work boundary. The
initial eight current uploads completed without errors. An unchanged baseline
of 480 historical pending-file records predating every original worker is
excluded from blocking; any changed historical record re-enters the check.
No historical file or Redis state was cleaned up as part of the upgrade.

The captured pre-upgrade work drained without errors or missing results,
including all eight midnight automations. Permanent serving revision `442` and
ingestion revision `20` then completed their image-only rollout at the original
12+2 capacity. Normal scheduler polling is unchanged. All 14 preceding 0.12.3
tasks stopped normally, with exit code zero from all 42 containers.

Public traffic returned to the permanent 0.12.4 services at 00:21:09 UTC on
2026-10-26. Fresh requests through both hostnames and all three ALB addresses
were independently attributed to permanent tasks in nginx logs; none reached
the temporary fleet. The temporary workers stayed alive to finish their
captured work before retirement.

The temporary drain followed the full parent/subagent family, including actual
Redis wrapper tasks, chat JSON and normalized assistant records, and pending
internal continuations. Five child runs completed without recorded assistant
errors; one reached the existing 30-tool-call limit, whose code and configured
value are unchanged from 0.12.3.
That exact child/error fingerprint was reviewed without clearing its error.

The parent then paused on native `ask_user`. Its unanswered question, `done=false`,
and saved resume context were preserved. Source review confirmed that answering
uses a fresh HTTP request to reconstruct context from the database and start a
new task on the serving worker; it requires no callback in the temporary worker.
Only that exact assistant/call and unchanged saved snapshot were accepted as a
durable pause, not as completed work. All ten captured uploads had completed.
After more than 76 seconds with no live family tasks or response streams, no
pending internal continuations, and no missing records, temporary retirement
started at 00:44:06 UTC. No user work was cancelled or marked complete by the
rollout.

All 14 temporary tasks reached `STOPPED` by 00:51:26 UTC, with exit code zero
from all 42 containers. Their shutdowns were initiated by the ECS service
scheduler after the guarded retirement.

Temporary-resource cleanup completed at 00:53:57 UTC. Both original main-rule
actions were restored exactly, both temporary services and target groups were
removed, and both holding rules were deleted. The permanent fleet remains at
12 serving and two ingestion tasks; dev remains at one of each.
Both temporary task-definition revisions were deactivated after their services
and running family tasks were confirmed absent.

Production checks passed:

- All 14 candidate tasks passed health, readiness, database connectivity, version,
  image-digest, and unchanged-sidecar checks. Startup logs contained no new error,
  traceback, or application-load failure markers.
- Existing 445 configuration values, all 1,009 model metadata/parameter
  fingerprints, and schema head remained unchanged. Only the same three native
  defaults recorded for dev were added; personal direct integrations remain off.
- API-key identity verification and a real read-only MCP app listing succeeded.
- A disposable knowledge base and file passed dedicated ingestion routing,
  background extraction/indexing, knowledge linking, source download through the
  serving service, and vector retrieval. Both temporary objects were deleted.
- A real production browser session passed a visibly rendered reply,
  regeneration, and a linked native timestamp tool call/result with a visible
  final answer. There were no JavaScript errors, and its test chat was deleted.
- The Microsoft sign-in route returned a redirect to the expected provider.
  A full corporate SSO roundtrip was not performed. The headless test browser
  required temporary certificate-key pinning after independent certificate-chain
  and hostname validation; no application certificate or shared setting changed.
- The normal chat layout matched the dev/baseline comparison. The admin-only
  release-notes dialog was dismissed locally without saving user settings.

The early five-minute traffic sample recorded 2,868 target 2xx responses and no
target 5xx datapoints. Per-minute p95 target latency was 348–418 ms, within the
pre-cutover 125–432 ms range. Aurora writer CPU averaged 5.98% (7.15% maximum),
compared with 6.58% (8.21% maximum) before cutover. Connections were 125–150 while
both fleets and the scheduled batch coexisted. These are bounded release-window
observations, not a performance guarantee or a test of every external tool.

A later five-minute sample on the permanent services recorded 2,113 target 2xx
responses, no target 5xx datapoints, per-minute p95 latency of 122–337 ms, and
Aurora writer CPU averaging 5.95%. Repeated identity and read-only MCP probes
returned 200 with timings near the earlier samples.

Final log review identified empty-content validation rejections in a newly
appearing `python-httpx` workload attaching files through the serving route
(58 successful requests and nine HTTP 400 responses in the bounded audit).
The rejecting branch is unchanged from the preceding fork. Dedicated ingestion
attachments all returned 200 across the sampled old, temporary, and permanent
fleets, and no retained recently modified file had failed status or the matching
error. Logs did not expose correlatable file IDs, so the rejected inputs' ages
and provenance could not be established. No upgrade regression was identified;
no application code, file data, or shared settings were changed for this audit.

Deployment pins were squash-merged in
[OxyGPT PR #1268](https://dev.azure.com/oxy-well-surveillance/OpsAI/_git/oxygpt/pullrequest/1268).
Infrastructure `master` at `fa3a68d80ea50a1d8ff8b01bf35ae627479c29cc` contains
the verified dev, production serving/ingestion, and compatibility-wrapper pins.
The three changed file blobs match the reviewed source exactly. No broad
Terraform apply was performed.

The retained rollback definitions are production serving `437` and ingestion
`16`, both using
`sha256:8ce87fcfac408cd79ec9e5ee2ed21239927708722b81a0144bfab7330d49ba65`.
