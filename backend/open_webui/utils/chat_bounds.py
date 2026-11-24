"""Keep saved chats small enough to load, save and replay.

A chat is loaded and saved whole on every turn and every open, so one ~900 MB chat took a 16 GB task out. Only the
saved copies of tool results, citation excerpts and file text copied into a message are bounded here; what people
type and what models write is never changed. A shortened text keeps its start and end and says how much was removed,
so running the bounds again on their own output changes nothing (the frontend saves back what it was served).

Inputs are never modified: a message that needs bounding is copied first, because callers such as the tool loop
keep using their live objects for the model.
"""

from __future__ import annotations

import copy
import logging
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
# Used when nothing of an old copy is kept (only for chats still over budget after every copy is a short preview).
REMOVED_NOTES = {
    'tool': '[Saved copy removed: this old tool result ({total:,} characters) was removed to keep this chat small. '
    'Run the tool again to see it.]',
    'source': '[Citation excerpt removed ({total:,} characters) to keep this chat small. Open the source to read it.]',
}


def enabled() -> bool:
    return bool(env.CHAT_STORAGE_BOUNDS)


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


def shorten(text: str, target: int, kind: str = 'tool') -> str:
    """Keep the start and end of ``text`` within ``target`` characters, with a note in the middle."""
    total = len(text)
    if total <= target:
        return text
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


def _tool_slots(output) -> list[tuple[str, tuple, int]]:
    slots = []
    for index, item in enumerate(output if isinstance(output, list) else []):
        if not isinstance(item, dict) or item.get('type') != 'function_call_output':
            continue
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


def _file_drops(message: dict, limit: int) -> list[int]:
    """Attached files whose copied text is too large to keep in the message (the file record keeps it)."""
    drops = []
    files = message.get('files')
    if not isinstance(files, list):
        return drops
    for index, item in enumerate(files):
        if not isinstance(item, dict) or item.get('type') != 'file' or not item.get('id'):
            continue
        data = (item.get('file') or {}).get('data') if isinstance(item.get('file'), dict) else None
        if isinstance(data, dict) and isinstance(data.get('content'), str) and len(data['content']) > limit:
            drops.append(index)
    return drops


def _apply(message: dict, targets: dict[tuple, tuple[str, int]], drops: list[int]) -> dict:
    if not targets and not drops:
        return message
    bounded = copy.deepcopy(message)  # containers are copied; the strings themselves are shared, not duplicated
    for path, (kind, target) in targets.items():
        container = bounded
        for key in path[:-1]:
            container = container[key]
        container[path[-1]] = shorten(container[path[-1]], target, kind)
    for index in drops:
        bounded['files'][index]['file']['data'].pop('content', None)
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
            cap = max(cap, limits.floor)
        for (path, length), value in zip(group, capped):
            target = value if cap is None else min(value, cap)
            if target < length:
                targets[path] = (kind, target)
    return targets


def _bound_with_slots(message, limits: SimpleNamespace):
    slots = _slots(message)
    targets = _message_targets(slots, limits)
    drops = _file_drops(message, limits.inline_file)
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
    """Branch messages the model still reads: those after the latest compaction checkpoint."""
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
    """Shorten one message's saved texts longer than ``above`` (default ``cap``) to ``cap``; return chars removed."""
    above = cap if above is None else above
    targets = {path: (kind, cap) for kind, path, length in slots[key] if length > above}
    if not targets:
        return 0
    before = sum(length for _, _, length in slots[key])
    view[key] = _apply(view[key], targets, [])
    changed.add(key)
    slots[key] = _slots(view[key])
    return before - sum(length for _, _, length in slots[key])


def _fit_chat(
    view: dict, slots: dict, current_id: str | None, limits: SimpleNamespace, changed: set[str], keep: set[str]
) -> None:
    """Shorten saved copies across the chat, in ``view``, until it fits the chat budget.

    Per tier: first the largest copies, down to a generous preview; then, oldest message first, copies down to a short
    preview, so a long chat of many medium results keeps its recent ones whole; and only if that is still not enough,
    the oldest copies down to a note.
    """
    total = _total(slots)
    if total <= limits.chat_bulk:
        return
    branch = _branch(view, current_id)
    protected = set(branch[-limits.protected :]) if limits.protected else set()
    # A reply still being written is the tool loop's live state; so is any message a caller is saving right now.
    protected |= keep
    protected |= {key for key, message in view.items() if isinstance(message, dict) and message.get('done') is False}
    replayed = _replayed(view, branch)
    position = {key: index for index, key in enumerate(branch)}
    # Other branches first (oldest first), then the current branch's messages before its latest checkpoint.
    off_branch = sorted(
        (key for key in slots if key not in replayed and key not in protected),
        key=lambda key: (key in position, position.get(key, 0), view[key].get('timestamp') or 0),
    )
    tiers = [off_branch, [key for key in branch if key in replayed and key not in protected]]
    for tier in tiers:
        lengths = [length for key in tier for _, _, length in slots[key]]
        cap = _cap_for(lengths, max(limits.chat_bulk - (total - sum(lengths)), 0))
        if cap is None:
            continue  # this tier is empty or already small; the next one has to give
        for key in tier:
            total -= _shorten_to(view, slots, key, max(cap, limits.floor), changed)
        for key in tier:
            if total <= limits.chat_bulk:
                return
            total -= _shorten_to(view, slots, key, limits.old_preview, changed)
        # Still over with every copy a short preview (thousands of results): oldest copies keep only a note.
        removed_note = len(REMOVED_NOTES['tool'].format(total=10**9)) * 3 // 2
        for key in tier:
            if total <= limits.chat_bulk:
                return
            total -= _shorten_to(view, slots, key, 0, changed, above=removed_note)
        if total <= limits.chat_bulk:
            return


def bound_messages_map(messages, current_id: str | None = None, keep=()) -> tuple[dict, set[str]]:
    """Bound every message, then the whole chat. Return the map (a new one if anything changed) and changed ids.

    The newest messages of the current branch, replies still being written and ``keep`` are never shortened for the
    chat budget (they still get the per-message bounds). Copies the model no longer
    reads (other branches, and messages before the latest compaction) are shortened first, largest first; copies it
    still replays only when those were not enough.
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


def bound_chat_with_changes(chat) -> tuple[dict, set[str]]:
    """Bound a chat body (history and its ``messages`` list). Return it and the ids of changed history messages."""
    if not enabled() or not isinstance(chat, dict):
        return chat, set()
    history = chat.get('history')
    changed: set[str] = set()
    result = chat
    new_map = None
    if isinstance(history, dict) and isinstance(history.get('messages'), dict):
        current_id = history.get('currentId') or chat.get('currentId') or chat.get('branchPointMessageId')
        new_map, changed = bound_messages_map(history['messages'], current_id)
        if changed:
            result = {**chat, 'history': {**history, 'messages': new_map}}
    listed = chat.get('messages')
    if isinstance(listed, list):
        bounded_list, list_changed = [], False
        for message in listed:
            key = message.get('id') if isinstance(message, dict) else None
            if key in changed:
                bounded = new_map[key]
            else:
                bounded = bound_message(message)
            list_changed = list_changed or bounded is not message
            bounded_list.append(bounded)
        if list_changed:
            result = {**result, 'messages': bounded_list}
    return result, changed


def bound_chat(chat):
    return bound_chat_with_changes(chat)[0]


def bounded_fields(message: dict) -> dict:
    """The columns a chat-budget change can touch, for writing a message's chat_message row."""
    return {key: message[key] for key in ('output', 'sources', 'files') if key in message}
