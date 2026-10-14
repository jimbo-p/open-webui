# Oxy Open WebUI 0.12.2 upgrade

Reviewed October 14, 2026. This branch starts at official [v0.12.2](https://github.com/open-webui/open-webui/releases/tag/v0.12.2), commit `6a18585ff4c2e5f6805f682222cfbc46dc669ef8`, and carries the remaining corporate overlay in one commit. Earlier release branches and merged PRs remain as history.

The [0.12.1 audit](oxy-0.12.1-upgrade.md) records the 32 exact upstream backports and partial UUID, skill-import, and compaction fixes already retired. None is replayed here. Reviewing the 0.12.2 changes found no further complete equivalents for the remaining corporate behavior: CA/build support, originals-only extraction policy, config caching, incremental Redis/UI streaming, deterministic note seeding, tool schema ordering/retry, automation recursion protection, chat initialization/regeneration, and document/knowledge/Markdown UI corrections remain needed.

## Native tool dispatch

Use upstream's `call_tool` dispatcher and provider-history replay without any custom provider adapter. It keeps a declared dispatcher in the provider tool list and resolves discovered targets before approval and execution. The discarded lazy-loading workaround stays absent. Tool Search configuration is unchanged.

The only merge overlap moves our existing incremental tool UI notification into the native dispatch loop. Resolved call names update the existing displayed item; invalid calls emit their failure result. Native approval, target execution, citation handling, and original provider-call history remain intact. Regression coverage checks dispatch history through incremental Redis persistence and frontend replacement without duplicate tool calls.

## Build and rollout

There are no new database migrations after `0bd252b206fb`. This upgrade uses the normal dev rolling deployment without offline maintenance. Production is outside this rollout.

The frontend dependency lockfile changes only the package version. Active Python requirements, startup scripts, and runtime Dockerfile behavior are unchanged; the requirements-file change is an optional Valkey dependency comment. Dev uses pgvector. Build fresh frontend/backend assets from the merged commit on the existing immutable Python runtime, copying the current requirements file for accurate provenance. Preserve runtime settings, secrets, sidecars, resources, replica counts, autoscaling, and persistent configuration.

Run the retained backend/frontend regressions, differential Python/Svelte checks, full frontend build, packaged-source verification, and dev ECS/HTTP/database/assets/WebSocket checks. Record exact image digest, merged commits, and completed validation in the release PR. Authenticated SSO, real provider/MCP calls, and visual acceptance remain dev acceptance tasks before any production promotion.
