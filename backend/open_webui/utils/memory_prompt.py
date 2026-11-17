"""Reconcile a published memory section without owning the person's other prompt text.

The publisher stamps settings.oxy_memory={"managed": true, "version": 1} and
writes the canonical section in ui.system in the same locked transaction.
Only that publisher may change the ownership metadata. Marker text belonging to
an account without this metadata is ordinary user text and is left untouched.
"""

import re

METADATA_KEY = 'oxy_memory'
_MARKER = re.compile(r'</?oxy_memory>', re.IGNORECASE)


class MemoryPromptError(ValueError):
    pass


def _ranges(text: str) -> list[tuple[int, int]]:
    ranges = []
    start = None
    for marker in _MARKER.finditer(text):
        if marker.group().lower() == '<oxy_memory>':
            if start is not None:
                raise MemoryPromptError('Nested memory prompt markers.')
            start = marker.start()
        else:
            if start is None:
                raise MemoryPromptError('Unmatched memory prompt marker.')
            ranges.append((start, marker.end()))
            start = None
    if start is not None:
        raise MemoryPromptError('Unmatched memory prompt marker.')
    return ranges


def _settings_dict(settings) -> dict:
    if hasattr(settings, 'model_dump'):
        settings = settings.model_dump()
    return settings if isinstance(settings, dict) else {}


def canonical_block(settings) -> str | None:
    """None means unmanaged; an empty string means an owned, cleared section."""
    settings = _settings_dict(settings)
    ownership = settings.get(METADATA_KEY)
    if not (
        isinstance(ownership, dict)
        and ownership.get('managed') is True
        and type(ownership.get('version')) is int
        and ownership['version'] == 1
    ):
        return None
    ui = settings.get('ui')
    text = ui.get('system') if isinstance(ui, dict) else None
    if text is None:
        return ''
    if not isinstance(text, str):
        raise MemoryPromptError('Invalid managed system prompt.')
    ranges = _ranges(text)
    if len(ranges) > 1:
        raise MemoryPromptError('Multiple canonical memory sections.')
    return text[ranges[0][0] : ranges[0][1]] if ranges else ''


def replace_block(text: str, block: str) -> str:
    """Replace complete owned ranges; preserve every character outside them."""
    ranges = _ranges(text)
    if not ranges:
        if not block:
            return text
        separator = '' if not text or text.endswith('\n\n') else ('\n' if text.endswith('\n') else '\n\n')
        return text + separator + block
    pieces, previous = [], 0
    for position, (start, end) in enumerate(ranges):
        pieces.append(text[previous:start])
        if position == 0:
            pieces.append(block)
        previous = end
    pieces.append(text[previous:])
    return ''.join(pieces)


def settings_patch(current, updated: dict) -> dict:
    """A personal edit changes personal text; its memory copy is never authoritative."""
    updated = dict(updated)
    updated.pop(METADATA_KEY, None)
    block = canonical_block(current)
    ui = updated.get('ui')
    if block is not None and isinstance(ui, dict) and 'system' in ui:
        ui = dict(ui)
        incoming = ui['system']
        if incoming is not None and not isinstance(incoming, str):
            raise MemoryPromptError('Invalid system prompt.')
        ui['system'] = replace_block(incoming or '', block) if incoming is not None or block else None
        updated['ui'] = ui
    return updated


def _request_content(content, block: str):
    if isinstance(content, str):
        return replace_block(content, block)
    if not isinstance(content, list):
        raise MemoryPromptError('Invalid managed system prompt content.')
    # Validate every text part before constructing any replacement. Split
    # markers across parts fail safely instead of deleting uncertain text.
    has_section = False
    for part in content:
        if isinstance(part, dict) and part.get('type') == 'text':
            if not isinstance(part.get('text'), str):
                raise MemoryPromptError('Invalid system prompt text part.')
            has_section = bool(_ranges(part['text'])) or has_section
    inserted = False
    changed = []
    for part in content:
        if isinstance(part, dict) and part.get('type') == 'text' and _ranges(part['text']):
            changed.append({**part, 'text': replace_block(part['text'], '' if inserted else block)})
            inserted = True
        else:
            changed.append(part)
    if block and not has_section:
        changed.append({'type': 'text', 'text': block})
    return changed


def request_messages(messages: list[dict], settings) -> list[dict]:
    """Use the request's fresh user snapshot to refresh only its managed section.

    The first system message receives the current canonical section. Additional
    system messages retain all personal content while stale owned sections are
    removed, so the request contains one published memory section in total.
    """
    block = canonical_block(settings)
    if block is None:
        return messages
    changed = []
    found_system = False
    for message in messages:
        if message.get('role') == 'system':
            content = _request_content(message.get('content'), '' if found_system else block)
            changed.append({**message, 'content': content})
            found_system = True
        else:
            changed.append(message)
    if not found_system and block:
        changed.insert(0, {'role': 'system', 'content': block})
    return changed
