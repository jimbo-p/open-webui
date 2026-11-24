"""Keep saved chats small enough to load, save and replay.

A chat is loaded and saved whole on every turn and every open, so one ~900 MB chat took a 16 GB task out. The rules:

1. Only saved copies are bounded: tool results, citation excerpts, and copies of file text. What people type, what
   models write, ask_user answers, approval and agent resolutions, and hidden context the model must keep whole
   (preloaded skills, manifests, recalled memories) are never changed.
2. History is the one source of truth. The top-level ``messages`` list holds the bounded history's copy of every
   message it names, and a citation's copy of a file or page (which nothing reads) is not kept. An attachment's text
   is dropped only when a file record of the chat's owner holds the same text.
3. Recent work is protected whatever branch a save points at: replies still being written and the message being
   saved, then, newest first and up to the watermark share of the budget, everything written since the last few
   prompts and everything under the fork the user is working at (the parent of the newest prompt and of the newest
   message).
4. Over the budget, copies are shortened in a fixed order, each group all the way before the next: citation
   excerpts (never read by the model), then tool results the model no longer replays (other branches, before a
   compaction checkpoint), then replayed tool results. Replayed results never go below a short preview, even if the
   chat then stays over budget; only unread copies become notes.
5. A shortened text keeps its start and end and says how much was removed, counted from the original length, so the
   bounds are stable on their own output; caps are stepped, and an over-budget chat is brought down to a watermark
   where previews allow, so steady growth does not re-shorten on every save.

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
# Caps are rounded down to these multiples of the preview floor, so they do not creep save by save.
STEP_MULTIPLES = (1, 2.5, 5, 10, 25, 50, 100, 200)
# An over-budget chat is brought down to this share of the budget where previews allow it.
WATERMARK = 0.75
# A file's copied text dropped from a message is marked, so a reader knows to use the file record.
OMITTED_MARK = 'content_in_file_record'

_compaction = {'enabled': False}


def enabled() -> bool:
    return bool(env.CHAT_STORAGE_BOUNDS)


def note_compaction_enabled(value: bool) -> None:
    """Record whether context compaction is on. Until it is known to be on, whole branches count as replayed."""
    _compaction['enabled'] = bool(value)


def _limits() -> SimpleNamespace:
    # Read at call time so an operator change or a test override applies without a restart of this module.
    return SimpleNamespace(
        tool_result=env.TOOL_RESULT_MAX_CHARS,
        message_tool=env.CHAT_MESSAGE_TOOL_OUTPUT_MAX_CHARS,
        source_document=env.CHAT_SOURCE_DOCUMENT_MAX_CHARS,
        message_sources=env.CHAT_MESSAGE_SOURCES_MAX_CHARS,
        inline_file=env.CHAT_INLINE_FILE_CONTENT_MAX_CHARS,
        chat_bulk=env.CHAT_STORED_BULK_MAX_CHARS,
        recent_turns=env.CHAT_BOUND_PROTECTED_TURNS,
        floor=env.CHAT_TRIM_PREVIEW_MIN_CHARS,
        old_preview=env.CHAT_TRIM_OLD_PREVIEW_CHARS,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Shortening one text


def _text_of(value) -> str | None:
    if isinstance(value, list):
        parts = [part.get('text', '') for part in value if isinstance(part, dict) and isinstance(part.get('text'), str)]
        return ' '.join(parts)
    return value if isinstance(value, str) else None


def has_bound_note(value) -> bool:
    """True when a saved text (or list of text parts) carries a note these bounds wrote."""
    text = _text_of(value)
    return bool(text) and _NOTE.search(text) is not None


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


# ---------------------------------------------------------------------------------------------------------------------
# What may be shortened in one message


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


def saved_text_length(message) -> int:
    """Characters of a message's bounded texts (tool results and citation excerpts)."""
    return sum(length for _, _, length in _slots(message)) if isinstance(message, dict) else 0


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


