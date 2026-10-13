"""
Tool search: large tool schemas are left out of the provider `tools` array and listed by name in an
`<available_tools>` system prompt block. `search_tools` returns the full definitions of matching tools,
which are registered in subsequent provider requests. Compatible backends can opt into native inline
loading to preserve the cached prefix; other backends receive the schemas in the top-level tool list.
"""

import fnmatch
import re
import unicodedata

import regex
from open_webui.models.config import Config
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import add_or_update_system_message

SEARCH_TOOL_NAME = 'search_tools'
MANIFEST_DESCRIPTION_MAX_CHARS = 100
MAX_SEARCH_COUNT = 20
DEFAULT_SEARCH_COUNT = 5

_MANIFEST_RE = re.compile(r'\n?<available_tools>\n.*?</available_tools>', re.DOTALL)


async def get_tool_search_config() -> dict | None:
    """Tool search settings, or None when the feature is off."""
    if not await Config.get('chat.tool_search.enable'):
        return None
    values = await Config.get_many(
        'chat.tool_search.defer_threshold',
        'chat.tool_search.always_loaded',
        'chat.tool_search.defer_builtin_tools',
    )
    return {
        'defer_threshold': values['chat.tool_search.defer_threshold'],
        'always_loaded': values['chat.tool_search.always_loaded'],
        'defer_builtin_tools': values['chat.tool_search.defer_builtin_tools'],
    }


def select_deferred_tools(tools_dict: dict[str, dict], config: dict) -> list[str]:
    """Sorted names of tools whose schema exceeds the threshold and that are not always loaded."""
    patterns = config['always_loaded']
    return sorted(
        name
        for name, tool in tools_dict.items()
        if (config['defer_builtin_tools'] or tool.get('type') != 'builtin')
        and not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
        and len(JSONCodec.dumps(tool.get('spec') or {})) > config['defer_threshold']
    )


def _truncate_description(description: str | None) -> str:
    text = ' '.join(str(description or '').split())
    if len(text) > MANIFEST_DESCRIPTION_MAX_CHARS:
        text = text[: MANIFEST_DESCRIPTION_MAX_CHARS - 1].rstrip() + '…'
    return text


def build_deferred_tools_manifest(tools_dict: dict[str, dict], deferred: list[str]) -> str:
    entries = ''
    for name in deferred:
        description = _truncate_description(tools_dict[name].get('spec', {}).get('description'))
        entries += f'<tool>\n<name>{name}</name>\n<description>{description}</description>\n</tool>\n'

    return (
        '<available_tools>\n'
        'The following tools are available but their definitions are not loaded. '
        f'To use one, call `{SEARCH_TOOL_NAME}` with a short keyword query or the exact tool name to get its '
        'definition, then call the tool by name with the parameters it defines. '
        'Never tell the user a capability is unavailable without searching first.\n'
        f'{entries}</available_tools>'
    )


async def apply_tool_search(form_data: dict, metadata: dict, tools_dict: dict[str, dict]) -> set[str]:
    """Add search_tools and the manifest when tools can be deferred; returns the names to leave out of `tools`."""
    config = SEARCH_TOOL_NAME not in tools_dict and await get_tool_search_config()
    deferred = select_deferred_tools(tools_dict, config) if config else []
    if not deferred:
        return set()

    from open_webui.tools.builtin import search_tools
    from open_webui.utils.tools import (
        get_async_tool_function_and_apply_extra_params,
        get_builtin_function_introspection,
        get_builtin_tool_spec,
    )

    metadata['deferred_tools'] = deferred
    form_data['messages'] = add_or_update_system_message(
        build_deferred_tools_manifest(tools_dict, deferred),
        form_data['messages'],
        append=True,
    )
    tools_dict[SEARCH_TOOL_NAME] = {
        'tool_id': f'builtin:{SEARCH_TOOL_NAME}',
        'callable': await get_async_tool_function_and_apply_extra_params(
            search_tools, {'__metadata__': metadata}, get_builtin_function_introspection(search_tools)
        ),
        'spec': get_builtin_tool_spec(search_tools),
        'type': 'builtin',
    }
    return set(deferred)


def strip_deferred_tools_manifest(system_prompt: str | None) -> str | None:
    """Remove the manifest from a system prompt that is reused by a request which builds its own."""
    return _MANIFEST_RE.sub('', system_prompt) if system_prompt else system_prompt


def search_tool_definitions(query: str, count: int, metadata: dict) -> str:
    """Return matching schemas and remember which tools the next provider request must declare."""
    tools = metadata['tools']
    candidates = {name: tools[name]['spec'] for name in metadata['deferred_tools'] if name in tools}
    matches = search_deferred_tools(query, candidates, count)
    if not matches:
        return JSONCodec.dumps(
            {'tools': [], 'message': 'No matching tools found. Try different keywords or the exact tool name.'}
        )
    discovered = metadata.setdefault('discovered_tools', [])
    discovered.extend(name for name in matches if name not in discovered)
    return JSONCodec.dumps({'tools': [candidates[name] for name in matches]})


