"""Keep saved chats small enough to load, save and replay.

A chat is loaded and saved whole on every turn and every open, so one ~900 MB chat took a 16 GB task out. Only the
saved copies of tool results, citation excerpts and file text copied into a message are bounded here; what people
type and what models write is never changed. That includes answers people give to ask_user, approval and agent
resolutions, and the hidden context the model must keep whole (preloaded skills, manifests, recalled memories).

A shortened text keeps its start and end and says how much was removed (from the original length, also when it is
shortened again), so running the bounds again on their own output changes nothing; the frontend saves back what it
was served. Once a chat is over its budget it is brought down to a lower watermark with stepped caps, so steady growth
does not re-shorten the same copies on every save.

Inputs are never modified: a message that needs bounding is copied first, because callers such as the tool loop
keep using their live objects for the model.
"""

from __future__ import annotations

import copy
import logging
import re
from types import SimpleNamespace

from open_webui import env

log = logging.getLogger(__name__)

TOOL_NOTE = (
    '\n\n[Saved copy shortened: {removed:,} of {total:,} characters were removed from the middle to keep this '
    'chat small. Run the tool again to see all of it.]\n\n'
)
SOURCE_NOTE = (
    '\n\n[Citation excerpt shortened: {removed:,} of {total:,} characters were removed from the middle. '
    'Open the source to read all of it.]\n\n'
)
NOTES = {'tool': TOOL_NOTE, 'source': SOURCE_NOTE}
# Used when nothing of a copy is kept: only for chats still over budget after every copy is a short preview.
REMOVED_NOTES = {
    'tool': '[Saved copy removed: this tool result ({total:,} characters) was removed to keep this chat small. '
    'Run the tool again to see it.]',
    'source': '[Citation excerpt removed ({total:,} characters) to keep this chat small. Open the source to read it.]',
}
NOTE_MARKERS = (
    '[Saved copy shortened: ',
    '[Saved copy removed: ',
    '[Citation excerpt shortened: ',
    '[Citation excerpt removed (',
)
_SHORTENED_TOTAL = re.compile(r'\[(?:Saved copy|Citation excerpt) shortened: [\d,]+ of ([\d,]+) characters')
_REMOVED_TOTAL = re.compile(
    r'\[(?:Saved copy removed: this tool result|Citation excerpt removed) \(([\d,]+) characters'
)
# Tool calls whose results hold what a person answered or context the model must keep whole.
KEPT_CALL_NAMES = frozenset({'ask_user'})
# Chat-budget caps are rounded down to these multiples of the preview floor, so they do not creep save by save.
STEP_MULTIPLES = (1, 2.5, 5, 10, 25, 50, 100, 200)
# After the chat budget is exceeded, the chat is brought down to this share of it.
WATERMARK = 0.75

_compaction = {'enabled': False}


def enabled() -> bool:
    return bool(env.CHAT_STORAGE_BOUNDS)


def note_compaction_enabled(value: bool) -> None:
    """Record whether context compaction is on (read with its config on every request).

    Until it is known to be on, every message of the current branch counts as replayed.
    """
    _compaction['enabled'] = bool(value)


def has_bound_note(value) -> bool:
    """True when a saved text (or list of text parts) was shortened or removed by these bounds."""
    if isinstance(value, list):
        value = ' '.join(
            part.get('text', '') for part in value if isinstance(part, dict) and isinstance(part.get('text'), str)
        )
    return isinstance(value, str) and any(marker in value for marker in NOTE_MARKERS)