def _attachment_drops(files, droppable: dict, prefix: tuple) -> list[tuple]:
    """Attachments whose copied text matches a file record of the chat's owner (``droppable``: id -> text length)."""
    return [
        (*prefix, index)
        for index, item in enumerate(files if isinstance(files, list) else [])
        if _is_attachment(item) and _inline_text_length(item) and droppable.get(item['id']) == _inline_text_length(item)
    ]


def _source_copy_drops(sources) -> list[tuple]:
    """Citation sources holding a copy of a file's or page's text. Nothing reads it there (the excerpt is in
    'document' and the attachment keeps its own text), so it is never kept."""
    drops = []
    for index, source in enumerate(sources if isinstance(sources, list) else []):
        item = source.get('source') if isinstance(source, dict) else None
        if not isinstance(item, dict):
            continue
        if _inline_text_length(item):
            drops.append(('sources', index, 'source', 'file'))
        docs = item.get('docs')
        if isinstance(docs, list) and any(isinstance(doc, dict) and doc.get('content') for doc in docs):
            drops.append(('sources', index, 'source', 'docs'))
    return drops


def _apply(message: dict, targets: dict[tuple, tuple[str, int]], drops: list[tuple]) -> dict:
    if not targets and not drops:
        return message
    bounded = copy.deepcopy(message)  # containers are copied; the strings themselves are shared, not duplicated
    for path, (kind, target) in targets.items():
        container = _get(bounded, path[:-1])
        container[path[-1]] = shorten(container[path[-1]], target, kind)
    for path in drops:
        found = _get(bounded, path)
        if path[-1] == 'docs':
            for doc in found:
                if isinstance(doc, dict):
                    doc.pop('content', None)
        elif path[-1] == 'file':
            found['data'].pop('content', None)
        else:
            found['file']['data'].pop('content', None)
            found['file']['data'][OMITTED_MARK] = True
    return bounded


def _fit_group(capped: list[int], budget: int, limits: SimpleNamespace) -> list[int]:
    """Target lengths for one message's texts of one kind (oldest first) within ``budget``, never below the short
    preview: largest first to a stepped preview, then oldest first to the short preview."""
    cap = _cap_for(capped, budget)
    if cap is None:
        return capped
    cap = _step_down(max(cap, limits.floor), limits.floor)
    values = [min(value, cap) for value in capped]
    total = sum(values)
    for index, value in enumerate(values):
        if total <= budget:
            break
        if value > limits.old_preview:
            total -= value - limits.old_preview
            values[index] = limits.old_preview
    return values


def _message_targets(slots: list[tuple[str, tuple, int]], limits: SimpleNamespace) -> dict[tuple, tuple[str, int]]:
    targets = {}
    for kind, per_item, budget in (
        ('tool', limits.tool_result, limits.message_tool),
        ('source', limits.source_document, limits.message_sources),
    ):
        group = [(path, length) for slot_kind, path, length in slots if slot_kind == kind]
        values = _fit_group([min(length, per_item) for _, length in group], budget, limits)
        for (path, length), target in zip(group, values):
            if target < length:
                targets[path] = (kind, target)
    return targets


def _bound_with_slots(message, limits: SimpleNamespace, droppable: dict):
    slots = _slots(message)
    targets = _message_targets(slots, limits)
    drops = _attachment_drops(message.get('files'), droppable, ('files',)) + _source_copy_drops(message.get('sources'))
    if not targets and not drops:
        return message, slots
    bounded = _apply(message, targets, drops)
    return bounded, _slots(bounded)


def bound_message(message, droppable_files=None):
    """Return ``message``, or a bounded copy of it when one of its saved texts is over its limit.

    ``droppable_files`` maps attachment ids to the text length of a file record the chat's owner holds; only an
    attachment whose copied text has that length is dropped.
    """
    if not enabled() or not isinstance(message, dict):
        return message
    try:
        return _bound_with_slots(message, _limits(), dict(droppable_files or {}))[0]
    except Exception:
        log.exception('Could not bound a saved chat message; keeping it as it is')
        return message


