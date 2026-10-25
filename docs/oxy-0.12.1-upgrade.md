# Oxy Open WebUI 0.12.1 upgrade

Reviewed October 14, 2026. This release starts from official [`v0.12.1`](https://github.com/open-webui/open-webui/releases/tag/v0.12.1), commit `bd495677ab7835a37a2914ada66f4541e5e7ce46`. The previous fork remains on `0.12.0` at `b6f98b3dcd72ae93238b772cb483c99fc1b69666`. The new `0.12.1` branch carries only the remaining corporate overlay, regression tests, and this audit; it does not replay the old backport series.

## Backports retired

Git patch comparison finds 32 exact upstream equivalents, including all 15 commits from [fork PR #2](https://github.com/jimbo-p/open-webui/pull/2). Their native implementations now supply sign-in reconnects, note uploads, model regeneration and settings persistence, feedback comments, follow-up JSON parsing, Ask/Explain text preservation, code execution results, calendar reminders, OAuth preflight handling, empty file lists, history/dialog display, and temporary-chat attachment handling.

Earlier exact equivalents include MCP error logging, external citations, iframe handling, document-setting help text, translated model editing, new-chat tool selections, recurring automations, chat search, skill archive imports, note-backed model versions, knowledge-folder deletion, shared-chat editing, and follow-up generation. They require no fork patch.

Three additional changes are replaced by native implementations rather than identical patches:

| Previous carry                                                           | 0.12.1 decision                                                                                                                                    |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| HTTP-safe UUID fixes (#32239 and the later terminal/realtime completion) | Use native UUID helpers throughout. The earlier terminal formatting-only patch is also unnecessary.                                                |
| Skill-import modal teardown fix (#32315)                                 | Use the current native import/preview lifecycle, which no longer uses the problematic keyed remount.                                               |
| First long-tool-run compaction patch (#32308)                            | Use the complete native implementation, including persistence of compaction checkpoints into later turns. Do not retain the earlier partial patch. |

The custom lazy-tool-loading/provider adapter experiment remains removed. Tool Search configuration is not changed by this upgrade. Historical PRs and branches remain available as an audit trail; their patches are absent from the new overlay.

## Custom behavior retained

| Area                  | Remaining change and reason                                                                                                                                                                                                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Corporate image       | Trust the corporate CA in frontend and Python runtime stages; retain the configurable 8 GiB frontend heap and Docker context exclusions.                                                                                                                                                          |
| File handling         | Keep spreadsheets, CSV/TSV, GIS/CAD, scientific arrays, and DXP/PBIX/PBIT as original files without extraction/embedding. Preserve the actual MIME type of compressed uploads.                                                                                                                    |
| Configuration         | Keep the short-lived snapshot cache with defensive copies, transaction-aware invalidation, and direct reads for settings updated as whole collections. Native 0.12.1 still performs individual uncached reads.                                                                                    |
| Streaming             | Keep incremental Redis response persistence, expiry/eviction recovery, cleanup after Redis failures, incremental tool UI events, absolute output positions, and preservation of previous tool rounds. Keep base64 model input images out of these UI events and surface provider error responses. |
| Notes                 | Seed empty collaborative history from externally authored stored note JSON using deterministic CRDT identifiers.                                                                                                                                                                                  |
| Tools and automations | Keep deterministic OpenAPI required-field ordering, one retry after a cached terminal-spec failure, and protection against scheduled runs creating further automations.                                                                                                                           |
| Chat                  | Wait for tool/default initialization before first submission, reject overlapping submissions during that wait, retain parameterized prompt initialization, use the currently selected model for single-model regeneration, and retain the initial title task's model context.                     |
| UI                    | Preserve knowledge-view pagination reset, document MIME editor semantics, and reactive inline Markdown streaming behavior.                                                                                                                                                                        |

No Python runtime dependency or startup-script change is required relative to the prior deployed image. Native frontend dependency changes, including the merge editor and Mermaid update, come from the release lockfile and are installed during the frontend build.

## Validation

- 46 backend tests pass. These cover the retained cache, extraction, and streaming behavior, plus prior release regressions now exercised against native code.
- A new integration regression verifies that native compaction checkpoints survive incremental Redis persistence, restore the summary on the next turn, retain matching tool calls/results, and leave stored output intact.
- Seven frontend tests pass, including tool/result identity, sparse output positions, prior tool-round preservation, and native version-diff behavior.
- All six changed Svelte components compile without new warnings compared with the official tag. Changed Python modules compile; differential Ruff F checks introduce no findings.
- The merged commit's full frontend build, packaged-source checks, and live dev rollout results are recorded in the release PR.

These checks do not replace authenticated dev acceptance for corporate SSO, real model/MCP calls, long conversations, automations, or collaborative notes.

## Dev migration and rollout

Two native migrations follow the current `b8f2c6d91a04` schema:

1. `c92da83b017f` adds channel Apps, app membership, and message attribution.
2. `0bd252b206fb` creates missing auth rows for existing SCIM users with an unusable empty password hash, preserving existing credentials.

Use the OxyGPT offline migration runner with explicit **dev** cluster, database, image, family, and log group. Verify the pending chain and Aurora recovery availability first. Stop dev serving tasks for migration and restart them together on the new image, following upstream's warning against mixed-version rolling upgrades. Serving tasks retain `ENABLE_DB_MIGRATIONS=false`. Check the migration head, Apps schema/constraints/indexes, and absence of users missing auth rows after the upgrade.

Preserve existing runtime environment, secret references, sidecars, resource settings, and persistent configuration. No database downgrade, vector reindex, embedding model change, optional-feature configuration, or production rollout is part of this upgrade. Apps are available as a native release feature; no App integration is created or activated here.

Production remains on its existing image until separate dev acceptance and promotion. The previous dev image is `sha256:6da0410c786fe46d51c6bf2fb4e9a05cb4bb56f969cc471b1f052fb29f5d5c88`; restoring an application image does not undo migrations. In particular, do not downgrade Apps after app-authored messages exist without an explicit data-preservation plan.
