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
- A fresh browser profile without a stored locale selected English and rendered
  a reply without errors. This also avoided the empty-locale initialization
  error seen in the 0.12.3 baseline's fresh test profile.
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
