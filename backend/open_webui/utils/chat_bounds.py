"""Keep saved chats small enough to load, save and replay.

A chat is loaded and saved whole on every turn and every open, so one ~900 MB chat took a 16 GB task out. Only the
saved copies of tool results, citation excerpts and file text copied into a message are bounded here; what people
type and what models write is never changed. That includes answers people give to ask_user, approval and agent
resolutions, and the hidden context the model must keep whole (preloaded skills, manifests, recalled memories).
Attachment text is dropped only where the caller has checked that the file record holds it.

A shortened text keeps its start and end and says how much was removed (from the original length, also when it is
shortened again), so running the bounds again on their own output changes nothing; the frontend saves back what it
was served. Once a chat is over its budget it is brought down to a lower watermark with stepped caps where previews
allow it, so steady growth does not re-shorten the same copies on every save.

The newest turns are protected whatever branch a save points at: both the branch of the posted current id and the
branch of the newest message are kept whole near their ends, with their siblings and the replies under them.
Citation excerpts, which the model never reads, are shortened before any tool result the model replays.

Inputs are never modified: a message that needs bounding is copied first, because callers such as the tool loop
keep using their live objects for the model.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

from open_webui import env

log = logging.getLogger(__name__)

TOOL_NOTE = (
    '\n\n[Saved copy shortened: {removed:,} of {total:,} characters were removed from the middle to keep this '
    'chat small; the full output is not kept.]\n\n'
)
SOURCE_NOTE = (
    '\n\n[Citation excerpt shortened: {removed:,} of {total:,} characters were removed from the middle. '
    'Open the source to read all of it.]\n\n'
)
NOTES = {'tool': TOOL_NOTE, 'source': SOURCE_NOTE}
# Used when nothing of a copy is kept: only when a chat or message is still over budget with every copy a preview.
REMOVED_NOTES = {
    'tool': '[Saved copy removed: this tool result ({total:,} characters) was not kept, to keep this chat small.]',
    'source': '[Citation excerpt removed ({total:,} characters) to keep this chat small. Open the source to read it.]',
}
_NOTE = re.compile(
    r'\[(?:Saved copy|Citation excerpt) shortened: [\d,]+ of ([\d,]+) characters were removed from the middle'
    r'|\[(?:Saved copy removed: this tool result|Citation excerpt removed) \(([\d,]+) characters\)'
)
# Tool calls whose results hold what a person answered or context the model must keep whole.
KEPT_CALL_NAMES = frozenset({'ask_user'})
# Chat-budget caps are rounded down to these multiples of the preview floor, so they do not creep save by save.
STEP_MULTIPLES = (1, 2.5, 5, 10, 25, 50, 100, 200)
# After the chat budget is exceeded, the chat is brought down to this share of it where previews allow.
WATERMARK = 0.75
# A file's copied text dropped from a message is marked, so a reader knows to use the file record.
OMITTED_MARK = 'content_in_file_record'

_compaction = {'enabled': False}


def enabled() -> bool:
    return bool(env.CHAT_STORAGE_BOUNDS)


def note_compaction_enabled(value: bool) -> None:
    """Record whether context compaction is on (read with its config on every request).

    Until it is known to be on, every message of the current branch counts as replayed.
    """
    _compaction['enabled'] = bool(value)


def _text_of(value) -> str | None:
    if isinstance(value, list):
        return ' '.join(
            part.get('text', '') for part in value if isinstance(part, dict) and isinstance(part.get('text'), str)
        )
    return value if isinstance(value, str) else None


def has_bound_note(value) -> bool:
    """True when a saved text (or list of text parts) carries a note these bounds wrote."""
    text = _text_of(value)
    return bool(text) and _NOTE.search(text) is not None


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
    match = _NOTE.search(text)
    if not match:
        return len(text)
    return max(int((match.group(1) or match.group(2)).replace(',', '')), len(text))


def _removed_note_length(kind: str) -> int:
    return len(REMOVED_NOTES[kind].format(total=10**9))


def shorten(text: str, target: int, kind: str = 'tool') -> str:
    """Keep the start and end of ``text`` within ``target`` characters, with a note in the middle."""
    if len(text) <= target:
        return text
    total = _original_length(text)
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
    """(kind, path, length) of every bounded text in one message, tool results in the order they ran."""
    return _tool_slots(message.get('output')) + _source_slots(message.get('sources'))


def _get(message: dict, path: tuple):
    value = message
    for key in path:
        value = value[key]
    return value


def _inline_text_length(item) -> int:
    data = item.get('file', {}).get('data') if isinstance(item, dict) and isinstance(item.get('file'), dict) else None
    return len(data['content']) if isinstance(data, dict) and isinstance(data.get('content'), str) else 0


def _is_attachment(item) -> bool:
    return isinstance(item, dict) and item.get('type') == 'file' and isinstance(item.get('id'), str) and item['id']


def _file_drops(files, limits: SimpleNamespace, droppable: frozenset, prefix: tuple) -> list[tuple]:
    return [
        (*prefix, index)
        for index, item in enumerate(files if isinstance(files, list) else [])
        if _is_attachment(item) and item['id'] in droppable and _inline_text_length(item) > limits.inline_file
    ]


def _inline_drops(message: dict, limits: SimpleNamespace, droppable: frozenset) -> list[tuple]:
    """File items whose copied text is dropped.

    An attachment's text only when the caller checked that its file record holds it (``droppable``); a citation
    source's copy of a file or page always, since the excerpt is in 'document' and the attachment itself keeps it.
    """
    drops = _file_drops(message.get('files'), limits, droppable, ('files',))
    sources = message.get('sources')
    for index, source in enumerate(sources if isinstance(sources, list) else []):
        if isinstance(source, dict) and _inline_text_length(source.get('source')) > limits.source_document:
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
        data = _get(bounded, path)['file']['data']
        data.pop('content', None)
        if path[0] != 'sources':
            data[OMITTED_MARK] = True
    return bounded


def _fit_group(capped: list[int], budget: int, limits: SimpleNamespace, kind: str) -> list[int]:
    """Target lengths for one message's texts of one kind (oldest first) within ``budget``."""
    cap = _cap_for(capped, budget)
    if cap is None:
        return capped
    cap = _step_down(max(cap, limits.floor), limits.floor)
    values = [min(value, cap) for value in capped]
    total = sum(values)
    # More results than budget / preview: the oldest ones go to a short preview, then to a note.
    note = _removed_note_length(kind)
    for preview, above in ((limits.old_preview, limits.old_preview), (0, note * 3 // 2)):
        for index, value in enumerate(values):
            if total <= budget:
                return values
            if value > above:
                new = preview if preview else note
                total -= value - new
                values[index] = preview
    return values


def _message_targets(slots: list[tuple[str, tuple, int]], limits: SimpleNamespace) -> dict[tuple, tuple[str, int]]:
    targets = {}
    for kind, per_item, budget in (
        ('tool', limits.tool_result, limits.message_tool),
        ('source', limits.source_document, limits.message_sources),
    ):
        group = [(path, length) for slot_kind, path, length in slots if slot_kind == kind]
        values = _fit_group([min(length, per_item) for _, length in group], budget, limits, kind)
        for (path, length), target in zip(group, values):
            if target < length:
                targets[path] = (kind, target)
    return targets


def _bound_with_slots(message, limits: SimpleNamespace, droppable: frozenset = frozenset()):
    slots = _slots(message)
    targets = _message_targets(slots, limits)
    drops = _inline_drops(message, limits, droppable)
    if not targets and not drops:
        return message, slots
    bounded = _apply(message, targets, drops)
    return bounded, _slots(bounded)


def bound_message(message, droppable_files: frozenset = frozenset()):
    """Return ``message``, or a bounded copy of it when one of its saved texts is over its limit."""
    if not enabled() or not isinstance(message, dict):
        return message
    try:
        return _bound_with_slots(message, _limits(), frozenset(droppable_files))[0]
    except Exception:
        log.exception('Could not bound a saved chat message; keeping it as it is')
        return message


def bound_files(files, droppable_files: frozenset = frozenset()):
    """Drop checked attachments' copied text from a list of file items (a chat's top-level ``files``)."""
    if not enabled() or not isinstance(files, list):
        return files
    try:
        drops = _file_drops(files, _limits(), frozenset(droppable_files), ('files',))
        return _apply({'files': files}, {}, drops)['files']
    except Exception:
        log.exception('Could not bound chat files; keeping them as they are')
        return files


def inline_file_ids(chat) -> set[str]:
    """Ids of attachments whose copied text is over the inline limit, in a chat body."""
    if not enabled() or not isinstance(chat, dict):
        return set()
    limit = _limits().inline_file
    history = chat.get('history') if isinstance(chat.get('history'), dict) else {}
    messages = history.get('messages') if isinstance(history.get('messages'), dict) else {}
    lists = [message.get('files') for message in messages.values() if isinstance(message, dict)]
    lists.append(chat.get('files'))
    listed = chat.get('messages')
    lists.extend(
        message.get('files') for message in (listed if isinstance(listed, list) else []) if isinstance(message, dict)
    )
    return {
        item['id']
        for files in lists
        for item in (files if isinstance(files, list) else [])
        if _is_attachment(item) and _inline_text_length(item) > limit
    }


# ---------------------------------------------------------------------------------------------------------------------
# The chat budget


def _children(view: dict) -> dict:
    children: dict = {}
    for key, message in view.items():
        if isinstance(message, dict):
            children.setdefault(message.get('parentId'), []).append(key)
    return children


def _child_ids(view: dict, children: dict, message_id: str) -> list[str]:
    message = view.get(message_id)
    listed = message.get('childrenIds') if isinstance(message, dict) else None
    ids = [child for child in listed if isinstance(child, str)] if isinstance(listed, list) else []
    # Rows carry no childrenIds; their parent links say the same, in the order they were written.
    extra = sorted(
        (child for child in children.get(message_id, []) if child not in ids),
        key=lambda child: view[child].get('timestamp') or 0,
    )
    return ids + extra


def _next_child(view: dict, children: dict, message_id: str, seen: set) -> str | None:
    return next(
        (
            child_id
            for child_id in reversed(_child_ids(view, children, message_id))
            if child_id not in seen
            and isinstance(child := view.get(child_id), dict)
            and child.get('role')
            and child.get('parentId') == message_id
        ),
        None,
    )


def _down(view: dict, children: dict, message_id: str, limit: int | None = None) -> list[str]:
    """``message_id`` and its newest descendants (as ChatTable._last_descendant_id follows them)."""
    path, seen = [], set()
    while message_id in view and message_id not in seen and (limit is None or len(path) < limit):
        seen.add(message_id)
        path.append(message_id)
        message_id = _next_child(view, children, message_id, seen)
    return path


def _branch(messages: dict, leaf: str | None) -> list[str]:
    """Ids from the root to ``leaf``."""
    branch, seen = [], set()
    message_id = leaf
    while message_id and message_id in messages and message_id not in seen:
        seen.add(message_id)
        branch.append(message_id)
        message = messages[message_id]
        message_id = message.get('parentId') if isinstance(message, dict) else None
    return branch[::-1]


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


@dataclass
class Layout:
    """Which messages the chat budget never shortens, and which the model replays."""

    protected: set = field(default_factory=set)
    replayed: set = field(default_factory=set)


def chat_layout(messages: dict, current_id: str | None = None, keep=()) -> Layout:
    """Protected and replayed messages of a message map.

    Protected: near the end of both the posted current id's branch and the newest message's branch (each followed
    to its newest descendant), every other reply to those messages' parents with the replies under it, replies still
    being written, and ``keep``. Replayed: both branches (after the latest checkpoint while compaction is on).
    """
    limits = _limits()
    view = {key: value for key, value in messages.items() if isinstance(value, dict)}
    children = _children(view)
    starts = []
    if current_id in view:
        starts.append(current_id)
    with_roles = [key for key, message in view.items() if message.get('role')]
    if with_roles:
        starts.append(max(with_roles, key=lambda key: view[key].get('timestamp') or 0))
    layout = Layout(protected=set(keep))
    depth = max(limits.protected, 0)
    for start in starts:
        branch = _branch(view, _down(view, children, start)[-1])
        layout.replayed |= _replayed(view, branch)
        for key in branch[-depth:] if depth else []:
            layout.protected.add(key)
            # Other replies to the same prompt or prompts after the same answer, and what was said under them
            # (the previous regeneration, the answer to the prompt before it was edited, multi-model replies).
            for sibling in children.get(view[key].get('parentId'), []):
                layout.protected.update(_down(view, children, sibling, depth))
    layout.protected |= {key for key, message in view.items() if message.get('done') is False}
    return layout


def _total(slots: dict) -> int:
    return sum(length for message_slots in slots.values() for _, _, length in message_slots)


def _shorten_to(
    view: dict, slots: dict, key: str, kind: str, cap: int, changed: set[str], above: int | None = None
) -> int:
    """Shorten one message's texts of ``kind`` longer than ``above`` (default ``cap``) to ``cap``; return chars removed.

    A text that already carries a saved-copy note is left alone unless it is well above the new cap.
    """
    above = cap if above is None else above
    message = view[key]
    targets = {}
    for slot_kind, path, length in slots[key]:
        if slot_kind != kind or length <= above:
            continue
        if length <= cap * 3 // 2 and has_bound_note(_get(message, path)):
            continue
        targets[path] = (kind, cap)
    if not targets:
        return 0
    before = sum(length for _, _, length in slots[key])
    view[key] = _apply(message, targets, [])
    changed.add(key)
    slots[key] = _slots(view[key])
    return before - sum(length for _, _, length in slots[key])


def _tiers(view: dict, slots: dict, layout: Layout) -> tuple[list, list[list]]:
    """(message id, kind) groups that may be shortened, oldest first.

    Citation excerpts (which the model never reads), then the tool results the model no longer reads (other
    branches, before a compaction checkpoint), then the tool results it replays.
    """
    age = {key: (view[key].get('timestamp') or 0, index) for index, key in enumerate(view)}
    unprotected = sorted((key for key in slots if key not in layout.protected), key=lambda key: age[key])
    sources = [(key, 'source') for key in unprotected]
    unread = [(key, 'tool') for key in unprotected if key not in layout.replayed]
    replayed = [(key, 'tool') for key in unprotected if key in layout.replayed]
    return sources, [unread, replayed]


def _lengths(slots: dict, group: tuple[str, str]) -> list[int]:
    key, kind = group
    return [length for slot_kind, _, length in slots[key] if slot_kind == kind]


class _Fit:
    def __init__(self, view: dict, slots: dict, limits: SimpleNamespace, changed: set[str]):
        self.view, self.slots, self.limits, self.changed = view, slots, limits, changed
        self.total = _total(slots)

    def preview(self, tier: list, target: int) -> None:
        """Largest first, to one stepped cap no lower than the generous preview, aiming at ``target``."""
        if self.total <= target:
            return
        lengths = [length for group in tier for length in _lengths(self.slots, group)]
        cap = _cap_for(lengths, max(target - (self.total - sum(lengths)), 0))
        if cap is None:
            return
        cap = _step_down(max(cap, self.limits.floor), self.limits.floor)
        for key, kind in tier:
            self.total -= _shorten_to(self.view, self.slots, key, kind, cap, self.changed)

    def oldest_first(self, groups: list, target: int, note: bool) -> None:
        """Oldest first, to a short preview (or to a note), until the chat is within ``target``."""
        for key, kind in groups:
            if self.total <= target:
                return
            cap = 0 if note else self.limits.old_preview
            above = _removed_note_length(kind) * 3 // 2 if note else None
            self.total -= _shorten_to(self.view, self.slots, key, kind, cap, self.changed, above=above)


def _fit_chat(view: dict, slots: dict, layout: Layout, limits: SimpleNamespace, changed: set[str]) -> None:
    """Shorten saved copies across the chat, in ``view``, once it is over the chat budget.

    Citation excerpts first, all the way (a preview, a short preview, then a note) before any tool result. Then tool
    results, each step across both tiers before the next: stepped caps no lower than a generous preview, then oldest
    first a short preview, then a note, the last two only while the chat is still over the budget itself. The aim is
    WATERMARK of the budget, or the budget itself when the protected messages leave no room for it with previews.
    """
    fit = _Fit(view, slots, limits, changed)
    if fit.total <= limits.chat_bulk:
        return
    sources, tool_tiers = _tiers(view, slots, layout)
    tool_groups = [group for tier in tool_tiers for group in tier]
    lowest = fit.total - sum(sum(_lengths(slots, group)) for group in sources + tool_groups)
    lowest += sum(min(length, _removed_note_length('source')) for group in sources for length in _lengths(slots, group))
    lowest += sum(min(length, limits.floor) for group in tool_groups for length in _lengths(slots, group))
    watermark = int(limits.chat_bulk * WATERMARK)
    target = watermark if lowest <= watermark else limits.chat_bulk
    fit.preview(sources, target)
    fit.oldest_first(sources, target, note=False)
    fit.oldest_first(sources, target, note=True)
    for tier in tool_tiers:
        fit.preview(tier, target)
    fit.oldest_first(tool_groups, limits.chat_bulk, note=False)
    fit.oldest_first(tool_groups, limits.chat_bulk, note=True)


def bound_messages_map(
    messages, current_id: str | None = None, keep=(), layout: Layout | None = None, droppable_files=frozenset()
) -> tuple[dict, set[str]]:
    """Bound every message, then the whole chat. Return the map (a new one if anything changed) and changed ids.

    ``layout`` (from chat_layout) lets a caller apply the decisions made on one copy of a chat to another copy, such
    as its chat_message rows.
    """
    if not enabled() or not isinstance(messages, dict):
        return messages, set()
    try:
        limits = _limits()
        droppable = frozenset(droppable_files)
        view = dict(messages)
        changed, slots = set(), {}
        for key, message in messages.items():
            if not isinstance(message, dict):
                continue
            bounded, slots[key] = _bound_with_slots(message, limits, droppable)
            if bounded is not message:
                view[key] = bounded
                changed.add(key)
        if _total(slots) > limits.chat_bulk:
            _fit_chat(view, slots, layout or chat_layout(view, current_id, keep), limits, changed)
        if changed:
            log.info('Bounded %d saved messages to keep a chat within %d characters', len(changed), limits.chat_bulk)
        return (view if changed else messages), changed
    except Exception:
        log.exception('Could not bound a saved chat; keeping it as it is')
        return messages, set()


def bound_chat_with_changes(chat, keep=(), droppable_files=frozenset()) -> tuple[dict, set[str]]:
    """Bound a chat body (history, its ``messages`` list and ``files``). Return it and the changed history ids.

    ``keep`` names messages a caller is saving right now: they get their own bounds but are never shortened for the
    chat budget. ``droppable_files`` names attachments whose file record was checked to hold their text.
    """
    if not enabled() or not isinstance(chat, dict):
        return chat, set()
    try:
        return _bound_chat(chat, keep, frozenset(droppable_files))
    except Exception:
        log.exception('Could not bound a saved chat; keeping it as it is')
        return chat, set()


def _bound_chat(chat: dict, keep, droppable: frozenset) -> tuple[dict, set[str]]:
    history = chat.get('history')
    changed: set[str] = set()
    result = chat
    new_map = {}
    if isinstance(history, dict) and isinstance(history.get('messages'), dict):
        current_id = history.get('currentId') or chat.get('currentId') or chat.get('branchPointMessageId')
        current_id = current_id if isinstance(current_id, str) else None
        new_map, changed = bound_messages_map(history['messages'], current_id, keep, droppable_files=droppable)
        if changed:
            result = {**chat, 'history': {**history, 'messages': new_map}}
    listed = chat.get('messages')
    if isinstance(listed, list):
        bounded_list, list_changed = [], False
        for message in listed:
            key = message.get('id') if isinstance(message, dict) else None
            bounded = new_map[key] if isinstance(key, str) and key in changed else bound_message(message, droppable)
            list_changed = list_changed or bounded is not message
            bounded_list.append(bounded)
        if list_changed:
            result = {**result, 'messages': bounded_list}
    files = chat.get('files')
    bounded_chat_files = bound_files(files, droppable)
    if bounded_chat_files is not files:
        result = {**result, 'files': bounded_chat_files}
    return result, changed


def bound_chat(chat, droppable_files=frozenset()):
    return bound_chat_with_changes(chat, droppable_files=droppable_files)[0]


def keep_saved_shortening(stored, incoming):
    """When a client saves back the full copy of a text the bounds had shortened, keep the shortened copy.

    The frontend keeps the copies it streamed and posts them back whole; taking them would undo the bounds and make
    the same rows swing between shortened and full on later saves.
    """
    if not enabled() or not isinstance(stored, dict) or not isinstance(incoming, dict) or stored is incoming:
        return incoming
    if incoming.get('done') is False:
        return incoming
    try:
        stored_paths = {path for _, path, _ in _slots(stored)}
        restore = {}
        for _, path, length in _slots(incoming):
            if path not in stored_paths or not _same_item(stored, incoming, path):
                continue
            old = _get(stored, path)
            if len(old) < length and has_bound_note(old) and _original_length(old) == length:
                if (
                    old.startswith('[Saved copy removed')
                    or old.startswith('[Citation excerpt removed')
                    or (old[:32] == _get(incoming, path)[:32])
                ):
                    restore[path] = old
        if not restore:
            return incoming
        kept = copy.deepcopy(incoming)
        for path, old in restore.items():
            _get(kept, path[:-1])[path[-1]] = old
        return kept
    except Exception:
        log.exception('Could not compare a saved message with its saved copy')
        return incoming


def _same_item(stored: dict, incoming: dict, path: tuple) -> bool:
    a, b = _get(stored, path[:2]), _get(incoming, path[:2])
    if path[0] == 'output':
        return isinstance(a, dict) and isinstance(b, dict) and a.get('call_id') == b.get('call_id')
    source_a = (a.get('source') or {}) if isinstance(a, dict) else {}
    source_b = (b.get('source') or {}) if isinstance(b, dict) else {}
    return isinstance(source_a, dict) and isinstance(source_b, dict) and source_a.get('id') == source_b.get('id')


def bounded_fields(message: dict) -> dict:
    """The columns a chat-budget change can touch, for writing a message's chat_message row."""
    return {key: message[key] for key in ('output', 'sources', 'files') if key in message}


def saved_text_length(message) -> int:
    """Characters of a message's bounded texts (tool results and citation excerpts)."""
    return sum(length for _, _, length in _slots(message)) if isinstance(message, dict) else 0
