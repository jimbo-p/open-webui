# Oxy Open WebUI 0.12.4 overlay reduction

Prepared from official Open WebUI `v0.12.4`, commit
`33c1978d5bad52a31df75ea55143025dfad18d16`. The preceding Oxy source is
`origin/main` at `e17315b82565496626e3af356087bb8bf43a8144`.

The user approved the specific patch removals and retentions below after an
audit of the released upstream implementation. This document records a source
candidate, not a deployment. Historical rollout details remain in the earlier
upgrade documents.

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

No shared application database was used for these tests. Full browser acceptance,
image packaging, and deployment integration have not been performed. The manual
skill cleanup utility was not executed; its formatting changes preserve its AST.
