# Oxy Open WebUI 0.12.3 upgrade

Reviewed October 19, 2026 against official [v0.12.3](https://github.com/open-webui/open-webui/releases/tag/v0.12.3), `91ea4e3421d1d8069f13d4a463fcb5986d94ea60`. The branch starts at that release and adds the remaining corporate overlay. `origin` is our `jimbo-p/open-webui` fork; `upstream` is the official project. No post-release upstream commits are included.

Both live environments were running `v0.12.2-oxy2`, digest `sha256:682b6ef9ea90b0a3936e7333d2ef931a19a26c5160bb55e46cd4596edf7b1ce7`, at inspection. Dev used task revision 424 with two tasks; production used revision 425 with twelve tasks. The October 15 incident report's older production version is no longer the deployed version.

## Alignment with upstream

| Existing fork behavior | 0.12.3 decision |
| --- | --- |
| Eight post-0.12.2 backports from fork PR #5 | Retire all eight; `git cherry` confirms exact native equivalents. Includes long PDF export, leaving-chat scroll errors, multi-model Continue Response, imported/archived tags, knowledge sharing, API-key audit logging, and the accompanying upstream refactor. |
| Custom incremental Redis stream format | Replace with native snapshots and appended deltas, byte-length guards, TTL handling, reconstruction and cleanup. Retain only local task cleanup before attempting Redis cleanup, so Redis failures cannot leak local state. |
| Custom promise implementation for model defaults | Replace with upstream's shared promise. Retain waiting at submission boundaries and the overlapping-submit guard, which upstream still lacks. |
| Custom suppression of response-completion output from later tool rounds | Use native output filtering in `get_response_data_with_full_output_index`; remove the redundant helper argument. |
| Native terminal-spec recovery versus new OpenAPI outage recovery | Keep the terminal retry: upstream's new fix covers OpenAPI server discovery, a different cache and path. |
| Corporate CA/build support and originals-only file handling | Retain. Native extraction policy does not match Oxy's spreadsheet/GIS/CAD/scientific/BI handling. |
| Config snapshot cache, deterministic collaborative-note seeding, stable OpenAPI required-field order, automation recursion protection | Retain; no equivalent in the release. |
| Incremental tool UI notifications, model-input-image exclusion, provider error display, selected-model regeneration, parameterized prompt initialization, document/Markdown/knowledge UI corrections | Retain the portions still missing upstream. Native tool dispatch and provider-history replay remain untouched. |

The [0.12.1 audit](oxy-0.12.1-upgrade.md) documents previously retired backports. They remain retired. This release does not reintroduce the discarded provider/lazy-tool adapter.

## User-facing changes

- Selected model skills now load full instructions on each message. Readable, unselected skills remain discoverable when the Skills builtin tool is enabled. Selecting a skill is now an intentional prompt-context choice.
- Workspace search spans chats, shared content, messages and notes, with filters and result previews.
- Shared chats gain visible collaborators and improved editing/regeneration. Folder sharing moves into the folder editor's Access control, with access-inheritance fixes.
- Terminal file previews gain richer Markdown, document comments and replies, older Excel support, and Ask or edit on selected file text.
- Model default features and builtin-tool overrides save correctly. OAuth tool activation/refresh, OpenAPI server recovery, custom provider tools, long-tool compaction, PDF reading and read-aloud behavior improve.
- Encrypted temporary channels are available but disabled by default. This rollout does not enable them.
- Skill read/preview limits become configurable. Redis room subscriptions become narrower; their ACL requirements change to PUBLISH, SUBSCRIBE, UNSUBSCRIBE and PING with access to `&socketio` and `&socketio#*`.

Native Redis improvements address stream write amplification and fan-out. They do not demonstrate a fix for the separate oversized persisted citation/chat-JSON issue in the October 15 incident investigation.

## Skill notification and database sequence

The read-only October 19 inventory found 394 production models with selected skills, owned by 194 email recipients; 32 affected models were inactive. Dev had eight affected models, three inactive, owned by one of the same recipients. There were no missing owner emails, malformed selection arrays, or global default skill selections. Counts can change before execution.

Recipient and per-agent CSV/text files are local, ignored artifacts under the primary checkout's `.cache/owui-0123/`. Personal data is not committed or packaged into the image.

1. Deliver the owner list. The user sends the notification.
2. Wait for the user's confirmation before committing any selection removal or promoting production.
3. Re-inventory both environments and notify any additional owners. Run the exact cleanup in dev first, verify it, then apply to production during the approved release window.

`scripts/clear_model_skills.py` defaults to a transaction followed by rollback. It removes only nonempty `model.meta.skillIds`, including inactive/hidden/base-model rows; preserves builtin Skills capability, skill records, permissions and unrelated metadata; stores full affected rows in `oxy_model_skill_selection_backup`; and creates new model-history revisions linked to the previous versions. Older revisions are preserved, so deliberately restoring an old revision can restore its selections. Existing chat-specific selections are outside this model-builder cleanup and remain unchanged.

The script requires an existing administrator as revision author. Applying requires both `--notification-sent` and the actual `--notified-emails` file; newly affected owners outside that list stop the transaction. A bounded table lock prevents a concurrent model creation/edit from escaping the cleanup. Rollback is available with `--restore-run RUN_ID`; it refuses to overwrite later model edits. Always rehearse restoration in dev before an actual restore.

```powershell
# Rehearse in dev; commits nothing.
python scripts/clear_model_skills.py --environment dev --actor-email James_Pruet@oxy.com

# Only after the user confirms notification and authorizes proceeding:
python scripts/clear_model_skills.py --environment dev --actor-email James_Pruet@oxy.com --apply --notification-sent --notified-emails PATH_TO_SENT_EMAIL_LIST
# Verify dev before using the same command with --environment prod.
```

After removal, refresh open browser tabs so cached model metadata cannot reselect old defaults. Check affected agents in fresh chats; explicit future skill selection continues to work normally.

## Build and rollout

There are no new database schema migrations or Python runtime dependency/startup changes relative to 0.12.2. Schema head remains `0bd252b206fb`. Build from a Git archive so Windows checkout line-ending conversion does not invalidate upstream's committed encryption-artifact fingerprints. Keep upstream's encryption verification enabled.

Build fresh frontend and backend artifacts on the existing immutable Python runtime. Preserve runtime environment, secrets, nginx/PgDog sidecars, resources, task counts, autoscaling and persistent configuration. Pin the resulting image by digest.

The fork's old Redis stream keys/encoding differ from native 0.12.3; upstream's legacy fallback only understands its own former shared hash. Drain/restart dev serving tasks together to avoid mixed stream formats. Use the same approach for a later approved production promotion; do not assume native rolling-upgrade compatibility covers our old custom format.

Production promotion and committed skill cleanup remain on hold pending the user's notification confirmation. Dev validation can exercise the cleanup inside a rolled-back transaction without changing saved selections.

## Validation record

- 48 backend regressions pass in the deployed Python runtime with the candidate source. Includes native Redis append/retry/eviction/TTL, legacy native hash reads, compaction checkpoints and tool dispatcher history.
- Eight frontend regressions pass, including tool/output identity and prior-round preservation.
- Five PostgreSQL migration tests pass: selective cleanup, immutable old history, exact restoration, rollback-only rehearsal, notification drift, later-edit protection and unexpected global defaults.
- Dev database rehearsal changed eight models inside a transaction and rolled it back successfully. No skill cleanup has been committed.
- Final build, source integrity and live dev results are recorded after deployment.