def bound_files(files, droppable_files=None):
    """Drop copied text of checked attachments from a list of file items (a chat's top-level ``files``)."""
    if not enabled() or not isinstance(files, list):
        return files
    try:
        drops = _attachment_drops(files, dict(droppable_files or {}), ('files',))
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
    lists.extend(m.get('files') for m in (listed if isinstance(listed, list) else []) if isinstance(m, dict))
    return {
        item['id']
        for files in lists
        for item in (files if isinstance(files, list) else [])
        if _is_attachment(item) and _inline_text_length(item) > limit
    }


# ---------------------------------------------------------------------------------------------------------------------
# Which messages are protected and which the model replays


def _stamp(view: dict, key: str) -> float:
    value = view[key].get('timestamp')
    return value if isinstance(value, (int, float)) else 0


def _children(view: dict) -> dict:
    children: dict = {}
    for key, message in view.items():
        children.setdefault(message.get('parentId'), []).append(key)
    return children


def _subtree(children: dict, root) -> set[str]:
    """Every message under ``root``, on all branches."""
    found, stack = set(), list(children.get(root, []))
    while stack:
        key = stack.pop()
        if key not in found:
            found.add(key)
            stack.extend(children.get(key, []))
    return found


def _leaf(view: dict, children: dict, message_id: str) -> str:
    """Follow the newest child from ``message_id`` to a leaf, as ChatTable._last_descendant_id does."""
    seen = set()
    while message_id in view and message_id not in seen:
        seen.add(message_id)
        listed = view[message_id].get('childrenIds')
        ids = [child for child in listed if isinstance(child, str)] if isinstance(listed, list) else []
        # Rows carry no childrenIds; their parent links say the same, in the order they were written.
        ids += sorted((c for c in children.get(message_id, []) if c not in ids), key=lambda c: _stamp(view, c))
        message_id = next(
            (c for c in reversed(ids) if c not in seen and c in view and view[c].get('parentId') == message_id),
            message_id,
        )
    return message_id


def _branch(view: dict, leaf: str | None) -> list[str]:
    branch, seen = [], set()
    while leaf and leaf in view and leaf not in seen:
        seen.add(leaf)
        branch.append(leaf)
        leaf = view[leaf].get('parentId')
    return branch[::-1]


def _replayed(view: dict, branch: list[str]) -> set[str]:
    """Branch messages the model still reads: all of them, unless compaction is on and a checkpoint cuts earlier."""
    if not _compaction['enabled']:
        return set(branch)
    try:
        from open_webui.utils.context_compaction import _apply_latest_summary_checkpoint

        kept, _ = _apply_latest_summary_checkpoint([{**view[key], 'id': key} for key in branch])
        return {message.get('id') for message in kept} & set(branch)
    except Exception:
        return set(branch)


@dataclass
class Layout:
    """Which messages the chat budget never shortens, and which the model replays."""

    protected: set = field(default_factory=set)
    replayed: set = field(default_factory=set)


def _recent(view: dict, children: dict, by_age: list[str], turns: int) -> set[str]:
    """Everything written since the last ``turns`` prompts, and everything under the parent of the newest prompt
    and of the newest message (the fork a regeneration, an edit or another model's reply hangs from)."""
    recent: set[str] = set()
    prompts = [key for key in by_age if view[key].get('role') == 'user']
    if prompts:
        since = _stamp(view, prompts[-max(turns, 1) :][0])
        recent = {key for key in view if _stamp(view, key) >= since}
        recent |= _subtree(children, view[prompts[-1]].get('parentId'))
    if by_age:
        recent |= _subtree(children, view[by_age[-1]].get('parentId'))
    return recent


