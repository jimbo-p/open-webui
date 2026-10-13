"""Deferred tools must be declared before a strict provider can emit their calls."""

import copy
import json

import pytest
from open_webui.utils.misc import convert_output_to_messages
from open_webui.utils.tool_search import (
    add_discovered_tools,
    apply_native_tool_search,
    search_tool_definitions,
    select_deferred_tools,
)


@pytest.fixture
def tool_context():
    def tool(name, description, tool_type='external'):
        return {
            'type': tool_type,
            'spec': {
                'name': name,
                'description': description,
                'parameters': {'type': 'object', 'properties': {'body': {'type': 'string'}}},
            },
        }

    registry = {
        'tool_sendEmail_post': tool('tool_sendEmail_post', 'Send the current user an email'),
        'notify': tool('notify', 'Send the user a notification', 'builtin'),
        'unrelated': tool('unrelated', 'Look up microapp storage status'),
    }
    metadata = {'tools': registry, 'deferred_tools': list(registry)}
    form_data = {
        'messages': [{'role': 'user', 'content': 'Send me an email saying hello'}],
        'tools': [{'type': 'function', 'function': {'name': 'search_tools'}}],
    }
    return form_data, metadata


def names(form_data):
    return [tool['function']['name'] for tool in form_data['tools'] if tool['type'] == 'function']


def search_history(result):
    # Use the same saved-output conversion as chat continuation and approval resume.
    return convert_output_to_messages(
        [
            {
                'type': 'function_call',
                'call_id': 'search-1',
                'name': 'search_tools',
                'arguments': '{"query":"tool_sendEmail_post"}',
                'status': 'completed',
            },
            {
                'type': 'function_call_output',
                'call_id': 'search-1',
                'output': [{'type': 'input_text', 'text': result}],
                'status': 'completed',
            },
        ],
        raw=True,
    )


@pytest.mark.parametrize('target', ['tool_sendEmail_post', 'notify'])
def test_search_registers_external_and_builtin_tools_for_next_request(tool_context, target):
    form_data, metadata = tool_context
    add_discovered_tools(form_data, metadata)
    assert names(form_data) == ['search_tools']

    result = json.loads(search_tool_definitions(target, 5, metadata))
    follow_up = {**form_data}
    add_discovered_tools(follow_up, metadata)

    assert names(follow_up) == ['search_tools', target]
    assert follow_up['tools'][-1]['function'] == result['tools'][0]
    assert names(form_data) == ['search_tools']  # Do not mutate earlier requests through a shared list.


def test_multiple_searches_accumulate_without_duplicates_after_compaction(tool_context):
    form_data, metadata = tool_context
    search_tool_definitions('notify', 5, metadata)
    add_discovered_tools(form_data, metadata)
    first_tools = copy.deepcopy(form_data['tools'])
    search_tool_definitions('tool_sendEmail_post', 5, metadata)
    search_tool_definitions('notify', 5, metadata)
    # Compaction/stateful continuation may leave no search result in the next request.
    form_data['messages'] = [{'role': 'system', 'content': 'Compacted history'}]
    add_discovered_tools(form_data, metadata)
    add_discovered_tools(form_data, metadata)
    assert names(form_data) == ['search_tools', 'notify', 'tool_sendEmail_post']
    assert form_data['tools'][:2] == first_tools


def test_new_turn_and_approval_resume_restore_search_results_with_current_schemas(tool_context):
    form_data, metadata = tool_context
    result = search_tool_definitions('tool_sendEmail_post', 5, metadata)
    metadata.pop('discovered_tools')  # A resumed request gets fresh metadata.
    form_data['messages'].extend(search_history(result))
    metadata['tools']['tool_sendEmail_post']['spec']['description'] = 'Current authorized definition'
    add_discovered_tools(form_data, metadata)
    assert names(form_data) == ['search_tools', 'tool_sendEmail_post']
    assert form_data['tools'][-1]['function']['description'] == 'Current authorized definition'


def test_previous_tool_calls_remain_callable_when_search_history_is_gone(tool_context):
    form_data, metadata = tool_context
    form_data['messages'].append(
        {'role': 'assistant', 'tool_calls': [{'id': 'email-1', 'function': {'name': 'tool_sendEmail_post'}}]}
    )
    add_discovered_tools(form_data, metadata)
    assert names(form_data) == ['search_tools', 'tool_sendEmail_post']


def test_history_cannot_restore_removed_tools_or_supply_definitions(tool_context):
    form_data, metadata = tool_context
    form_data['messages'].extend(
        search_history(json.dumps({'tools': [{'name': 'removed'}, {'name': 'notify', 'parameters': {'bad': True}}]}))
    )
    metadata['discovered_tools'] = ['removed']
    add_discovered_tools(form_data, metadata)
    assert names(form_data) == ['search_tools', 'notify']
    assert form_data['tools'][-1]['function'] == metadata['tools']['notify']['spec']