def _search_result_tool_names(content) -> list[str]:
    if isinstance(content, list):
        references = [
            part['tool_name']
            for part in content
            if isinstance(part, dict)
            and part.get('type') == 'tool_reference'
            and isinstance(part.get('tool_name'), str)
        ]
        if references:
            return references
        content = ''.join(
            part['text']
            for part in content
            if isinstance(part, dict)
            and part.get('type') in ('text', 'input_text')
            and isinstance(part.get('text'), str)
        )
    if not isinstance(content, str):
        return []
    try:
        result = JSONCodec.loads(content)
    except (TypeError, ValueError):
        return []
    specs = result.get('tools') if isinstance(result, dict) else None
    if not isinstance(specs, list):
        return []
    return [spec['name'] for spec in specs if isinstance(spec, dict) and isinstance(spec.get('name'), str)]


def add_discovered_tools(form_data: dict, metadata: dict) -> None:
    """Declare discovered tools, including discoveries replayed after a new turn or approval resume."""
    deferred = set(metadata.get('deferred_tools') or [])
    if not deferred or not form_data.get('tools'):
        return

    tools = metadata.get('tools', {})
    discovered = dict.fromkeys(metadata.get('discovered_tools') or [])
    search_calls = set()
    for message in form_data.get('messages', []):
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls') or []:
                name = call.get('function', {}).get('name')
                if name == SEARCH_TOOL_NAME and call.get('id'):
                    search_calls.add(call['id'])
                elif name in deferred:
                    discovered[name] = None
        elif message.get('role') == 'tool' and message.get('tool_call_id') in search_calls:
            discovered.update(dict.fromkeys(_search_result_tool_names(message.get('content'))))

    # History only supplies names. Use the current access-checked registry for all definitions.
    metadata['discovered_tools'] = [name for name in discovered if name in deferred and name in tools]
    existing = {tool.get('function', {}).get('name') for tool in form_data['tools']}
    additions = [
        {'type': 'function', 'function': tools[name]['spec']}
        for name in metadata['discovered_tools']
        if name not in existing
    ]
    if additions:
        form_data['tools'] = [*form_data['tools'], *additions]


def _replay_openai_discoveries(history, converted, load_tools, deferred):
    search_calls = set()
    for message in history:
        if message.get('type') == 'function_call':
            if message.get('name') == SEARCH_TOOL_NAME:
                search_calls.add(message.get('call_id'))
            else:
                load_tools([message.get('name')])
        elif message.get('type') == 'additional_tools':
            load_tools(tool.get('name') for tool in message.get('tools', []))
            continue
        if message.get('type') == 'function_call_output' and message.get('call_id') in search_calls:
            names = _search_result_tool_names(message.get('output'))
            if names:
                names = list(dict.fromkeys(name for name in names if name in deferred))
                # Definitions belong in the native item, not also in the text tool result.
                message = {**message, 'output': JSONCodec.dumps({'tools': [{'name': name} for name in names]})}
            converted.append(message)
            load_tools(names)
        else:
            converted.append(message)


def _replay_anthropic_discoveries(history, converted, load_tools, deferred, loaded):
    search_calls = set()
    for message in history:
        if message.get('role') == 'assistant':
            calls = message.get('tool_calls') or []
            search_calls.update(
                call.get('id') for call in calls if call.get('function', {}).get('name') == SEARCH_TOOL_NAME
            )
            load_tools(call.get('function', {}).get('name') for call in calls)
        elif message.get('role') == 'tool' and message.get('tool_call_id') in search_calls:
            names = list(
                dict.fromkeys(name for name in _search_result_tool_names(message.get('content')) if name in deferred)
            )
            if names:
                message = {
                    **message,
                    'content': [{'type': 'tool_reference', 'tool_name': name} for name in names],
                }
                loaded.update(names)
        converted.append(message)


def _use_native_tool_search(mode: str, is_responses: bool) -> bool:
    if mode == 'compatibility':
        return False
    if mode not in ('openai', 'anthropic'):
        raise ValueError('tool_search_mode must be compatibility, openai, or anthropic')
    if (mode == 'openai') != is_responses:
        raise ValueError(
            'tool_search_mode=openai requires a Responses API connection; '
            'tool_search_mode=anthropic requires a Chat Completions connection with native tool-reference support'
        )
    return True