def chat_layout(messages: dict, current_id: str | None = None, keep=()) -> Layout:
    """Protected and replayed messages of a message map (rules 3 and 4 of the module docstring)."""
    limits = _limits()
    view = {key: value for key, value in messages.items() if isinstance(value, dict)}
    children = _children(view)
    by_age = sorted(view, key=lambda key: (_stamp(view, key), key))
    layout = Layout(protected={key for key in keep if key in view})
    layout.protected |= {key for key in view if view[key].get('done') is False}
    used = sum(saved_text_length(view[key]) for key in layout.protected)
    # Recent work may take up to the watermark, so the rest of the conversation keeps room for its own previews.
    allowance = int(limits.chat_bulk * WATERMARK)
    recent = _recent(view, children, by_age, limits.recent_turns) - layout.protected
    for key in sorted(recent, key=lambda key: (_stamp(view, key), key), reverse=True):
        size = saved_text_length(view[key])
        if used + size > allowance:
            break  # what does not fit is shortened like any older copy
        layout.protected.add(key)
        used += size
    for start in ([current_id] if current_id in view else []) + by_age[-1:]:
        layout.replayed |= _replayed(view, _branch(view, _leaf(view, children, start)))
    return layout


# ---------------------------------------------------------------------------------------------------------------------
# The chat budget


def _total(slots: dict) -> int:
    return sum(length for message_slots in slots.values() for _, _, length in message_slots)


def _lengths(slots: dict, group: tuple[str, str]) -> list[int]:
    key, kind = group
    return [length for slot_kind, _, length in slots[key] if slot_kind == kind]