@pytest.mark.parametrize('content', ['not JSON', 'null', '[]', '{"tools":null}', '{"tools":[null,{"name":[]}]}'])
def test_invalid_search_results_do_not_break_continuation(tool_context, content):
    form_data, metadata = tool_context
    form_data['messages'].extend(search_history(content))
    add_discovered_tools(form_data, metadata)
    assert names(form_data) == ['search_tools']


def test_unrelated_tool_results_and_user_text_do_not_activate_tools(tool_context):
    form_data, metadata = tool_context
    content = json.dumps({'tools': [metadata['tools']['notify']['spec']]})
    form_data['messages'] += [
        {'role': 'user', 'content': content},
        {'role': 'tool', 'tool_call_id': 'not-a-search', 'content': content},
    ]
    add_discovered_tools(form_data, metadata)
    assert names(form_data) == ['search_tools']


def test_no_matches_and_disabled_search_leave_payload_unchanged(tool_context):
    form_data, metadata = tool_context
    before = copy.deepcopy(form_data)
    assert json.loads(search_tool_definitions('zzzzzz', 5, metadata))['tools'] == []
    add_discovered_tools(form_data, metadata)
    assert form_data == before
    metadata.pop('deferred_tools')
    metadata['discovered_tools'] = ['notify']
    add_discovered_tools(form_data, metadata)
    assert form_data == before


def test_existing_definitions_and_provider_tools_are_preserved(tool_context):
    form_data, metadata = tool_context
    form_data['tools'] += [
        {'type': 'web_search'},
        {'type': 'function', 'function': {'name': 'notify', 'description': 'Existing definition'}},
    ]
    before = copy.deepcopy(form_data['tools'])
    search_tool_definitions('notify', 5, metadata)
    add_discovered_tools(form_data, metadata)
    assert form_data['tools'] == before


def test_explicit_empty_tools_are_preserved(tool_context):
    form_data, metadata = tool_context
    search_tool_definitions('notify', 5, metadata)
    form_data['tools'] = []
    add_discovered_tools(form_data, metadata)
    assert form_data['tools'] == []


def test_builtin_deferral_setting_and_always_loaded_patterns_are_preserved(tool_context):
    _, metadata = tool_context
    config = {'defer_threshold': 0, 'always_loaded': ['tool_*'], 'defer_builtin_tools': False}
    assert select_deferred_tools(metadata['tools'], config) == ['unrelated']
    config['defer_builtin_tools'] = True
    assert select_deferred_tools(metadata['tools'], config) == ['notify', 'unrelated']


def native_payload(form_data, metadata, mode):
    from open_webui.routers.openai import convert_to_responses_payload

    payload = copy.deepcopy(form_data)
    add_discovered_tools(payload, metadata)
    if mode == 'openai':
        payload = convert_to_responses_payload(payload)
    apply_native_tool_search(payload, metadata, mode, is_responses=mode == 'openai')
    return payload


@pytest.mark.parametrize('mode', ['openai', 'anthropic'])
@pytest.mark.parametrize('target', ['tool_sendEmail_post', 'notify'])
def test_native_discovery_preserves_tools_and_history_prefix(tool_context, mode, target):
    form_data, metadata = tool_context
    first = native_payload(form_data, metadata, mode)
    result = search_tool_definitions(target, 5, metadata)
    form_data['messages'].extend(search_history(result))
    original = copy.deepcopy(form_data)
    second = native_payload(form_data, metadata, mode)
    assert second['tools'] == first['tools']
    assert form_data == original  # The saved transcript stays provider-neutral.
    if mode == 'openai':
        assert second['input'][: len(first['input'])] == first['input']
        assert second['input'][-1] == {
            'type': 'additional_tools',
            'role': 'developer',
            'tools': [{'type': 'function', **metadata['tools'][target]['spec'], 'strict': False}],
        }
        assert [t['name'] for t in second['tools']] == ['search_tools']
        assert json.loads(second['input'][-2]['output']) == {'tools': [{'name': target}]}
    else:
        assert second['messages'][: len(first['messages'])] == first['messages']
        assert second['messages'][-1]['content'] == [{'type': 'tool_reference', 'tool_name': target}]
        deferred = [t['function'] for t in second['tools'] if t['function'].get('defer_loading')]
        assert {t['name'] for t in deferred} == set(metadata['deferred_tools'])

    # A normal tool round, then a new request/approval resume with fresh metadata.
    form_data['messages'] += [
        {
            'role': 'assistant',
            'tool_calls': [{'id': 'run-1', 'type': 'function', 'function': {'name': target, 'arguments': '{}'}}],
        },
        {'role': 'tool', 'tool_call_id': 'run-1', 'content': 'done'},
        {'role': 'user', 'content': 'Continue'},
    ]
    metadata.pop('discovered_tools')
    third = native_payload(form_data, metadata, mode)
    key = 'input' if mode == 'openai' else 'messages'
    assert third[key][: len(second[key])] == second[key]
    assert third['tools'] == first['tools']