def apply_native_tool_search(payload: dict, metadata: dict | None, mode: str, *, is_responses: bool) -> None:
    """Translate discoveries at the provider boundary without changing stored chat history.

    The mode is explicit because OpenAI-compatible gateways can route arbitrary model aliases
    to different providers. Inline definitions always come from the current authorized registry.
    """
    if not _use_native_tool_search(mode, is_responses):
        return
    metadata = metadata or {}
    registry = metadata.get('tools') or {}
    deferred = {name: registry[name]['spec'] for name in metadata.get('deferred_tools', []) if name in registry}
    if not deferred or not payload.get('tools'):
        return

    def tool_name(tool):
        return tool.get('function', tool).get('name') if tool.get('type') == 'function' else None

    # Undo compatibility declarations before sending a stable provider-native tools list.
    payload['tools'] = [tool for tool in payload['tools'] if tool_name(tool) not in deferred]
    if mode == 'anthropic':
        payload['tools'] += [
            {'type': 'function', 'function': {**spec, 'defer_loading': True}} for spec in deferred.values()
        ]

    history_key = 'input' if is_responses else 'messages'
    history = payload.get(history_key) or []
    loaded = set()
    converted = []
    # Restoration IDs must not collide with calls in a retained transcript.
    call_ids = {call.get('id') for message in history for call in message.get('tool_calls') or []}

    def load_tools(names):
        names = list(dict.fromkeys(name for name in names if name in deferred and name not in loaded))
        if not names:
            return
        if is_responses:
            converted.append(
                {
                    'type': 'additional_tools',
                    'role': 'developer',
                    'tools': [
                        {'type': 'function', **deferred[name], 'strict': deferred[name].get('strict', False)}
                        for name in names
                    ],
                }
            )
        else:
            call_id = f'owui_search_restore_{len(converted)}'
            while call_id in call_ids:
                call_id += '_'
            call_ids.add(call_id)
            converted.extend(
                [
                    {
                        'role': 'assistant',
                        'content': None,
                        'tool_calls': [
                            {
                                'id': call_id,
                                'type': 'function',
                                'function': {
                                    'name': SEARCH_TOOL_NAME,
                                    'arguments': JSONCodec.dumps({'query': ' '.join(names)}),
                                },
                            }
                        ],
                    },
                    {
                        'role': 'tool',
                        'tool_call_id': call_id,
                        'content': [{'type': 'tool_reference', 'tool_name': name} for name in names],
                    },
                ]
            )
        loaded.update(names)

    if is_responses:
        _replay_openai_discoveries(history, converted, load_tools, deferred)
    else:
        _replay_anthropic_discoveries(history, converted, load_tools, deferred, loaded)

    # Compaction can remove the discovery while this run still knows the selected tools.
    load_tools(metadata.get('discovered_tools') or [])
    payload[history_key] = converted


# Letters, digits and combining marks, so vowel signs in scripts like Devanagari stay attached to their word.
_WORD_RE = regex.compile(r'[\p{L}\p{N}\p{M}]+')
_CAMEL_RE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')
# Scripts written without spaces between words: Han, Hiragana, Katakana and Hangul.
_CJK_RE = re.compile(r'([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+)')


def tokenize(text: str | None) -> list[str]:
    """Lowercase word tokens split on camelCase boundaries, with CJK runs split into character bigrams."""
    tokens = []
    for word in _WORD_RE.findall(unicodedata.normalize('NFKC', text or '')):
        for part in _CAMEL_RE.split(word):
            for chunk in _CJK_RE.split(part.casefold()):
                if _CJK_RE.fullmatch(chunk):
                    tokens.extend([chunk[i : i + 2] for i in range(len(chunk) - 1)] or [chunk])
                elif chunk:
                    tokens.append(chunk)
    return tokens


def _document_text(name: str, spec: dict) -> str:
    properties = (spec.get('parameters') or {}).get('properties') or {}
    return ' '.join([name, str(spec.get('description') or ''), *properties.keys()])


def search_deferred_tools(query: str, candidates: dict[str, dict], count: int = DEFAULT_SEARCH_COUNT) -> list[str]:
    """Rank candidate tools (name -> spec) against the query; an exact tool name wins outright."""
    from rank_bm25 import BM25Okapi

    query = (query or '').strip()
    if query in candidates:
        return [query]

    query_tokens = tokenize(query)
    if not candidates or not query_tokens:
        return []

    names = list(candidates)
    documents = [tokenize(_document_text(name, candidates[name])) or ['_'] for name in names]
    # BM25 IDF is zero for a term found in half the corpus, common with small tool sets,
    # so raw token overlap keeps every matching term rankable.
    bm25 = BM25Okapi(documents).get_scores(query_tokens)
    scores = {
        name: max(0.0, float(bm25[index])) + len(set(query_tokens) & set(documents[index]))
        for index, name in enumerate(names)
    }

    ranked = sorted((name for name in names if scores[name] > 0), key=lambda name: (-scores[name], name))
    return ranked[: max(1, min(count, MAX_SEARCH_COUNT))]