class _Fit:
    def __init__(self, view: dict, slots: dict, limits: SimpleNamespace, changed: set[str]):
        self.view, self.slots, self.limits, self.changed = view, slots, limits, changed
        self.total = _total(slots)

    def _shorten(self, group: tuple[str, str], cap: int, above: int) -> None:
        """Shorten one message's texts of one kind longer than ``above`` to ``cap``.

        A text that already carries a saved-copy note is left alone unless it is well above the new cap.
        """
        key, kind = group
        message = self.view[key]
        targets = {}
        for slot_kind, path, length in self.slots[key]:
            if slot_kind != kind or length <= above:
                continue
            if length <= cap * 3 // 2 and has_bound_note(_get(message, path)):
                continue
            targets[path] = (kind, cap)
        if not targets:
            return
        before = sum(length for _, _, length in self.slots[key])
        self.view[key] = _apply(message, targets, [])
        self.changed.add(key)
        self.slots[key] = _slots(self.view[key])
        self.total -= before - sum(length for _, _, length in self.slots[key])

    def shrink(self, groups: list, target: int, *, short_target: int, notes: bool) -> None:
        """Bring ``groups`` (oldest first) down: one stepped preview cap, largest first, aiming at ``target``; then,
        while over ``short_target``, oldest first to the short preview; then, if ``notes``, to a note."""
        if not groups:
            return
        if self.total > target:
            lengths = [length for group in groups for length in _lengths(self.slots, group)]
            cap = _cap_for(lengths, max(target - (self.total - sum(lengths)), 0))
            if cap is not None:
                cap = _step_down(max(cap, self.limits.floor), self.limits.floor)
                for group in groups:
                    self._shorten(group, cap, cap)
        for group in groups:
            if self.total <= short_target:
                break
            self._shorten(group, self.limits.old_preview, self.limits.old_preview)
        if not notes:
            return
        for group in groups:
            if self.total <= short_target:
                break
            self._shorten(group, 0, _removed_note_length(group[1]) * 3 // 2)


def _fit_chat(view: dict, slots: dict, layout: Layout, limits: SimpleNamespace, changed: set[str]) -> None:
    """Shorten saved copies once the chat is over its budget (rule 4 of the module docstring)."""
    fit = _Fit(view, slots, limits, changed)
    if fit.total <= limits.chat_bulk:
        return
    order = sorted((key for key in slots if key not in layout.protected), key=lambda key: (_stamp(view, key), key))
    citations = [(key, 'source') for key in order]
    unread = [(key, 'tool') for key in order if key not in layout.replayed]
    replayed = [(key, 'tool') for key in order if key in layout.replayed]
    # The watermark applies where unread copies (down to notes) and replayed previews can reach it.
    lowest = fit.total
    for group in citations + unread:
        lowest -= sum(max(n - _removed_note_length(group[1]), 0) for n in _lengths(slots, group))
    for group in replayed:
        lowest -= sum(max(n - limits.floor, 0) for n in _lengths(slots, group))
    watermark = int(limits.chat_bulk * WATERMARK)
    target = watermark if lowest <= watermark else limits.chat_bulk
    fit.shrink(citations, target, short_target=target, notes=True)
    fit.shrink(unread, target, short_target=target, notes=True)
    fit.shrink(replayed, target, short_target=limits.chat_bulk, notes=False)


def bound_messages_map(
    messages, current_id: str | None = None, keep=(), layout: Layout | None = None, droppable_files=None
) -> tuple[dict, set[str]]:
    """Bound every message, then the whole chat. Return the map (a new one if anything changed) and changed ids.

    ``layout`` (from chat_layout) lets a caller apply the decisions made on one copy of a chat to another copy, such
    as its chat_message rows.
    """
    if not enabled() or not isinstance(messages, dict):
        return messages, set()
    try:
        limits = _limits()
        droppable = dict(droppable_files or {})
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


# ---------------------------------------------------------------------------------------------------------------------
# A chat body


def bound_chat_with_changes(chat, keep=(), droppable_files=None) -> tuple[dict, set[str]]:
    """Bound a chat body (history, its ``messages`` list and ``files``). Return it and the changed history ids.

    ``keep`` names messages a caller is saving right now: they get their own bounds but are never shortened for the
    chat budget.
    """
    if not enabled() or not isinstance(chat, dict):
        return chat, set()
    try:
        return _bound_chat(chat, keep, dict(droppable_files or {}))
    except Exception:
        log.exception('Could not bound a saved chat; keeping it as it is')
        return chat, set()


def _mirror(listed: list, history_map: dict, droppable: dict) -> list | None:
    """The ``messages`` list with the history's copy of every message the history holds; None when unchanged."""
    mirrored, changed = [], False
    for message in listed:
        key = message.get('id') if isinstance(message, dict) else None
        in_history = history_map.get(key) if isinstance(key, str) else None
        if in_history is None:
            bounded = bound_message(message, droppable)
        elif message is in_history or message == in_history:
            bounded = message
        else:
            bounded = in_history
        changed = changed or bounded is not message
        mirrored.append(bounded)
    return mirrored if changed else None


def _bound_chat(chat: dict, keep, droppable: dict) -> tuple[dict, set[str]]:
    history = chat.get('history')
    changed: set[str] = set()
    result = chat
    history_map = {}
    if isinstance(history, dict) and isinstance(history.get('messages'), dict):
        current_id = history.get('currentId') or chat.get('currentId') or chat.get('branchPointMessageId')
        current_id = current_id if isinstance(current_id, str) else None
        history_map, changed = bound_messages_map(history['messages'], current_id, keep, droppable_files=droppable)
        if changed:
            result = {**chat, 'history': {**history, 'messages': history_map}}
    if isinstance(chat.get('messages'), list):
        mirrored = _mirror(chat['messages'], history_map, droppable)
        if mirrored is not None:
            result = {**result, 'messages': mirrored}
    files = chat.get('files')
    bounded_chat_files = bound_files(files, droppable)
    if bounded_chat_files is not files:
        result = {**result, 'files': bounded_chat_files}
    return result, changed


def bound_chat(chat, droppable_files=None):
    return bound_chat_with_changes(chat, droppable_files=droppable_files)[0]


def keep_saved_shortening(stored, incoming):
    """When a client saves back a longer copy of a text the bounds had shortened, keep the stored copy.

    The frontend keeps the copies it streamed, or was served before a later cut, and posts them back; taking them
    would undo the bounds and make the same rows swing between shortened and longer on later saves.
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
            old, new = _get(stored, path), _get(incoming, path)
            if len(old) >= length or not has_bound_note(old) or _original_length(old) != _original_length(new):
                continue
            removed = old.startswith('[Saved copy removed') or old.startswith('[Citation excerpt removed')
            if removed or old[:32] == new[:32]:
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