def _limits() -> SimpleNamespace:
    # Read at call time so an operator change or a test override applies without a restart of this module.
    return SimpleNamespace(
        tool_result=env.TOOL_RESULT_MAX_CHARS,
        message_tool=env.CHAT_MESSAGE_TOOL_OUTPUT_MAX_CHARS,
        source_document=env.CHAT_SOURCE_DOCUMENT_MAX_CHARS,
        message_sources=env.CHAT_MESSAGE_SOURCES_MAX_CHARS,
        inline_file=env.CHAT_INLINE_FILE_CONTENT_MAX_CHARS,
        chat_bulk=env.CHAT_STORED_BULK_MAX_CHARS,
        protected=env.CHAT_BOUND_PROTECTED_MESSAGES,
        floor=env.CHAT_TRIM_PREVIEW_MIN_CHARS,
        old_preview=env.CHAT_TRIM_OLD_PREVIEW_CHARS,
    )


def _original_length(text: str) -> int:
    match = _SHORTENED_TOTAL.search(text) or _REMOVED_TOTAL.search(text)
    return max(int(match.group(1).replace(',', '')), len(text)) if match else len(text)


def shorten(text: str, target: int, kind: str = 'tool') -> str:
    """Keep the start and end of ``text`` within ``target`` characters, with a note in the middle."""
    if len(text) <= target:
        return text
    total = _original_length(text) if has_bound_note(text) else len(text)
    template = NOTES[kind]
    keep = max(target - len(template.format(removed=total, total=total)), 0)
    if keep == 0:
        return REMOVED_NOTES[kind].format(total=total)
    head = keep * 3 // 4
    tail = keep - head
    note = template.format(removed=total - head - tail, total=total)
    return text[:head] + note + (text[-tail:] if tail else '')


