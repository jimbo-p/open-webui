# Open WebUI 0.12.0 second dev build

Review date: October 12, 2026 (America/Chicago). Compared the first dev build with upstream `dev` through `93d25dbe3b2f8b3adf651486de08f6bf9d17f05b`, plus the open fixes below. The [0.12.1 release PR #32296](https://github.com/open-webui/open-webui/pull/32296) and [changelog PR #32228](https://github.com/open-webui/open-webui/pull/32228) were still open; no official `v0.12.1` tag existed at review time. This remains 0.12.0 with selected fixes, not the complete unreleased 0.12.1 feature set.

## Included since the first dev build

| Change | Source | Reason |
| --- | --- | --- |
| Compaction during long tool runs uses provider token usage plus fresh results | [PR #32308](https://github.com/open-webui/open-webui/pull/32308), `4eda1f00d` (open) | Highest priority for long MCP investigations. Character estimates can substantially undercount reasoning/code output. Latest-call aliases avoid treating cumulative billed tokens as current context. Providers without usage retain estimation; stateful Responses API behavior is unchanged. |
| Calendar automations use the user's current date and exact minute | [PR #32264](https://github.com/open-webui/open-webui/pull/32264), `c89336da5` (merged) | Prevent duplicate daily/weekly/monthly runs and skipped first runs when the user's timezone is behind the server. |
| Single-word chat search avoids repeating the same predicate | [PR #32268](https://github.com/open-webui/open-webui/pull/32268), `113a64ad1` (merged) | Reduces redundant PostgreSQL work without changing matching results. No performance percentage is claimed for this deployment. |
| Skill discovery tolerates repository symlinks outside selected skills | [PR #32313](https://github.com/open-webui/open-webui/pull/32313), `96e92ff46` (open) | Allows ordinary GitHub skill repositories to import. Links inside skills and links in directly uploaded ZIPs remain rejected. |
| Skill import preview opens/closes without modal teardown errors | [PR #32315](https://github.com/open-webui/open-webui/pull/32315), `718da9c73` (open) | Removes an unnecessary keyed remount. |
| Model version restoration resolves attached notes as notes | [PR #32317](https://github.com/open-webui/open-webui/pull/32317), `f3f9d9610` (open) | Older model versions with note attachments no longer fail an incorrect knowledge-base lookup. Existing resource permission checks remain. |
| Knowledge folder deletion closes only a file actually deleted | [PR #32319](https://github.com/open-webui/open-webui/pull/32319), `63528a66c` (open) | Uses the server's deleted-file IDs; moving files to the parent preserves the open viewer. |
| Shared-chat editing hides Save on another author's messages | [PR #32321](https://github.com/open-webui/open-webui/pull/32321), `c12de1c49` (open) | Removes a misleading action that the server would silently discard. Send / Save As Copy remain available. |
| Voice calls cannot start in someone else's shared chat | [Commit `3a54306c5`](https://github.com/open-webui/open-webui/commit/3a54306c50f351e5d6e89e296cb32518535712ee) | Applies the shared-chat restriction consistently and closes an existing overlay on navigation. |
| Temporary-chat follow-ups include the latest answer; default context is bounded | [Commits `53a8f131f`](https://github.com/open-webui/open-webui/commit/53a8f131f305a3beb87376d5dbc05889883fb950), [`7ae099674`](https://github.com/open-webui/open-webui/commit/7ae09967459151433be79c2e98f096c171fc5b18) | Improves relevant next-message suggestions, bounds default prompt size, validates/deduplicates returned suggestions. Custom prompt text is preserved. |
| Translation deletion preserves other languages; model suggestions inherit localized defaults | [Commit `1283b2701`](https://github.com/open-webui/open-webui/commit/1283b2701ecb0037cc2c374e34e69976c302a25c) | Small data-preservation/editor corrections omitted from the first build. |
| Complete upstream HTTP-safe UUID behavior | [Commit `d786ed56c`](https://github.com/open-webui/open-webui/commit/d786ed56c98e9b0fd4c8af111e1c95d4e00c7789) | Existing editor fixes are retained; terminal connection and realtime-call IDs now also use the native UUID helper. |

All twelve full upstream commit/PR picks applied without conflicts. Two remaining UUID hunks were applied to the already-patched source. No existing corporate overlay was removed or replaced by an incompatible implementation.

## Reconciled and deferred

- Previously selected [#32233](https://github.com/open-webui/open-webui/pull/32233) and [#32235](https://github.com/open-webui/open-webui/pull/32235) merged upstream unchanged. Their behavior was already in build one and is not applied twice.
- Previously selected [#32239](https://github.com/open-webui/open-webui/pull/32239) closed without merge; upstream independently implemented its HTTP-safe UUID behavior in `2eb77fc27` and `d786ed56c`. Retain the equivalent existing edits and include the two additional native hunks.
- New channel Apps, its websocket protocol, permissions, and migration `c92da83b017f` remain deferred until an explicit feature evaluation. This is a substantial new integration surface, not a prerequisite for the fixes above.
- Redis Sentinel authentication changes remain inapplicable to the direct ElastiCache endpoint configuration.
- Standard/realtime voice avatar features, wake-lock changes, VRM pose fixes, and voice-only open PRs #32255/#32256/#32259 remain deferred from this corporate MCP hotfix build. Realtime voice is not enabled by this rollout.
- Sidebar folder links, translation expansion, channel skill picker #32310, and multi-model placeholder feature #32309 are optional improvements, not immediate regressions required here.
- Older open #27145 (direct tool server selection) is unchanged since the first review and still conflicts with current implementation. Broad reindexing, Docling, OneDrive, PDF export, and template-variable features remain deferred.

## Configuration and validation

No environment variables, persistent configuration, dependencies, or database migrations change in this update. The live dev task already sets `ENABLE_CONTEXT_COMPACTION=true`; its persisted configuration remains unchanged. Existing decisions about Tool Search, MFA, realtime voice and other optional features remain in the [original upgrade audit](oxy-0.12.0-upgrade.md#environment-decisions). The historical full-context branch is outside this upgrade; no user decision is pending for it. The Apps variables in the proposed 0.12.1 release do not apply to this selected-fix build.

- 45 backend tests pass, including 13 new cases for compaction, timezone-sensitive recurrence, skill archive boundaries, and follow-up generation.
- Six regression cases reproduce failures with the previous implementation and pass with the selected fixes.
- Seven frontend tests pass. Changed frontend files pass formatting checks.
- Full type checking remains limited by upstream errors: 6,031 errors / 189 warnings versus 6,032 / 189 in build one. Diagnostic comparison found one removed error; one existing error's printed component type gains the new property count, with no new error category.
- Container identity, runtime checks, image digest, and ECS health results are recorded in `oxygpt/reports/owui-0.12.0-hotfix2-dev-2026-10-12.md` after rollout.

User acceptance should focus on a long multi-round tool run crossing the compaction threshold, automation previews, GitHub skill import, version restoration with notes, shared chat edits, knowledge-folder deletion, and temporary-chat follow-ups. Production remains a separate promotion after dev acceptance.
