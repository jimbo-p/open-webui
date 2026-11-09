# Oxy Open WebUI 0.13.3 upgrade and dev validation

Initial audit date: November 9, 2026. The user approved exact official 0.13.3
with the retained Oxy overlay and Escape fix, followed by the concrete timer
backpressure and builtin-tool default-inheritance fixes and development
validation. The approved application source is
`7e5fa4c5902d84d3b0d3c946bb5ebc8917d08486`. Final candidate-image validation
and development deployment remain pending in this record. Production deployment
requires a later decision.

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
integration and isolated checks are complete. The actual immutable image still
requires startup, regression and development serving/ingestion checks:
SSO/delegation, real tool use, immediate model switch/send and duplicate
prevention, files/knowledge, MCP identity, timers/subagents/automations, context
compaction, reconnects, changed UX and resource usage. Recheck effective
configuration/model/schema fingerprints. Development deployment remains pending;
production deployment is excluded from this authorization.

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
and the build-relevant source manifest is
`24a391da55ac381685d811d793e5eae9010006687dd58ee94edb0a277c6aa6e8`.
The final full-CPU build uses that exact source SHA in `BUILD_HASH`,
`BUILD_CHANNEL=main`, and `USE_SLIM=false`, `USE_CUDA=false`,
`USE_OLLAMA=false`. It is running; completion and an immutable image digest are
not yet recorded. Later documentation-only commits do not replace `7e5fa4c59`
as the image source; build-manifest equivalence must verify that distinction.
Actual-image checks and dev deployment evidence must be recorded against that
final source before declaring the release validated.

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
services; actual final-image pressure and live dev validation remain required.

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

### Remaining image, dev and cleanup gates

No production change is authorized. Final-image startup/health/version and
regression checks, dev serving/ingestion integration, memory/backlog pressure,
and deployment/provenance verification remain pending in this record. Preserve
existing user work and settings during the reviewed migration and handoff.
Do not report development deployment complete until its exact task definitions,
images/digests, health and user-visible checks are recorded.

The owned skill-test database, PostgreSQL timer fixtures and the temporary
frontend formatting/i18n archive have been removed and verified. Local proposal
and regression evidence, the timer-validation archive/source directory, the
managed worktree's validation dependencies and the migration-rehearsal container
remain intentionally retained while final-image and dev gates are active.
Remove confirmed throwaway artifacts when those gates end, preserving release
images and required rollback/recovery evidence. Final cleanup must report exact
remaining resources and verify removal; this record does not claim the
environment is clean.