def _cap_for(lengths: list[int], budget: int) -> int | None:
    """The largest per-item cap that fits ``lengths`` into ``budget``; None when they already fit."""
    if sum(lengths) <= budget:
        return None
    ordered = sorted(lengths)
    kept = 0
    for index, length in enumerate(ordered):
        remaining = len(ordered) - index
        if kept + length * remaining > budget:
            return max((budget - kept) // remaining, 0)
        kept += length
    return ordered[-1]


def _step_down(cap: int, floor: int) -> int:
    """Round a cap down to a fixed step (never below ``floor``), so repeated passes reuse the same cap."""
    if cap <= floor:
        return floor
    steps = [int(floor * multiple) for multiple in STEP_MULTIPLES]
    if cap >= steps[-1]:
        return cap // steps[-1] * steps[-1]
    return max(step for step in steps if step <= cap)


def _kept_call_ids(output: list) -> set:
    kept = set()
    for item in output:
        if (
            isinstance(item, dict)
            and item.get('type') == 'function_call'
            and (item.get('hidden') is True or item.get('name') in KEPT_CALL_NAMES or item.get('agent_resolution'))
        ):
            kept.add(item.get('call_id'))
    return kept


def _tool_slots(output) -> list[tuple[str, tuple, int]]:
    slots = []
    output = output if isinstance(output, list) else []
    kept = _kept_call_ids(output)
    for index, item in enumerate(output):
        if not isinstance(item, dict) or item.get('type') != 'function_call_output':
            continue
        if item.get('call_id') in kept or item.get('status') == 'rejected':
            continue  # an answer, a resolution or hidden context: never shortened
        value = item.get('output')
        if isinstance(value, str):
            slots.append(('tool', ('output', index, 'output'), len(value)))
            continue
        for part_index, part in enumerate(value if isinstance(value, list) else []):
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                slots.append(('tool', ('output', index, 'output', part_index, 'text'), len(part['text'])))
    return slots


def _source_slots(sources) -> list[tuple[str, tuple, int]]:
    slots = []
    for index, source in enumerate(sources if isinstance(sources, list) else []):
        documents = source.get('document') if isinstance(source, dict) else None
        for document_index, document in enumerate(documents if isinstance(documents, list) else []):
            if isinstance(document, str):
                slots.append(('source', ('sources', index, 'document', document_index), len(document)))
    return slots


def _slots(message: dict) -> list[tuple[str, tuple, int]]:
    """(kind, path, length) of every bounded text in one message."""
    return _tool_slots(message.get('output')) + _source_slots(message.get('sources'))


def _get(message: dict, path: tuple):
    value = message
    for key in path:
        value = value[key]
    return value


def _has_inline_text(item, limit: int) -> bool:
    if not isinstance(item, dict) or item.get('type') != 'file' or not item.get('id'):
        return False
    data = item['file'].get('data') if isinstance(item.get('file'), dict) else None
    return isinstance(data, dict) and isinstance(data.get('content'), str) and len(data['content']) > limit


def _inline_drops(message: dict, limits: SimpleNamespace) -> list[tuple]:
    """File items whose copied text is dropped (the file record keeps it): attachments and citation sources."""
    drops = []
    files = message.get('files')
    for index, item in enumerate(files if isinstance(files, list) else []):
        if _has_inline_text(item, limits.inline_file):
            drops.append(('files', index))
    sources = message.get('sources')
    for index, source in enumerate(sources if isinstance(sources, list) else []):
        # A citation's source is the request's file item; its copied text duplicates the excerpt in 'document'.
        if isinstance(source, dict) and _has_inline_text(source.get('source'), limits.source_document):
            drops.append(('sources', index, 'source'))
    return drops


def _apply(message: dict, targets: dict[tuple, tuple[str, int]], drops: list[tuple]) -> dict:
    if not targets and not drops:
        return message
    bounded = copy.deepcopy(message)  # containers are copied; the strings themselves are shared, not duplicated
    for path, (kind, target) in targets.items():
        container = _get(bounded, path[:-1])
        container[path[-1]] = shorten(container[path[-1]], target, kind)
    for path in drops:
        _get(bounded, path)['file']['data'].pop('content', None)
    return bounded


def _message_targets(slots: list[tuple[str, tuple, int]], limits: SimpleNamespace) -> dict[tuple, tuple[str, int]]:
    targets = {}
    for kind, per_item, budget in (
        ('tool', limits.tool_result, limits.message_tool),
        ('source', limits.source_document, limits.message_sources),
    ):
        group = [(path, length) for slot_kind, path, length in slots if slot_kind == kind]
        capped = [min(length, per_item) for _, length in group]
        cap = _cap_for(capped, budget)
        if cap is not None:
            cap = _step_down(max(cap, limits.floor), limits.floor)
        for (path, length), value in zip(group, capped):
            target = value if cap is None else min(value, cap)
            if target < length:
                targets[path] = (kind, target)
    return targets


def _bound_with_slots(message, limits: SimpleNamespace):
    slots = _slots(message)
    targets = _message_targets(slots, limits)
    drops = _inline_drops(message, limits)
    if not targets and not drops:
        return message, slots
    bounded = _apply(message, targets, drops)
    return bounded, _slots(bounded)


def bound_message(message):
    """Return ``message``, or a bounded copy of it when one of its saved texts is over its limit."""
    if not enabled() or not isinstance(message, dict):
        return message
    try:
        return _bound_with_slots(message, _limits())[0]
    except Exception:
        log.exception('Could not bound a saved chat message; keeping it as it is')
        return message


def bound_files(files):
    """Drop copied text over the inline limit from a list of file items (a chat's top-level ``files``)."""
    if not enabled() or not isinstance(files, list):
        return files
    try:
        return _apply({'files': files}, {}, _inline_drops({'files': files}, _limits()))['files']
    except Exception:
        log.exception('Could not bound chat files; keeping them as they are')
        return files


def _children(view: dict) -> dict:
    children: dict = {}
    for key, message in view.items():
        if isinstance(message, dict):
            children.setdefault(message.get('parentId'), []).append(key)
    return children


def _last_descendant(view: dict, message_id: str) -> str:
    """Follow the newest child from ``message_id`` (as ChatTable._last_descendant_id does)."""
    seen = set()
    while message_id in view and message_id not in seen:
        seen.add(message_id)
        message = view[message_id]
        child_ids = message.get('childrenIds') if isinstance(message, dict) else []
        next_id = next(
            (
                child_id
                for child_id in reversed(child_ids if isinstance(child_ids, list) else [])
                if isinstance(child_id, str)
                and child_id not in seen
                and isinstance(child := view.get(child_id), dict)
                and child.get('role')
                and child.get('parentId') == message_id
            ),
            None,
        )
        if not next_id:
            break
        message_id = next_id
    return message_id


def _branch(messages: dict, current_id: str | None) -> list[str]:
    """Ids from the root to ``current_id``; the newest messages when there is no usable current id."""
    branch, seen = [], set()
    message_id = current_id
    while message_id and message_id in messages and message_id not in seen:
        seen.add(message_id)
        branch.append(message_id)
        message = messages[message_id]
        message_id = message.get('parentId') if isinstance(message, dict) else None
    if branch:
        return branch[::-1]
    return sorted(
        (key for key, value in messages.items() if isinstance(value, dict)),
        key=lambda key: messages[key].get('timestamp') or 0,
    )


def _replayed(messages: dict, branch: list[str]) -> set[str]:
    """Branch messages the model still reads: all of them, unless compaction is on and a checkpoint cuts earlier."""
    if not _compaction['enabled']:
        return set(branch)
    try:
        from open_webui.utils.context_compaction import _apply_latest_summary_checkpoint

        listed = [{**messages[key], 'id': key} for key in branch]
        kept, _ = _apply_latest_summary_checkpoint(listed)
        return {message.get('id') for message in kept} & set(branch)
    except Exception:
        return set(branch)


def _total(slots: dict) -> int:
    return sum(length for message_slots in slots.values() for _, _, length in message_slots)


def _shorten_to(view: dict, slots: dict, key: str, cap: int, changed: set[str], above: int | None = None) -> int:
    """Shorten one message's saved texts longer than ``above`` (default ``cap``) to ``cap``; return chars removed.

    A text that already carries a saved-copy note is left alone unless it is well above the new cap.
    """
    above = cap if above is None else above
    message = view[key]
    targets = {}
    for kind, path, length in slots[key]:
        if length <= above or (length <= cap * 3 // 2 and has_bound_note(_get(message, path))):
            continue
        targets[path] = (kind, cap)
    if not targets:
        return 0
    before = sum(length for _, _, length in slots[key])
    view[key] = _apply(message, targets, [])
    changed.add(key)
    slots[key] = _slots(view[key])
    return before - sum(length for _, _, length in slots[key])


def _protected(view: dict, branch: list[str], limits: SimpleNamespace, keep: set[str]) -> set[str]:
    protected = set(branch[-limits.protected :]) if limits.protected else set()
    # Other replies to the newest prompts (multi-model replies, the previous regeneration) and edited prompts.
    children = _children(view)
    for key in list(protected):
        protected.update(children.get(view[key].get('parentId'), []))
    # A reply still being written is the tool loop's live state; so is any message a caller is saving right now.
    protected |= keep
    protected |= {key for key, message in view.items() if isinstance(message, dict) and message.get('done') is False}
    return protected


def _tiers(view: dict, slots: dict, current_id: str | None, limits: SimpleNamespace, keep: set[str]) -> list[list]:
    """Message ids that may be shortened, oldest first: copies the model no longer reads, then older replayed ones."""
    if current_id in view:
        current_id = _last_descendant(view, current_id)
    branch = _branch(view, current_id)
    protected = _protected(view, branch, limits, keep)
    replayed = _replayed(view, branch)
    position = {key: index for index, key in enumerate(branch)}
    unread = sorted(
        (key for key in slots if key not in replayed and key not in protected),
        key=lambda key: (key in position, position.get(key, 0), view[key].get('timestamp') or 0),
    )
    return [unread, [key for key in branch if key in replayed and key not in protected and key in slots]]


def _fit_chat(
    view: dict, slots: dict, current_id: str | None, limits: SimpleNamespace, changed: set[str], keep: set[str]
) -> None:
    """Shorten saved copies across the chat, in ``view``, once it is over the chat budget.

    The chat is brought down to WATERMARK of the budget in three steps, each across every tier before the next:
    the largest copies to stepped caps no lower than a generous preview; then, oldest message first, copies to a short
    preview; and only if that is still not enough, oldest first, copies to a note.
    """
    total = _total(slots)
    if total <= limits.chat_bulk:
        return
    target = int(limits.chat_bulk * WATERMARK)
    tiers = _tiers(view, slots, current_id, limits, keep)
    for tier in tiers:
        lengths = [length for key in tier for _, _, length in slots[key]]
        cap = _cap_for(lengths, max(target - (total - sum(lengths)), 0)) if total > target else None
        if cap is not None:
            cap = _step_down(max(cap, limits.floor), limits.floor)
            total -= sum(_shorten_to(view, slots, key, cap, changed) for key in tier)
    removed_note = len(REMOVED_NOTES['tool'].format(total=10**9)) * 3 // 2
    for cap, above in ((limits.old_preview, None), (0, removed_note)):
        for key in (key for tier in tiers for key in tier):
            if total <= target:
                return
            total -= _shorten_to(view, slots, key, cap, changed, above=above)


def bound_messages_map(messages, current_id: str | None = None, keep=()) -> tuple[dict, set[str]]:
    """Bound every message, then the whole chat. Return the map (a new one if anything changed) and changed ids.

    The newest messages of the current branch and their siblings, replies still being written and ``keep`` are never
    shortened for the chat budget (they still get the per-message bounds).
    """
    if not enabled() or not isinstance(messages, dict):
        return messages, set()
    try:
        limits = _limits()
        view = dict(messages)
        changed, slots = set(), {}
        for key, message in messages.items():
            if not isinstance(message, dict):
                continue
            bounded, slots[key] = _bound_with_slots(message, limits)
            if bounded is not message:
                view[key] = bounded
                changed.add(key)
        _fit_chat(view, slots, current_id, limits, changed, set(keep))
        if changed:
            log.info('Bounded %d saved messages to keep a chat within %d characters', len(changed), limits.chat_bulk)
        return (view if changed else messages), changed
    except Exception:
        log.exception('Could not bound a saved chat; keeping it as it is')
        return messages, set()


def bound_chat_with_changes(chat, keep=()) -> tuple[dict, set[str]]:
    """Bound a chat body (history, its ``messages`` list and ``files``). Return it and the changed history ids.

    ``keep`` names messages a caller is saving right now: they get their own bounds but are never shortened for the
    chat budget.
    """
    if not enabled() or not isinstance(chat, dict):
        return chat, set()
    history = chat.get('history')
    changed: set[str] = set()
    result = chat
    new_map = None
    if isinstance(history, dict) and isinstance(history.get('messages'), dict):
        current_id = history.get('currentId') or chat.get('currentId') or chat.get('branchPointMessageId')
        new_map, changed = bound_messages_map(history['messages'], current_id, keep)
        if changed:
            result = {**chat, 'history': {**history, 'messages': new_map}}
    listed = chat.get('messages')
    if isinstance(listed, list):
        bounded_list, list_changed = [], False
        for message in listed:
            key = message.get('id') if isinstance(message, dict) else None
            bounded = new_map[key] if key in changed else bound_message(message)
            list_changed = list_changed or bounded is not message
            bounded_list.append(bounded)
        if list_changed:
            result = {**result, 'messages': bounded_list}
    files = chat.get('files')
    bounded_chat_files = bound_files(files)
    if bounded_chat_files is not files:
        result = {**result, 'files': bounded_chat_files}
    return result, changed


def bound_chat(chat):
    return bound_chat_with_changes(chat)[0]


def bounded_fields(message: dict) -> dict:
    """The columns a chat-budget change can touch, for writing a message's chat_message row."""
    return {key: message[key] for key in ('output', 'sources', 'files') if key in message}