@pytest.mark.parametrize('mode', ['openai', 'anthropic'])
def test_native_multiple_discoveries_stay_at_their_original_positions(tool_context, mode):
    form_data, metadata = tool_context
    first = native_payload(form_data, metadata, mode)
    form_data['messages'].extend(search_history(search_tool_definitions('notify', 5, metadata)))
    second = native_payload(form_data, metadata, mode)
    later = search_history(search_tool_definitions('tool_sendEmail_post', 5, metadata))
    later[0]['tool_calls'][0]['id'] = 'search-2'
    later[1]['tool_call_id'] = 'search-2'
    form_data['messages'].extend(later)
    third = native_payload(form_data, metadata, mode)
    key = 'input' if mode == 'openai' else 'messages'
    assert third[key][: len(second[key])] == second[key]
    assert third['tools'] == second['tools'] == first['tools']


@pytest.mark.parametrize('mode', ['openai', 'anthropic'])
def test_native_replay_uses_current_schemas_and_excludes_revoked_tools(tool_context, mode):
    form_data, metadata = tool_context
    form_data['messages'].extend(search_history('{"tools":[{"name":"revoked"},{"name":"notify","parameters":{}}]}'))
    payload = native_payload(form_data, metadata, mode)
    if mode == 'openai':
        assert payload['input'][-1]['tools'] == [
            {'type': 'function', **metadata['tools']['notify']['spec'], 'strict': False}
        ]
    else:
        assert payload['messages'][-1]['content'] == [{'type': 'tool_reference', 'tool_name': 'notify'}]


@pytest.mark.parametrize('mode', ['openai', 'anthropic'])
@pytest.mark.parametrize('retained_call', [True, False])
def test_native_compaction_restores_discovery_before_use(tool_context, mode, retained_call):
    form_data, metadata = tool_context
    first = native_payload(form_data, metadata, mode)
    search_tool_definitions('notify', 5, metadata)
    if retained_call:
        form_data['messages'] += [
            {'role': 'assistant', 'tool_calls': [{'id': 'run-1', 'function': {'name': 'notify', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'run-1', 'content': 'done'},
        ]
    payload = native_payload(form_data, metadata, mode)
    assert payload['tools'] == first['tools']
    if mode == 'openai':
        assert payload['input'][1]['type'] == 'additional_tools'
        assert payload['input'][1]['tools'][0]['name'] == 'notify'
        if retained_call:
            assert payload['input'][2]['name'] == 'notify'
    else:
        assert payload['messages'][2]['content'] == [{'type': 'tool_reference', 'tool_name': 'notify'}]
        assert payload['messages'][1]['tool_calls'][0]['id'] == payload['messages'][2]['tool_call_id']
        if retained_call:
            assert payload['messages'][3]['tool_calls'][0]['function']['name'] == 'notify'


@pytest.mark.parametrize('mode', ['openai', 'anthropic'])
def test_native_does_not_load_from_unrelated_results_or_failed_search(tool_context, mode):
    form_data, metadata = tool_context
    form_data['messages'] += [
        {'role': 'tool', 'tool_call_id': 'unrelated', 'content': '{"tools":[{"name":"notify"}]}'},
        *search_history('Tool execution was denied'),
    ]
    payload = native_payload(form_data, metadata, mode)
    key = 'input' if mode == 'openai' else 'messages'
    assert len(payload[key]) == len(form_data['messages'])
    assert payload[key][-1].get('output', payload[key][-1].get('content')) == 'Tool execution was denied'


@pytest.mark.parametrize('mode', ['openai', 'anthropic'])
def test_native_explicit_empty_tools_remain_disabled(tool_context, mode):
    form_data, metadata = tool_context
    search_tool_definitions('notify', 5, metadata)
    form_data['tools'] = []
    assert native_payload(form_data, metadata, mode)['tools'] == []


@pytest.mark.parametrize('mode,is_responses', [('openai', False), ('anthropic', True), ('auto', True)])
def test_native_rejects_wrong_protocol_and_unknown_modes(tool_context, mode, is_responses):
    form_data, metadata = tool_context
    with pytest.raises(ValueError, match='tool_search_mode'):
        apply_native_tool_search(form_data, metadata, mode, is_responses=is_responses)


@pytest.mark.parametrize('mode', ['openai', 'anthropic'])
def test_native_repeated_preparation_does_not_duplicate_loading(tool_context, mode):
    form_data, metadata = tool_context
    form_data['messages'].extend(search_history(search_tool_definitions('notify', 5, metadata)))
    payload = native_payload(form_data, metadata, mode)
    expected = copy.deepcopy(payload)
    apply_native_tool_search(payload, metadata, mode, is_responses=mode == 'openai')
    assert payload == expected
