# OxyGPT Open WebUI release workflow

This repository is the maintained OxyGPT application fork. `origin` is
`https://github.com/jimbo-p/open-webui`; `upstream` is the official
`https://github.com/open-webui/open-webui` project. When reviewing whether a
custom fix is now native, compare with the official upstream release, not with
another branch of our fork.

On the home server, read `/home/coomie/.codex/runbooks/work.md` before access,
testing, or Git operations. Infrastructure and deployment belong to the separate
`/home/coomie/oxygpt` repository; read its `AGENTS.md` before working there.

## Approval before application changes

Obtain explicit approval of the concrete application change before editing or
applying it, building a modified image, deploying, pushing, or merging it,
including in dev. This includes dependencies, frontend/backend changes, custom
image overlays, and wrapper-applied patches. A general request to investigate,
fix an incident, or prepare an upgrade does not replace concrete approval.

Read-only investigation and unapplied proposals are allowed. Before requesting
approval, present the exact release revision, affected files and behaviors,
retained and retired customizations, automatic user experience changes, and the
validation/deployment plan. Honor approval already given for that concrete scope;
do not repeatedly request it. Use the maintained approved image and scoped
operational controls while approval is pending.

## Synchronize and audit each release

1. Inspect remotes, branch, tracking configuration, and working-tree status.
   Fetch `origin` and pull the intended clean base with
   `git pull --ff-only origin <branch>` before repository work. Preserve other
   work and shared history; use a task-specific worktree when appropriate. Stop
   dependent edits and builds if synchronization is blocked. Start new task
   branches from the freshly synchronized intended base.
2. Fetch official upstream refs with the configured tag namespace. Official tags
   are `upstream/vX.Y.Z`; unprefixed tags may belong to the fork and differ from
   the official release. Do not use `git fetch upstream --tags`, overwrite fork
   tags, or include post-release upstream commits in a pinned release upgrade.
3. Inventory the complete current fork overlay against its previous exact
   official baseline, including changes since the last documented upgrade.
   Inspect both commit/patch equivalence and current source semantics. Verify
   deployed serving/ingestion image provenance and any approved wrapper/runtime
   patches as well, so fixes outside the current fork tree are accounted for.
4. Check every cherry-picked PR and custom behavior against the new official
   release. Retire an overlay when native behavior covers its purpose; prefer
   the upstream implementation. Preserve custom fixes and corporate integration
   behavior that upstream does not cover. Explain partial coverage and conflicts
   individually. Do not blindly replay old patches or restore retired ones.
5. Review release notes, security changes, dependencies, migrations and stored
   data formats, API compatibility, new configuration defaults, and automatic
   UI changes. Assess rollback compatibility even when no SQL migration exists.
6. Record the audit and release evidence in `docs/oxy-<version>-upgrade.md`, using
   the established upgrade records as the format. Keep temporary task status and
   incident details out of host setup runbooks.

## Build and validate in dev

After concrete application approval, implement the smallest remaining overlay
on the exact official release, preserving the fork's history and established PR
workflow. Keep the native dependency baseline and Dockerfile except for approved
corporate customizations. Build directly from the reviewed committed source;
do not introduce hidden wrapper patches.

Immediately before every image build/rebuild, fetch and incorporate applicable
`origin` changes and verify the actual build source, including a remote checkout
or transferred archive. Immediately before image publication or deployment,
fetch `origin` again. If relevant source advanced, reconcile it, rerun affected
checks, and rebuild. Record the source commit, build arguments/context changes,
image tag, and immutable digest; verify the published image matches the tested
artifact.

Use compatible Python/Node versions and the repository's current checks. Run
retained-fix regressions, frontend tests/build and encryption-artifact checks,
and appropriate backend checks. Run isolated backend/ops regressions against
disposable databases; never point tests designed for isolated databases at shared data. Dev
integration checks use attributable temporary objects and clean them up. Run
relevant regressions and an isolated startup/health/version check inside the
actual candidate image.

Validate both serving and ingestion in the actual dev cluster. Resolve each
cluster's service to its exact task-definition ARN; dev/prod share family names.
`ai-dev.oxy.com` routes to production, not the dev cluster. Check authentication,
MCP identity, real chat/tool behavior, model defaults and duplicate submissions,
file preservation and ingestion/retrieval, and changed upstream features as
applicable. Compare schema, configuration and model fingerprints, image digests,
sidecars, logs, capacity and health against the captured baseline.
For native readiness use `/ready`; validate response types because unknown
health-like paths can return the frontend HTML with status 200.

## Production, infrastructure and cleanup

Promote only after successful dev checks and review of material user experience
changes. Honor the user's requested production date/time and timezone; do not
deploy early. Establish a verified execution mechanism before claiming a future
deployment is scheduled, with explicit readiness gates and failure reporting.

Use the owning OxyGPT repository's documented workflow. Preserve full serving
capacity and long-running chats, uploads, scheduled work, and parent/subagent
families during handoff. Verify new targets before routing traffic; follow exact
captured old task ARNs through actual container exit. Do not cancel user work or
silently alter saved settings to make a drain complete. Assess mixed-version
data compatibility and preserve a usable rollback path.

Update OxyGPT Terraform image pins for dev, production serving and ingestion,
plus compatibility-wrapper references as applicable. Review the scoped diff,
complete required checks and PR policies, and merge the application and
infrastructure changes within the authorized delivery scope. Verify final live
images match the recorded pins; avoid unrelated Terraform changes.

Track temporary resources and test artifacts with ownership, purpose and a
cleanup condition. Before deleting, verify dependencies, live/scheduled work,
PRs/builds, infrastructure ownership and required retention/rollback needs.
Remove confirmed throwaway resources through their owning workflow, verify
removal, and report deliberately retained items and reasons. Report exact
remaining resources and next actions when cleanup is blocked.
