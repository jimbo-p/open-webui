# Oxy Open WebUI 0.13.3 upgrade and dev validation

## Built-in tool editor inheritance correction

On November 11, 2026, the user approved the narrow frontend correction, dev
validation and production delivery. The base is maintained `origin/main`
`a590d2588ac5c2b7812ca4c2b3a77095108064c4`, retaining exact official 0.13.3
and the existing Oxy overlay.

The backend intentionally inherits `meta.builtinTools` defaults per category.
The editor previously selected an entire model map instead and deleted a
category when enabled. With administrator `notes=false` and model `time=true`,
it displayed Notes enabled while the backend disabled it; toggling off/on could
not create the required true override. This is a fork integration mismatch.

`ModelEditor.svelte`, `Capabilities.svelte` and `BuiltinTools.svelte` now pass
administrator defaults separately from sparse model overrides. Toggles and the
subagent selector display effective merged settings, while saves retain only
model overrides. Enabling or disabling a category writes explicit true or false.
Untouched categories continue to inherit later administrator default changes.
The shared administrator-default controls retain their existing meaning.

No backend source, dependency declaration, schema, Dockerfile or global configuration
change is required. All existing customizations and explicit production Notes overrides
are retained; none are retired. The previous same-version image remains usable
for rollback, although its editor still has this mismatch. Deployment evidence
is tracked in the [infrastructure release record](https://dev.azure.com/oxy-well-surveillance/OpsAI/_git/oxygpt?path=%2Freports%2Fbuiltin-tools-inheritance-20261111.md&version=GBmaster).

### Reviewed artifact and dev evidence

- Application source: `036dfe64d8c444ee987ba43e913f1854155a8762`.
- Committed archive SHA256:
  `209eb1816d332521f91eee64a15008e028697da8518dc9e60daa6da2078afeb1`;
  all 5,336 archive files verified before the build.
- Build: unchanged maintained Dockerfile, full CPU image,
  `USE_SLIM=false`, `USE_CUDA=false`, `USE_OLLAMA=false`,
  `BUILD_HASH=036dfe64d8c444ee987ba43e913f1854155a8762`, and
  `BUILD_CHANNEL=main`. The image revision label records that same source.
- Tag: `open-webui:v0.13.3-oxy2-036dfe64d`.
- Immutable image:
  `217589814678.dkr.ecr.us-east-1.amazonaws.com/open-webui@sha256:625925e64ab0720f65a43a3279ca22a89054beea04641f268e5ff269eafbefff`.
- Tested configuration digest:
  `sha256:9cdb8b882ecee0ef13441fbfef0110a8ab2184752ac0f8f4fa4879b5df3c54f1`.
  Registry readback and an immutable pull verify the source label,
  configuration and all 21 root filesystem layers match the tested artifact.

Validation passes 14 frontend unit tests, native encryption verification,
formatting and translation checks, and 119 backend plus nine maintenance
regressions inside the actual candidate image with disposable databases. Native
startup becomes ready in 31.4 seconds; `/health`, `/ready` and `/api/version`
return typed JSON for version 0.13.3. All 321 backend Python files and 117 pinned
requirements match the reviewed source; eight corporate certificates and the
encryption WASM artifact are verified. Mounted test dependencies preserve the
candidate's native runtime packages.

The focused browser regression fails on the preceding dev image at the exact
inherited Notes assertion. All seven browser cases pass on the corrected built
frontend and again on actual dev serving definition 460 and ingestion definition
41 using the immutable digest above. Dev checks completed on November 11 at
**1:02 PM CST**: real editor save/reopen and OFF-to-ON persist explicit true;
native chat completes `search_notes` and `view_note` against a private synthetic
note; rapid double Enter submits one completion with no browser page errors;
credential-owner MCP identity, ingestion/retrieval and original CSV bytes pass.

All 474 existing configuration values and 55 existing models retain their
baseline hashes. Both temporary accounts and their sessions, model/history,
note, chat, knowledge, file/chunk/grant and related records are absent across
17 scoped categories; both uploaded S3 objects are verified absent. Inherited
false UI cases replace only the browser's defaults response because actual dev
defaults omit those categories. Real saves and Notes calls use unmocked APIs.
An initial direct readiness timeout occurred before fixtures; repeated native
serving and ingestion checks passed without configuration changes.

GitHub did not launch PR checks after push/open/reopen despite active workflow
configuration. Required formatting, translation, production build and frontend
tests passed locally or in the actual image build; no branch-protection bypass
was used. Infrastructure PR policies and final worker retirement, production
pins, verification and cleanup are recorded in the linked release report.

### Resolved transitive dependencies

The unchanged native recipe resolves unpinned transitive dependencies during
the build. The approved preceding image and candidate each contain 316 Python
distributions, with no additions or removals. All 117 pinned requirements match;
the following nine transitive versions differ and are included in the tested
artifact:

| Distribution   | Preceding image | Candidate |
| -------------- | --------------- | --------- |
| annotated-doc  | 0.0.5           | 0.0.6     |
| ctranslate2    | 4.8.5           | 4.9.0     |
| google-auth    | 2.62.1          | 2.62.2    |
| jsonpatch      | 1.45            | 1.47      |
| jsonpointer    | 3.10            | 3.12      |
| markdown-it-py | 4.2.0           | 4.3.0     |
| pybase64       | 1.5.1           | 1.5.2     |
| soupsieve      | 4.0.1           | 4.1       |
| typer          | 0.29.0          | 0.29.1    |

Refresh editor tabs opened before rollout to load the corrected frontend. The
application version remains 0.13.3, so automatic tab reload is not assumed.

Initial audit date: November 9, 2026. The user approved exact official 0.13.3
with the retained Oxy overlay and Escape fix, followed by the concrete timer
backpressure and builtin-tool default-inheritance fixes and development
validation. The approved application source is
`7e5fa4c5902d84d3b0d3c946bb5ebc8917d08486`. The final immutable image passes
isolated startup, retained regressions and PostgreSQL timer checks; registry
publication is verified. Dev now serves from the permanent 2+1 worker fleet.
Canonical UI/file/knowledge/identity/subagent checks, real timer completion,
and a 120-second same-job return test pass. All six retired tasks/18 containers
exit zero, owned QA fixtures are cleaned, and the exact 38 user-approved
unfinished-file originals/records remain unchanged. The database expansion and
22 native configuration defaults are registered. Owned infrastructure cleanup
remains limited to two asynchronous deletion metadata records. The durable dev
pin is tracked in [infrastructure PR1370](https://dev.azure.com/oxy-well-surveillance/OpsAI/_git/oxygpt/pullrequest/1370).
Production remains unchanged; promotion requires a later decision.

## Exact comparison and scope

- Maintained fork: `origin/main`, `0980084f77292f778c36cb6d667190f2f39890b7`.
- Current official baseline: `upstream/v0.12.5`,
  `f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`.
- Approved official release: `upstream/v0.13.3`,
  `b0e6098e940942281d9c83f3529ea117287a3a22`.
- Infrastructure: OxyGPT `origin/master`,
  `0cc8170dae16b7568c93f14dd7bb0e1406513855`.

In Git, `origin` is the maintained jimbo-p fork; `upstream` is official
Open WebUI. Native equivalence in this audit means the exact official release,
not another fork branch or unreleased upstream development commits.
Both repositories were fetched and pulled before the audit. The pre-existing
uncommitted infrastructure `AGENTS.md` edits were preserved.

The complete pre-upgrade overlay against official 0.12.5 contains 28 paths,
2,808 inserted lines and 29 removed lines. Most added lines are certificates,
tests and release records. Official 0.12.5 to 0.13.3 changes 670 paths, including
translations. See the [official comparison](https://github.com/open-webui/open-webui/compare/v0.12.5...v0.13.3).
No backend requirements or encryption source/artifact paths change between
these exact official releases; frontend dependencies do change.

## Active overlay decisions

No currently active behavioral customization is fully replaced by official
0.13.3. Preserve the narrow existing overlay, adapt it to the native release,
and keep retired historical implementations out of the upgrade.

| Behavior and original commits                                                | Current affected paths                                                                       | Decision and native coverage                                                                                                                                                          |
| ---------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Corporate trust, `c0d5a9cda`                                                 | `Dockerfile`, `corp-ca.crt`                                                                  | Keep. Upstream does not provide the corporate CA bundle or trust configuration.                                                                                                       |
| Context exclusions, `fa9a30a22` and subsequent release overlay               | `.dockerignore`                                                                              | Keep nested virtual environments, build output and test/cache exclusions.                                                                                                             |
| Original-only structured/binary files, `9952b1fb4`, `e1bdbe641`, `1d56dc057` | `backend/open_webui/retrieval/skip_extraction.py`, `backend/open_webui/routers/retrieval.py` | Keep format/MIME policy, permission checks and upload/batch hooks. Faster native spreadsheet extraction still extracts/indexes files; it does not replace Oxy's original-only policy. |
| Credential-owner identity/delegation, `bb9000791`, `2b9989cda`               | `backend/open_webui/routers/auths.py`                                                        | Keep `/api/v1/auths/verify` and strict delegation context. Native session-user routes are not substitutes for API-key, automation and subagent identity verification.                 |
| Local task cleanup despite Redis failure, narrow remainder of `2eccf2a3c`    | `backend/open_webui/tasks.py`                                                                | Keep. Native still awaits Redis cleanup before releasing local registries. The former broader custom Redis persistence implementation stays retired.                                  |
| Response-stream cleanup without a task ID, narrow remainder of `2eccf2a3c`   | `backend/open_webui/main.py`                                                                 | Keep. Native normal stream cleanup is partial coverage; its exception finalizer lacks the no-task fallback.                                                                           |
| No recursive automation management, `c840deadd`                              | `backend/open_webui/tools/builtin.py`, `backend/open_webui/utils/tools.py`                   | Keep management-tool exclusion and defensive create rejection for scheduled runs. New agent automations do not enforce this policy.                                                   |
| Defaults before send and duplicate-wait protection, `e81c2dfbd`              | `src/lib/components/chat/Chat.svelte`                                                        | Keep existing 11-line overlay. Native serializes default loading internally but does not await it at either submission boundary.                                                      |
| Compressed-image MIME, remaining narrow part of `c34e71b7f`                  | `src/lib/components/chat/MessageInput.svelte`                                                | Keep `blob.type`. Native still declares original MIME even when browser compression can return different bytes. Preserve native HEIC/image-drop improvements.                         |
| Removed Model Skills picker, `0660ff6dc`                                     | `src/lib/components/workspace/Models/ModelEditor.svelte`                                     | Keep intentional Oxy UX policy. Native still exposes the picker and saves model skill IDs. Hidden context messages are a separate change.                                             |
| Manual skill maintenance, `190acf344`, `0660ff6dc`, `996d52535`              | `scripts/clear_model_skills.py`                                                              | Keep selective backed-up cleanup/restoration capability. Do not run it as part of an upgrade.                                                                                         |
| Supporting validation and release evidence                                   | `pyproject.toml`, seven existing test paths, `AGENTS.md`, five existing upgrade records      | Retain. `fakeredis` is a development/test dependency; these are not duplicate runtime implementations.                                                                                |

The approved Escape fix adds `src/lib/shortcuts.ts` and
`src/lib/shortcuts.test.ts` to the retained overlay. The separately approved
incident fixes add runtime changes in `backend/open_webui/utils/automations.py`,
`backend/open_webui/utils/timers.py` and `backend/open_webui/utils/models.py`,
with regressions in `test/backend/test_timer_backpressure.py` and
`test/backend/test_models_default_builtin_tools.py`. Their behavior and limits
are recorded under Approved timer and model-default fixes below.

### Housekeeping candidates

These are proposals only; neither was removed during this audit.

- `FRONTEND_BUILD_MEMORY` in the maintained `Dockerfile`, introduced by
  `c0d5a9cda` and parameterized by `3294068b5`, sets `NODE_OPTIONS`. Native
  `package.json` explicitly runs the main Vite build with a 12 GiB heap, which
  overrides that setting for that process. Other Node subprocesses can still
  inherit the setting. Verify those before removing the knob; do not describe
  it as limiting the main build to 8 GiB.
- Infrastructure sets both `RAG_EMBEDDING_BATCH_SIZE=96` and legacy fallback
  `RAG_EMBEDDING_OPENAI_BATCH_SIZE=96`. The latter remains supported but is
  redundant because the former takes precedence. It can be removed in a
  separately reviewed infrastructure change.

## Historical patches that stay retired

Commit equivalence and current semantics were checked separately.

- Twelve recorded 0.11.4 upstream cherry-picks are ancestors of exact 0.13.3:
  PRs #31431, #31439, #31424, #31315, #31405, #31502, #31501, #31641,
  #31593, #31895, #31892 and #32029. Use native tool history, approvals,
  provider errors, inheritance, timeouts, caching and response handling.
- Thirty-two 0.12.0 backport commits remain patch-equivalent according to
  `git cherry -v upstream/v0.13.3 origin/0.12.0 upstream/v0.12.0`, including
  the fifteen commits from fork PR #2. Replay none.
- All eight post-0.12.2 hotfixes from fork PR #5 remain patch-equivalent:
  PDF export, leaving-chat scroll errors, Continue Response, imported tags,
  archived-tag preservation, knowledge-sharing preservation, API-key audit
  logging and the accompanying refactor. Replay none.
- Three older partial patches are not patch-identical: HTTP-safe UUID
  (`5619089fa`, completed by `03e4da470`), initial long-tool compaction
  (`f3780c738`) and skill-import modal lifecycle (`1c371a8d3`). Their broader
  native replacements were already adopted; keep native behavior.
- The 0.12.3/0.12.4 reductions remain retired: custom configuration snapshots
  and cache TTL, custom Redis stream encoding/deltas, note seeding,
  required-field sorting, extra progress events/image filter/reducer changes,
  regeneration/provider-error overlays, knowledge pagination, document-MIME
  editor workaround, Markdown fade rewrites, initial title-model context and
  parameterized-prompt rewriting. Their former paths have no active overlay.
- The deferred/provider adapter experiment `9e2e7b128` was reverted by
  `73ec2ad6f`. Keep native Tool Search dispatch/history and new batching.
- Archived PRs #26247, #25718 and #22249 should not replace native automation
  session propagation, pending-knowledge indexing or `knowledge_ids` lookup.
  Archived #21144's forced full-context branch was not deployed; current
  configurable preload/full-text controls are not identical to it.

One historical coverage gap remains: terminal-discovery retry `e0c760b7d`,
retired in 0.12.4, is only partly native. Native retries an entirely empty
cached terminal list. If terminal A succeeds and B fails, it can retain the
nonempty list containing A and report B unavailable without the old retry.
This is already the deployed source behavior, not an active patch newly eligible
for retirement. Do not restore an old patch automatically or claim complete
native equivalence for this particular historical behavior.

## Captured deployment and configuration baseline

At the initial baseline capture, all four services used approved maintained
0.12.5 image digest
`sha256:83f92c311fb924bec6085e8be7992ebad2a44cbdc80022abc13459c7b492f522`.

| Environment | Serving definition/capacity            | Ingestion definition/capacity               |
| ----------- | -------------------------------------- | ------------------------------------------- |
| Dev         | `oxygpt-open-webui-task:453`, 2 tasks  | `oxygpt-open-webui-ingest-task:35`, 1 task  |
| Production  | `oxygpt-open-webui-task:454`, 12 tasks | `oxygpt-open-webui-ingest-task:36`, 2 tasks |

Each application container has 149 explicitly configured ECS environment entries
and four secret references; image/startup variables add others. Secret values
were not included in the audit output. The
infrastructure `open-webui/Dockerfile` contains only the immutable maintained
image `FROM`; it adds no hidden application patches.

An AST inventory of native backend environment reads found zero removed reads
from official 0.12.5 to 0.13.3. Every current non-telemetry application variable
is directly read by native Python except `PORT`, consumed by the startup script.
No existing variable requires replacement because its read was removed.
This establishes source compatibility, not candidate runtime validation.

Shared environment seeds are in infrastructure `terraform/open-webui-config.tf`;
ingestion overrides are in `terraform/open-webui-ingest.tf`. Serving/ingestion
differences cover process pools, threads, logging and telemetry names; dev/prod
also differ in intentional login/signup policy.

`ENABLE_PERSISTENT_CONFIG=true` means saved configuration overrides environment
seeds. Read-only transactions against both databases found:

| Setting                      | Environment seed | Saved dev                                                       | Saved production              |
| ---------------------------- | ---------------- | --------------------------------------------------------------- | ----------------------------- |
| Main tool-loop rounds        | 100              | Process setting                                                 | Process setting               |
| Retrieval top K              | 35               | 10                                                              | 10                            |
| Reranker top K               | 12               | 12                                                              | 6                             |
| Chunk size                   | 1400             | 1400                                                            | 1400                          |
| Upload size limit            | 100 MB           | Unlimited/null                                                  | 500 MB                        |
| Upload file-count limit      | 25               | Unlimited/null                                                  | Unlimited/null                |
| Skill content/preview limits | 50,000 / 5,000   | Unlimited/null                                                  | 50,000 / 5,000                |
| Subagents                    | Unset            | Enabled, including background                                   | Enabled, including background |
| Subagent caps                | Unset            | 50 concurrent / 50 async / 30 rounds / 10,000 output characters | Same                          |
| Tool Search                  | Unset            | Disabled                                                        | Disabled                      |
| Tool approvals               | Unset            | Disabled                                                        | Disabled                      |
| JWT expiry                   | Unset            | `-1`                                                            | `4w`                          |

These existing differences were preserved. New native retrieval, chunking and
unlimited-subagent defaults do not replace these saved settings. Preserve the
explicit 100-round process cap. Environment changes alone would not reconcile
the saved differences above.

### Attachment and model defaults

Both saved global model-default dictionaries already set `file_context=false`.
Stored-profile capability inspection, merged with those global defaults, found:

| Environment | Stored profiles | Preload enabled | Preload disabled |
| ----------- | --------------- | --------------- | ---------------- |
| Dev         | 55              | 22              | 33               |
| Production  | 1,049           | 535             | 514              |

No stored profile relies on an absent `file_context` default after that merge;
the new native default alone should not broadly flip Oxy preloading. Explicit
true values remain true. These are stored-profile counts, not each user's live
visible-model list or an end-to-end guarantee.

Thirteen existing production profiles combine preload off with unavailable
built-in file tools: three explicitly disable Files, four disable builtin tools, and
other cases disable file uploads, with overlaps possible. Preserve their intent
and validate representative cases; do not bulk-rewrite models. External or
terminal tools may provide other file access.

Preload, extraction and retrieval are separate. Preserve original-only upload
hooks and test byte-preserving download, attachment use, knowledge linking and
retrieval on the candidate.

## Configuration choices and unchanged preferences

No new environment variable is mandatory for source compatibility.

| Setting or feature                | Proposed treatment                                                                                                                                                                                                                           |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ENABLE_DB_MIGRATIONS=false`      | Preserve steady-state setting. Run a reviewed coordinated migration separately before candidate services use the schema.                                                                                                                     |
| `TIMER_MAX_CONCURRENT`            | Approved code default is 2 per worker, reserved before timer claims and held through the returned response futures. No extra environment seed is required. This is not a fleet-wide limit.                                                   |
| Existing subagent caps            | Preserve saved 50/50/30/10,000. Optionally mirror as environment seeds for recovery consistency; this does not overwrite saved configuration.                                                                                                |
| `showInputSelections`             | Recommend enabling visible selections in interface defaults. Both saved default-interface dictionaries are `{}`; environment seeding alone will not replace them or explicit user preferences. This setting change is proposed, not applied. |
| `RAG_FULL_TEXT_CHAR_THRESHOLD`    | Review new 50,000-character default, absent from current saved config. Test small-document context/cost. Use 0 only if deliberately choosing retrieval for small files.                                                                      |
| `ENABLE_COMMENTS`                 | Native default false. Enable only as a deliberate feature choice.                                                                                                                                                                            |
| `ENABLE_NOTIFICATIONS`            | Native default true; the Inbox is an automatic user-visible addition unless deliberately disabled.                                                                                                                                           |
| `USER_PERMISSIONS_CHAT_SIDE_CHAT` | Default false. Saved permissions lack the new key. Review effective permissions rather than assuming an env seed replaces a saved dictionary.                                                                                                |
| `ENABLE_TOOL_PERMISSIONS_AUTO`    | Default false. Enabling requires a reviewed scoring endpoint/model/key and changes tool approval behavior.                                                                                                                                   |
| `ENABLE_TOOL_CALL_LABELS`         | Optional, default false.                                                                                                                                                                                                                     |
| `TOOL_SEARCH_ENGINE`              | Native default `bm25`; Tool Search is currently disabled. Extra scoring credentials are unnecessary unless an optional engine is chosen.                                                                                                     |
| `KNOWLEDGE_MAX_*`                 | Optional new collection/file/upload limits. Review existing sync needs before imposing limits.                                                                                                                                               |
| `VIEW_CHAT_MAX_CHARS`             | Optional limit on model chat-history reads.                                                                                                                                                                                                  |
| `SESSION_EXPIRES_IN`              | New renewable-session default `90d`, separate from existing JWT policy. Test native-client sessions and Oxy delegation/revocation.                                                                                                           |

Automatic behavior also includes Integrations moving into the composer `+`
menu, hidden selection indicators unless enabled, a tabbed/split workspace,
saved side chats when permitted, hidden tool messages for memory/skills/knowledge
context, and removal of file-processing-failure webhook notifications. Validate
custom pipes/filters against the changed message layout and retain ingestion
observability independent of the removed webhook event.

Microsoft OAuth variable names remain supported. Test full SSO roundtrips and
expired sessions; new native `sid` session validation deserves separate revoked,
expired, API-key, automation and delegated-user checks.

## Schema and validation plan

Both databases reported schema `0bd252b206fb` at the baseline capture. Six new revisions lead to
native head `9f6c2a8d1b43`:

1. `c41f8d9e2a10`: resource comments/reactions.
2. `6f8c2a91d4e7`: tool activation state.
3. `e42c8f710a36`: notification inbox.
4. `8bc7e5a902d1`: authentication sessions.
5. `6a23bd107e54`: per-user chat workspace state.
6. `9f6c2a8d1b43`: channel lookup indexes.

Official [0.13.0](https://github.com/open-webui/open-webui/releases/tag/v0.13.0)
and [0.13.1](https://github.com/open-webui/open-webui/releases/tag/v0.13.1)
explicitly warn that mixed-version rolling updates are unsupported. Do not
assume the prior image-only rolling handoff is compatible. Before deployment,
review schema operations and stored-data writes, rehearse migration on a
disposable restored database, establish recovery evidence and a coordinated
old/new-worker transition, and preserve active chats/uploads/automations and
parent/subagent families. An image rollback alone must not be assumed to undo
new schema or data writes.

The approved application scope is exact official 0.13.3 plus the retained narrow
overlay, Escape fix and the timer/model-default changes recorded below. Native
dependencies and native implementations of retired patches remain. Source
integration and isolated checks are complete. The planned image/dev verification
scope covered startup/regressions, authentication/delegation, real tools,
model switch/double-send, originals/knowledge, MCP identity, native timers and
subagents, and deployment provenance. Completed checks are recorded below.
A fresh interactive corporate identity-provider login and undocumented live
automation, context-compaction, reconnect or incident-pressure scenarios must
not be inferred from the passing checks; they remain separate promotion or
incident-specific reviews. Production deployment is outside this authorization.

## Escape fix and checks completed

Local application commit `a060293fca6b80a314961abe0ec9edd21f1f4e16`:
`Disable Escape shortcut for clearing chat input selections`.

- `src/lib/shortcuts.ts`: clear-selection default is unassigned and shortcut
  metadata advertises no default key. Loading a saved legacy `Escape` binding
  normalizes it to unassigned, covering users whose old full shortcut map was
  saved when another binding changed. Other custom clearing shortcuts remain.
- `src/lib/shortcuts.test.ts`: regression coverage for new defaults, legacy full
  maps, custom alternative bindings, disabled bindings and reset defaults.
- Escape response stopping and dialog closing are unchanged. No saved database
  settings are rewritten; normalization occurs when user shortcuts load.
- All ten frontend tests pass with both Linux and Macintosh navigator
  simulations using Node 22.23.4, Vitest 1.6.1 and Svelte 5.53.10. Standalone
  test configuration used pinned existing tooling without building the app;
  this is not a full Svelte/application build or candidate-image validation.
- Changed TypeScript formatting, `git diff --check` and native encryption
  artifact/checksum verification pass. Independent review has no remaining
  findings. No backend code changed in this commit.

The Escape commit was prepared on maintained 0.12.5 and carried into the
approved 0.13.3 integration. The upgrade branch and
[fork PR #14](https://github.com/jimbo-p/open-webui/pull/14) are now published.
The initial standalone checks above are supplemented by the complete frontend
checks recorded below.

The initial Escape investigation created no application objects or cloud
resources. Its remote configuration probes used read-only transactions and
allowlisted output, and its temporary validation directory was removed. Later
release-validation resources are tracked separately below.

## Approved dev release execution

The user approved the audited 0.13.3 application transition, build and actual dev
validation on November 9. Production remains outside this execution scope. The
build-memory knob and all active Oxy customizations remain; no optional interface
settings, model metadata or existing timer records were rewritten.

Application integration commit `c42ab69755fa35c89af58ac798a5e90832223870` merges
exact official 0.13.3 while preserving fork ancestry and the Escape commits. All
5,300 native paths outside the approved overlay match exact official blobs. The
reviewed archive SHA256 is
`6813c85d0dcd673a55bc6b4eb524abf4887ff82a49d19a05fb9650e2d9137dcc`.
An initial full-CPU image build used that committed archive on `permission-test`,
with its exact SHA in `BUILD_HASH`, `BUILD_CHANNEL=main`, and slim/CUDA/Ollama
all false. That source is superseded as the final release candidate by
`7e5fa4c5902d84d3b0d3c946bb5ebc8917d08486`. Its final image-build archive is
16,040,979 bytes, SHA256
`4ceb398a4fa651af1028f6883efee0a9537a0c5ed1a4135fef983ba35ab1369c`,
and its newline-serialized build-relevant source manifest is
`24a391da55ac381685d811d793e5eae9010006687dd58ee94edb0a277c6aa6e8`.
The deployment helper serializes the same Git paths, modes and blob IDs with
NUL separators and records
`7e431f97e07051af40e75c244fcb590f61ffb23e28b1c40ee1a9d78904004e69`.
Both manifests exclude only root `AGENTS.md` and `docs/`; all 5,325 retained
entries match between image source `7e5fa4c59` and documentation commit
`9a264f31bd02fb9b177195704e8d87417d4a174d`.

The final full-CPU build uses that exact source SHA in `BUILD_HASH`,
`BUILD_CHANNEL=main`, and `USE_SLIM=false`, `USE_CUDA=false`,
`USE_OLLAMA=false`. The published tag is `open-webui:v0.13.3-oxy1`; its immutable
ECR manifest digest is
`sha256:31066fb1b07a785942cd4d36045c13e4df9e41748fc4c2aac0acfdd2ca08feab`,
and its tested image configuration digest is
`sha256:e1461427a34ae69984b07f1edfa37c12034fbe42590a9bc5d28d6deefc677df9`.
Registry readback and an immutable pull verify that configuration, all 21 rootfs
layers and the source label match the tested artifact. Later documentation-only
commits do not replace `7e5fa4c59` as the image source. Live dev evidence must be
recorded against this digest before declaring development validation complete.

Frontend validation on Node 22.23.4 passes all 14 tests, native encryption
verification, full formatting and i18n extraction/placeholder checks. A disposable
archive comparison verifies all 5,331 tracked source files unchanged after the
mutating native checks.

Backend validation uses isolated SQLite/data/static directories and the compatible
Python 3.12 environment. The first source run exposed three outdated regression
fixtures: native uploads now check knowledge limits before linking, and native
compaction accepts form data and returns compacted messages. Commit `8bc2fdb25`
adapts those fixtures while preserving assertions about original-file handling,
durable linking and tool-pair retention. All 102 backend cases then pass. No
application runtime changes were needed for these fixture adaptations.

### Migration rehearsal and continuity evidence

An owned, disposable PostgreSQL 17.7 container was created from a cached image
with a 768 MiB memory cap. Native historical migrations created the exact old
schema; synthetic data, not customer data, was used. No live database was changed
for these tests.

- The native single-transaction upgrade was reproduced holding the tool table's
  exclusive lock while later channel indexes waited. A tool read timed out. A
  separately induced 500 ms lock timeout rolled the full upgrade back to the
  old head, with no partial new schema.
- Running the six official revisions individually with Alembic connection
  `lock_timeout=1s` and `statement_timeout=10s` succeeds. With dev-sized tables
  and continuous synthetic writes, 4,208 tool/message writes completed with
  zero errors; the maximum observed latency was 42.2 ms. The tool-column ALTER
  took 9.82 ms; the three channel index statements took 4.15/1.66/4.09 ms.
  Python/import startup accounts for most of each command's 5.66–8.20 seconds.
- Old and new ORM create/read/update operations and native getters work against
  the expanded schema. Existing columns/types/nullability/defaults remain
  unchanged; five new tables, the tool flag and valid new indexes are present.
- An old migration-enabled startup cannot resolve the new revision. An old-image
  rollback must keep migrations disabled and retain expanded schema. A schema
  downgrade destroys new feature tables/data and is not an automatic rollback.
- This is evidence for a controlled expansion and traffic handoff, not support
  for ordinary mixed-version request routing. New temporary-chat identifiers,
  renewable-session endpoints/revocation, tool activation and knowledge limits
  have version-specific behavior. All new traffic must move to the new fleet
  while captured old requests drain. Routing alone does not stop old workers'
  background scheduler acquisition.

Read-only live size observations found only eight dev channel messages and no
production channel messages/reactions at that snapshot, so index targets are
small. Production conditions must be rechecked before any later production
migration; rehearsal timings are not a zero-downtime guarantee.

### Applied dev schema expansion and native registration

The exact six official revisions were applied to `oxygpt-dev-cluster`, one
revision per transaction with `lock_timeout=1s` and `statement_timeout=10s`.
The run completed in 10.18 seconds, moving head `0bd252b206fb` to
`9f6c2a8d1b43`. It verified the five additive tables, `tool.is_active` and three
new indexes. Configurations (452 rows), model rows (58 including three owned
QA profiles at that checkpoint), and five notes had identical before/after
fingerprints. A verified recovery capture preceded the change; old and new
workers retain startup migrations disabled. Production schema was unchanged.

During the expansion, 47 observation sets recorded 141 typed HTTP probes from
20:19:31 to 20:21:04 UTC. All succeeded: maximum readiness/version/ingestion
latencies were 36/22/24 ms, and the public dev version remained `0.12.5`.
This observes the schema expansion, not a completed traffic handoff.

Native 0.13.3 startup subsequently registered 22 new configuration keys, bringing
the total to 474. A read-only dev check at 20:47:15 UTC confirms all original
452 configuration values and 55 existing model rows are preserved. The new keys
cover session expiry, tool labels/approval/search configuration, chat view limits,
comments, knowledge upload limits, notifications and the full-context threshold.
Registration adds the release's keys; it does not justify rewriting existing
preferences or claiming every new feature is enabled.

### Approved timer and model-default fixes

After reviewing the concrete additional changes, the user approved the fixes
and development validation on November 9. Commit
`7e5fa4c5902d84d3b0d3c946bb5ebc8917d08486`, based on `38bb12fef`, changes
exactly three application files and two regression files.

The incident investigation identified timer executions loading a large parent
conversation before admission and accumulating without a scheduler concurrency
bound. Exact official 0.13.3 retains that mechanism; native subagent caps do not
govern timer dispatch. The approved changes preserve timers, automations,
explicit subagent choices and queued work:

- `utils/automations.py` reserves timer capacity before claims. The positive
  `TIMER_MAX_CONCURRENT` default is two per worker. A dispatcher retains its
  slot through preparation and returned local response futures, including when
  that dispatcher is cancelled; it does not cancel the response to free space.
- `utils/timers.py` claims only free capacity and at most one due timer per
  parent within each batch. Busy local/Redis parents and PostgreSQL row-lock
  contention defer the matching claim with a short jittered retry. The original
  due time, prompt, tools, files, filters and cancellation rules remain stored.
  An ID-only parent row lock and claim check precede full parent JSON loading.
- Parent delivery, normalized messages and a claim-associated response marker
  commit together. Terminal state changes and rearming are claim-fenced.
  Handled failures after delivery preserve a registered response or store a
  visible terminal error in both message stores; they do not replay that turn.
- `utils/models.py` merges global `builtinTools` defaults by category, with
  explicit model overrides winning. A missing `subagents` entry now inherits
  the configured global false value instead of defaulting to true. Explicitly
  enabled models remain enabled. Saved model rows are not rewritten.

The bound is per worker, and parent fairness is per batch; neither is a global
one-active-parent exclusion or an absolute memory guarantee. A short handoff
window between parent commit and response registration still exists across
workers. One admitted large conversation can still require substantial memory.
Historical running timers are not automatically replayed, deleted or cancelled.
Exactly-once external effects after a hard process death are outside this fix.

### Final source validation

All 119 isolated backend regressions pass under Python 3.12: the 102 retained
cases and 17 new timer/default-inheritance cases. Coverage includes admission
capacity, busy-parent JSON avoidance, fairness, stale claims, atomic message
publication, socket/handler/cancellation failures, preservation of a live
response, and explicit model overrides.

Nine retained maintenance regressions pass against the owned disposable
`owui_skill_test` PostgreSQL database. The fixture asserts loopback access and
that exact database name before resetting its synthetic schema. No shared or
live application data was used. The database was removed afterward and its
absence verified; the separate migration-rehearsal database was preserved.
Native Ruff formatting passes for 374 files, and the exact CI F-rule checks pass.

An independent PostgreSQL 17.7 probe used 40 synthetic timers, two parents and a
50 MiB uncompressed parent. Concurrent claims were unique, honored capacity and
included the other parent. A locked-parent dispatch returned in 39.9 ms with
zero full-parent JSON reads and 128 KiB maximum RSS growth in that probe. Stale
claim rearm/error/delivery attempts were no-ops. A postcommit socket failure
stored a visible error and `done=true` in both parent JSON and normalized
messages without replay. All owned timer, parent and normalized fixtures were
removed and removal verified. These source-level probes mock model/socket
services; the final-image rerun below retains that boundary. Live provider/tool
integration and deployment pressure checks remain separate requirements.

The five committed application/test files match the source archive used for
these regressions, SHA256
`6a14fb644c37f3d21051bb8a3339396fbd97203a16c99586d99d02ed005ba022`.
That validation archive was assembled from base `38bb12fef` plus the five
reviewed files; it is not the final image-build archive.

GitHub checks on source commit `7e5fa4c59` pass:
[Frontend Format & Build and Unit Tests](https://github.com/jimbo-p/open-webui/actions/runs/42688408020),
[Python formatting and logic on 3.11/3.12](https://github.com/jimbo-p/open-webui/actions/runs/42688407794),
and the [Tests result check](https://github.com/jimbo-p/open-webui/actions/runs/42688409283).
The Tests Suite job was explicitly skipped; the 119 backend and nine maintenance
regressions above were run separately.

### Final immutable-image validation

The full-CPU image above passes content/source verification and `pip check`.
Inside that actual image, all 119 backend regressions pass without skips in
85.7 seconds and all nine maintenance regressions pass in 14.4 seconds against
the owned disposable PostgreSQL database, which was then dropped and verified.
The test fixtures needed existing SQLite data/static directories; correcting
those isolated paths required no application change.

The actual-image PostgreSQL probe repeats the 40-timer/two-parent test with a
50 MiB parent. Concurrent claims are unique and fair, stale claims cannot
rearm/error/deliver, and postcommit failure is visible in both message stores
without replay. The locked parent defers in 25.0 ms with zero full-parent JSON
reads and 128 KiB maximum RSS growth in that probe. All synthetic timer, parent
and normalized message fixtures were removed and removal verified. Model/socket
services remain synthetic; this is not a live provider or tool check.

A native startup with isolated data becomes ready in 35.3 seconds; `/health` and
`/ready` return JSON `status=true`, `/api/version` reports `0.13.3`, and the
frontend serves HTML. It runs the full-CPU image, exits with code zero without
OOM on the reviewed stop, and its disposable startup container is removed.
The evidence binds these results to the exact source/configuration/registry
digests above. It does not establish a bound on every conversation's memory.

### Infrastructure helper targeting correction

The initial infrastructure helper review missed a hardcoded production RDS
cluster and password-secret selector in `cutover.activity()`. During dev warmup,
that function combined dev Redis observations with production SQL metadata.
The SQL connection was read-only and no SQL writes were performed. The error
was found before cutover or cohort capture; the invalid warmup/file observations
were not accepted as dev drain evidence. This was an operations-helper error,
not an application image change.

Infrastructure commit `ad2e3990324dee4a64d57a5548288743163aa813` maps the exact
reviewed dev/prod ECS clusters to their matching RDS cluster and secret, rejects
unknown scope, and records the selected database in activity evidence. Both
actual connection paths are covered by mocked target regressions. The synthetic
helper run passes 118 cases with seven PostgreSQL cases skipped (125 total).
The previous 123-case helper suite passed with those seven read-only PostgreSQL
cases enabled; the SQL itself is unchanged by this two-case target-guard fix.

The invalid warmup and historical-file fields were independently verified and
recomputed against the exact 38 dev file IDs. At that repair point, all other state values and the
then-empty cohorts remained unchanged. Original warmup time/epoch is preserved exactly,
so recomputation does not extend the temporary scheduler window. Only the
infrastructure source pin and configuration binding advanced; application
source/image remain `7e5fa4c59` and digest `31066fb1…`. The prior raw-state/config
archives, both shadow copies and the local invalid prewarm copy were removed
and absence verified. The valid state and sanitized hash/count/provenance record
are retained. Other dev-reachable DB/Redis/log/notes/controller/cleanup targets
were audited; no additional production target was found.

### Initial QA staging and continuity attempts

The first old-chat HTTP 400 is confirmed as a private QA fixture access-policy
error: its provider base was available to administrators but had no registered
model row, so the inherited base-model policy denied the non-admin QA user.
Only the private QA preset's base was changed to a registered model accessible
to that user; existing profiles and access grants were unchanged. A short
replacement call returned HTTP 200 with real chat/task IDs, executed the timestamp
tool with the QA identity, and completed scoped cleanup. The current 474-setting
and 57-model baseline was preserved, alongside the original 452-setting/55-model
subset checks. This does not yet establish full handoff continuity.

Separately, both 0.12.5 and exact 0.13.3 resolve ordinary custom profiles through
`app.state.MODELS` and reject an absent profile. That registry can be Redis-backed
or process-local depending on runtime configuration. Native catalog construction
populates it before user-filtering the API response. New private QA fixtures are
refreshed and visibility-checked on each serving worker as preparation; a catalog
refresh does not grant access to an unregistered or restricted base model.
This baseline registry constraint is not the confirmed cause of the HTTP 400 and
is not an upgrade fix.

The first owned 64 KiB CSV upload completed in 90.267 seconds without a
cutover in its window, so its timing proof correctly failed. Its exact file row
and S3 object, and the failed chat probe's owned QA objects, were removed and
verified.

The retry passes upload continuity: one request started at 21:13:41.359 UTC,
spanned the recorded 21:14:36.809 cutover, and returned at 21:15:11.594 after
90.235 seconds. Its response request ID matches the exact captured old ingestion
worker's HTTP 200 access record (90.232 seconds, 66,419 request bytes). The
65,536-byte original retains its SHA256 and download filename, and processing
completed. Its file row and recorded S3 object were removed and absence verified
at 21:15:16.798. This proves one owned upload's continuity; chat/tool continuity
and the remaining live integration gates require their own evidence.

### Historical first-cutover checkpoint

At 21:14:36.809368 UTC, the helper moved ingestion then serving traffic to
the full-capacity temporary 0.13.3 fleet. Route proof at 21:15:19 attributed
unique requests to its exact tasks. The old canonical workers were deliberately
retained at that checkpoint; their later actual exits are recorded under the
completed handoff. Cutover alone did not establish worker exit.

The four-minute first-cutover window contains 118 samples per path, or
354 typed checks, all HTTP 200. Readiness median/max is 11/23 ms, version 6/11 ms
and ingestion readiness 5/13 ms; maximum gap is 3 seconds. Last sampled 0.12.5
is 21:14:42 and first 0.13.3 is 21:14:44, about 7.19 seconds after the routing API.
No later old-version sample appears. This measures sampled endpoint
availability and asynchronous propagation, not every user request. Summary
SHA256: `e51e1cf9abbe5c3569417cfeb0d80962d976601671aedfdb14e9fbbe794d0197`.

The temporary workers used a one-day timer poll interval while old workers
retained normal scheduling. Native isolated timer tests were already passing;
the live canonical timer response required its later, separate check.
Managed Get-Identity resolved the existing API-key owner without supplied
identity fields. Three Databricks MCP catalog 502s matched 0.12.5. This was not
a fresh interactive IdP login or a business query.

The owned 90-second chat completed before the actual first cutover, so that
timing assertion failed. Its already-terminal task/assistant were deleted by
scoped QA cleanup. A finite state-only review removed exactly that completed
owned work from active drain expectations, preserving capture history, chat
anchor and all 38 file checks. It introduced no generic missing-row exception
and did not turn the failed continuity assertion into a pass. Proposal SHA256:
`5432795419122512528a9519ccf1c9914148c2613090083598a8c61d38cc1d11`.

Early QA fixture corrections used actual dev defaults (subagents unset,
calendar false), a supported `en-US` browser locale instead of the headless
host's invalid `en-US@POSIX`, an explicit private knowledge read grant, and
the native completed-upload ordering. They changed no application/global
setting or existing model/knowledge permission. Diagnostic errors remained
failures until corrected runs passed. The native locale/timestamp source is
identical in 0.12.5/0.13.3; invalid-locale handling is not claimed fixed. The
earlier immediate pre-processing download 404 has no confirmed exact cause;
the corrected ordinary-flow check requires one 200 without a download retry.

At that historical checkpoint, 38 specific unfinished dev uploads blocked
retirement despite existing originals, unchanged metadata/xmins and no
observed active work. Age and absent links could not exclude an untracked
retry. The subsequent explicit scoped user approval, unchanged-object proof,
actual retirement and preserved records are recorded below. A dev-only
scheduler observation 21:37:34 had no timers/due work, with the next active
automation December 1 at 15:00 UTC; production schedules require their own review.

The owned skill-test database, isolated PostgreSQL fixtures/container, interim
image and exported image tar were removed with verification after their
purpose ended. Final/rollback images and required recovery inputs remain.
Current live-resource removal and asynchronous metadata retention are covered
in the final cleanup record; this checkpoint is not a final clean-state claim.

### Read-only production capacity planning

Production remains on web definition 454 (12 tasks, 2 vCPU/16 GiB each) and
ingestion definition 36 (two tasks, 1 vCPU/8 GiB each). Both preserve
100% minimum healthy and 200% maximum deployment capacity. A full temporary
fleet plus the canonical rolling replacement therefore has an upper bound of
42 simultaneous tasks: 28 more than the normal 14, or an additional 52 vCPU
and 416 GiB of task memory. Planning only for the 14 temporary tasks would
understate that handoff peak.

Read-only checks around 21:36 UTC found a 4,000-vCPU regional Fargate On-Demand
quota, recent account usage of 231.25–244.25 vCPU, and 29/25/16 available
addresses across the three configured production subnets. The aggregate
70 addresses accommodates the estimated 28 additional task addresses, but
per-zone placement, spare-address margin and concurrent workloads must be
rechecked immediately before an approved production handoff. Warmup must also
verify database/Redis connection and latency headroom. The current native
15-second scheduler safety check does not universally cover the entire
canonical warmup; running parent/subagent families and due schedules require
a production-specific risk and drain review. These checks create no production
resources and do not authorize production deployment.

### Corrected temporary-fleet browser validation

Run `341837aa71614488` passes the strict browser scope at 21:55:59 UTC
with supported `en-US`: nested-default inheritance without stored rewrite,
idle Escape preserving tools, immediate model-switch/double-Enter producing
one submission, correct QA identity in the real timestamp tool, original
composer CSV bytes after native processing, Escape-to-stop, the next same-chat
tool call retaining selections, and a completed foreground subagent/parent
result. Zero uncaught page errors are observed. Timer creation is explicitly
deferred before any prompt in this temporary-worker scope. API file/knowledge
checks also pass: CSV/XLSX/DXP/TXT originals retain bytes and filenames, native
knowledge membership is intact, and the unique token is returned from actual
document text rather than filenames/metadata. Credential-owner verification
passes. Native logout and scoped cleanup finish at 21:56:16 UTC: all 18
tracked owned tables have zero rows and all five S3 objects are absent. The
original 452-setting/55-model values and the fresh 474-setting/61-model
baseline are preserved; the additional models belong to other Gateway work
and are untouched. Production definitions/images/schema remain unchanged.
The temporary scope passes, with release_complete=false and canonical timer
validation explicitly deferred.

The prior immediate post-POST download HTTP 404 remains a failed diagnostic,
not a retroactive pass. Native composer upload waits for process/status SSE
completion before marking an attachment usable; the test's direct response
interception had attempted download earlier. The provider, processing-cache
cleanup and content-serving code are unchanged from 0.12.5. Shared local-cache
unlink timing is a plausible source explanation, but the exact earlier 404
cause was not captured and is not claimed confirmed or fixed. The corrected
normal-flow test requires one HTTP 200 and matching SHA/filename after native
processing, without a download retry.

The i18n locale conversion and bounded timestamp formatter source are identical
in 0.12.5 and 0.13.3. Node 22 independently reproduces the same RangeError for
both POSIX spellings and accepts `en-US`; this is an unchanged invalid-locale
limitation, not a candidate-specific formatter change.

### Old-client and legacy-token compatibility

Serial run `a0622e1787334575` passes at 22:00:03 UTC. A real token issued
by the exact captured 0.12.5 task/image, without `sid`, authenticates as its
own temporary QA user through the actual 0.13.3 backend. Credentials remain
in memory and the ordinary QA token/cleanup flow stays intact.

A separate browser scope loads 208 whitelisted old HTML/static resources while
35 API requests and one native socket use the real new ALB backend. It executes
one private timestamp tool with the correct identity, receives one completed
assistant without error, displays the result, and has zero uncaught page errors.
No browser authorization header is forwarded to the old static source. The
asset-version manifest is deliberately held old and service workers are blocked
for this compatibility fixture; native automatic refresh is not tested. This
proves old-loaded-client/new-backend compatibility, not an in-flight chat
crossing the historical route switch.

All 18 tracked owned row categories are zero after native cleanup; original
and current settings/models and production remain unchanged. No owned QA user
or job remains active. The corrected temporary functional scope and this
compatibility scope both pass while release_complete remains false.

### Scoped unfinished-file retirement approval

The user explicitly chose “Allow scoped dev retirement” for the exact 38
unchanged unfinished uploads from February–April. This accepts the possible
interruption of an untracked indexing retry in the old and temporary dev
fleets. It is not a finding that indexing completed, not a production exception,
and not permission to rewrite statuses, files or saved settings. Original
objects, file rows and error history remain intact.

A fresh proposal verifies all 38 metadata/xmin fingerprints against the original
snapshots and all 38 original-object HEAD checks. The finite review binds the
original warmup time, old cohort creation/seal and actual initial cutover, and
allows only old/temporary captured membership. A changed or missing fingerprint
still blocks retirement. The proposal SHA256 is
`39426cf9824e2d9963ccff564b04b9b9700a66980eb777e7b6a60d400eb016af`.
Independent verification confirms exactly two controller-state additions;
existing state, earliest warmup epoch and config SHA `05e4206f…` are preserved.
The native retirement gate then passes with no other work blockers.

At 22:20:57 UTC the canonical dev service update is accepted for web definition
455 and ingestion definition 37, retaining desired capacity 2+1 while the
temporary fleet serves. At 22:24:39 all three new targets are healthy. The exact
old workers were then draining under the preserved 300-second connection window.
Actual exit, return continuity and normal-polling timer checks were still pending
at that historical evidence point; their completion is recorded below.

### Completed dev handoff and canonical timer validation

All three captured old 0.12.5 tasks are verified STOPPED at 22:31:29 UTC;
all nine containers exit zero. The canonical replacement remains web455 at
2/2 and ingestion37 at 1/1 on digest `31066fb1…`.

The first owned return probe, `c01aa57200bc47eb`, fails before any RETURN or
strong-ready marker because its diagnostic log reader moves the end boundary
during pagination. A later read-only replay reproduces empty pages with
changing tokens in all six exact streams. A fixed boundary exhausts those
streams in three pages; genuine 20-page truncation remains a failure. This is
a test-helper correction, not application code or an image change. The failed
probe stays failed. Its own 120-second native response completes normally;
uncaptured/terminal checks precede its scoped native cleanup.

Fresh probe `39ecce5060a248f6` uses reviewed helper SHA256
`41c72bb6f387cf10cfa08701fe330a4ced5874f0f458e1777dc641b9d724412b`.
Strong readiness at 22:39:48 UTC leaves 112 seconds on the conservative hold
budget. Actual RETURN occurs at 22:40:30.517392, ingestion then web. Nine
bounded, uniquely named native HTTP probes all return typed 200 while the same
job is live and its assistant unfinished before and after each request. Three
initial TEMP observations document propagation. The selected probe at
22:40:49.277 has exactly one captured canonical owner and zero TEMP matches.
The bound controller proof completes at 22:41:32.359. The original browser
shows the one final assistant at 22:41:43.192, with one POST, one actual
120-second tool call, done=true and no error. This establishes same-version
in-flight chat continuity; it does not retroactively establish the missed
0.12.5-to-0.13.3 chat crossing.

The TEMP cohort is sealed after canonical route proof and drains with the
38 exact approved file reviews, no other outstanding work, and at least
60 seconds of quiescence. Retirement begins at 22:45:51.887. All three TEMP
tasks and nine containers are verified STOPPED/exit zero at 22:52:55. A full
dev-cluster inventory, including pending/stopping/standalone candidates, finds
only canonical web455(2) and ingestion37(1), all at the tested digest. The
38 original objects and metadata/xmin snapshots remain unchanged. Only after
that proof does the operator release the retained QA records for cleanup.
The full continuity scope and native cleanup pass at 22:54:14; all 18 owned
row categories are zero and original/current fingerprints are preserved.

Canonical run `e3a59dfe19604c14` passes at 22:57:11 with no deferred checks.
It repeats ESC preservation/stop/next-turn tools, immediate model switch and
duplicate submission, identity, zero uncaught browser errors, originals and
actual knowledge retrieval, and foreground subagents. Its single native timer
publishes the expected exact parent response, and that assistant is verified
done=true/no error with the actual completion marker at 22:56:56. Checking
only timer metadata status would be insufficient; the final response itself
is checked. All three canonical workers use one Uvicorn process, approved
image defaults of two concurrent timer dispatches per process, and normal
one-second polling; no timer environment override was added.

The original 452 configuration values/55 model values and this run's current
474-value/59-model baseline are unchanged before and after. The later two missing model IDs between serial baselines belong to the known
Gateway namespace; no actor or mechanism is inferred. They are outside the
original 55, and no such models were restored or edited by this task. All 18 owned database categories are zero
and all five owned S3 objects are absent after native cleanup.
Real-owner managed Get-Identity through canonical succeeds at 22:58:29 without
an injected identity. Three Databricks MCP catalog HTTP 502s match baseline.
Production definitions, image and schema remain unchanged.

Read-only signing-key metadata also confirms identical references/configuration
and no secret change since April16, with rotation disabled. No secret value is
recorded. Existing-token/old-client compatibility was separately verified; a
fresh interactive corporate identity-provider login remains a promotion check.

### Final cleanup progress

The independently reviewed cleanup rebind changes only the explicit indirect
reference-review flag, its configuration binding, and one holding-rule
condition-array-order hash; the conditions, actions, owner tags and priorities
are semantically identical. Every other state/cohort/proof value is preserved.
Native cleanup restores the original canonical-only actions, removes the owned
ingestion service/target group, and then stops at its internal 23:00 UTC write
guard before remaining mutations. The app stays healthy and serving.

Under the existing dev cleanup authorization, a separately reviewed renewal
changes only expires_at to November10 00:30 UTC and the state config binding.
The original warmup, not_before, cutovers, approvals, source/image and all
cohorts/proofs remain intact. Fresh origin/writer and original/canonical
scheduled-controller checks pass. This renews cleanup only; it creates no
application change, rollout or production action. Native deletion resumes
with ownership and direct live/scheduled dependency checks. Final actual AWS
absence and the asynchronous definition metadata remainder are recorded below;
accepted deletion alone is not removal.

### Cleanup verification and remaining metadata

At 23:09:39 UTC, final read-only verification confirms exactly canonical
web455(2) and ingestion37(1), all at digest 31066fb1; no stray non-stopped
OWUI workers remain. Route actions match the captured baseline. Both owned
temporary services are INACTIVE/zero tasks, both target groups and both
holding rules are absent. Typed JSON readiness is independently established
by the full canonical functional scope, not inferred from a bare HTTP 200.

AWS accepted deletion of the ingestion temporary definition at 23:07:36 and
web definition at 23:08:31 after fresh direct/indirect dependency checks. Both
remain DELETE_IN_PROGRESS at this verification point:

- `oxygpt-open-webui-ingest-task-0133-dev-1109-cutover:1`
- `oxygpt-open-webui-task-0133-dev-1109-cutover:1`

These are unlaunchable deletion metadata records with no live tasks or
scheduled/indirect dependencies found. Accepted deletion is not verified
absence. Read-only follow-up must confirm Describe no longer finds each exact
ARN; cleaned_at remains unset until then. No further AWS mutation is planned.
The reviewed read-only verifier is running on the home server (PID3101782),
checking the exact two ARNs every five minutes through November10 23:18:31 UTC.
Its first account-verified round at 23:22:36 confirms both still pending. It
writes private atomic status to the release validation folder and exits when
both are absent, an unexpected active definition requires review, or its bound
expires. This is verification only, with no cloud mutation or redeployment.
Final live-resource proof SHA256:
`3e4cd790c0a2f34df51e23e0081242f8150d2f8ed2907d6e52e310035fad23b1`.

The owned observer is stopped and its disappearance verified after live
cleanup mutations finish. Across 6101 merged observation sets, 18303 typed
health/version checks have zero failures or malformed rows. The two actual
switch windows contain 354/357 checks and no failures, with maximum 3-second
gaps. The broader migration window has 222 successful checks, including one
1390 ms version response at 20:21:32; p95 there is 8 ms. No cause is assigned
to that isolated latency observation. Overall maximum sampling gap is 4 seconds;
observer replacement has a verified zero-gap overlap. These are sampled
observations, not a guarantee about every production request. Raw overlapping
logs contain 6102 sets/18306 successful probes; the merged series removes one
successful overlap timestamp and retains the source counts and hashes.

### Durable pin and promotion boundary

The only deployment image pin changed by infrastructure PR1370 is dev's, set to
the tested digest; ingestion inherits that value. Production serving/ingestion and the
compatibility-wrapper pins stay at `83f92c31…`. The train's exact reviewed
`d19f0ef46aea552ea31e17b6ad0ca2e66274a9d9` source excludes Open WebUI from its
image-build/pin rewrite list, so it preserves the merged dev pin. The inspected
ADO build definitions, branch policies, release definitions and service hooks
show no configured immediate deployment trigger on this repository's master
merge. The normal scheduled train still applies both dev and production; this
task does not queue it. Its next ordinary run is November10 09:33 UTC
(03:33 Central), subject to its existing schedule guard and readiness gates.

The application PR remains open for review: merging main invokes active
release/image/package publication workflows, which is outside this dev-only
delivery. The image remains traceable to committed source `7e5fa4c59`; later
documentation commits do not change its runtime manifest. Production promotion
requires a separate decision, fresh deployment-writer/capacity checks, a review
of existing parent/subagent families and due work, and fresh identity-provider
login validation. Keep the tested image, rollback image and private recovery
evidence available for that review.
