"""Managed memory text follows the publisher while personal text stays editable."""

import copy

import pytest

from open_webui.utils.memory_prompt import (
    MemoryPromptError,
    canonical_block,
    replace_block,
    request_messages,
    settings_patch,
)

OLD = '<oxy_memory>old memory</oxy_memory>'
NEW = '<oxy_memory>current memory</oxy_memory>'


def owned(block=NEW):
    return {'oxy_memory': {'managed': True, 'version': 1}, 'ui': {'system': 'Personal\n\n' + block}}


def test_surgical_replacement_preserves_whitespace_and_text_on_both_sides():
    before, after = '  Personal\r\n\n', '\n\nUser suffix  '
    assert replace_block(before + OLD + after, NEW) == before + NEW + after
    assert replace_block(before + OLD + after, '') == before + after


def test_initial_append_empty_prompt_and_repeated_refresh():
    assert replace_block('', NEW) == NEW
    initial = replace_block('Personal  ', OLD)
    refreshed = replace_block(initial, NEW)
    assert refreshed == 'Personal  \n\n' + NEW
    assert replace_block(refreshed, NEW) == refreshed


@pytest.mark.parametrize(
    'text',
    [
        '<oxy_memory>unfinished',
        '</oxy_memory>reversed<oxy_memory>',
        '<oxy_memory>nested<oxy_memory>x</oxy_memory></oxy_memory>',
    ],
)
def test_malformed_owned_ranges_reject_without_guessing(text):
    with pytest.raises(MemoryPromptError):
        replace_block(text, NEW)


def test_unmanaged_marker_text_remains_user_text():
    messages = [{'role': 'system', 'content': '<oxy_memory>unfinished'}]
    assert request_messages(messages, {'ui': {'system': NEW}}) is messages
    patch = {'ui': {'system': OLD}, 'oxy_memory': {'managed': True, 'version': 1}}
    assert settings_patch({}, patch) == {'ui': {'system': OLD}}


def test_stale_editor_preserves_new_personal_text_and_current_server_block():
    current = owned()
    patch = {'ui': {'system': 'New personal text\n\n' + OLD, 'theme': 'dark'}}
    untouched = copy.deepcopy((current, patch))
    result = settings_patch(current, patch)
    assert result == {'ui': {'system': 'New personal text\n\n' + NEW, 'theme': 'dark'}}
    assert (current, patch) == untouched
    assert settings_patch(current, {'ui': {'system': None}}) == {'ui': {'system': NEW}}


def test_cleared_owned_section_removes_stale_copy_but_keeps_personal_suffix():
    current = owned('')
    assert canonical_block(current) == ''
    messages = [{'role': 'system', 'content': 'Chat override ' + OLD + ' suffix'}]
    assert request_messages(messages, current)[0]['content'] == 'Chat override  suffix'


def test_request_uses_fresh_canonical_copy_and_keeps_messages():
    messages = [{'role': 'system', 'content': 'Chat override\n\n' + OLD}, {'role': 'user', 'content': 'Question'}]
    original = copy.deepcopy(messages)
    result = request_messages(messages, owned())
    assert result == [{'role': 'system', 'content': 'Chat override\n\n' + NEW}, messages[1]]
    assert messages == original
    assert request_messages(result, owned()) == result
    assert request_messages([messages[1]], owned()) == [{'role': 'system', 'content': NEW}, messages[1]]


def test_system_content_parts_preserve_nontext_and_part_metadata():
    parts = [
        {'type': 'text', 'text': 'Personal ' + OLD, 'annotation': 'kept'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,test'}},
        {'type': 'text', 'text': ' suffix ' + OLD},
    ]
    messages = [{'role': 'system', 'content': parts, 'name': 'context'}]
    original = copy.deepcopy(messages)
    result = request_messages(messages, owned())
    assert result[0] == {
        'role': 'system',
        'content': [
            {**parts[0], 'text': 'Personal ' + NEW},
            parts[1],
            {**parts[2], 'text': ' suffix '},
        ],
        'name': 'context',
    }
    assert messages == original


def test_canonical_duplicates_are_rejected():
    with pytest.raises(MemoryPromptError):
        canonical_block(owned(OLD + NEW))


def test_multiple_system_messages_receive_one_current_canonical_section():
    messages = [
        {'role': 'system', 'content': 'First personal ' + OLD},
        {'role': 'user', 'content': 'Question'},
        {'role': 'system', 'content': 'Second personal ' + OLD + ' suffix'},
        {
            'role': 'system',
            'content': [
                {'type': 'text', 'text': 'Part ' + OLD, 'annotation': 'kept'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,test'}},
            ],
        },
    ]
    original = copy.deepcopy(messages)
    result = request_messages(messages, owned())
    assert result[0]['content'] == 'First personal ' + NEW
    assert result[1] == messages[1]
    assert result[2]['content'] == 'Second personal  suffix'
    assert result[3]['content'] == [
        {'type': 'text', 'text': 'Part ', 'annotation': 'kept'},
        messages[3]['content'][1],
    ]
    assert messages == original
    assert request_messages(result, owned()) == result


@pytest.mark.asyncio
async def test_native_model_prompt_prepend_keeps_personal_prompt_and_one_current_memory():
    from open_webui.utils.payload import apply_system_prompt_to_body

    messages = request_messages(
        [
            {'role': 'system', 'content': 'Personal instructions\n\n' + OLD},
            {'role': 'user', 'content': 'Question'},
        ],
        owned(),
    )
    body = await apply_system_prompt_to_body('Main model instructions', {'messages': messages})
    assert body['messages'][0]['content'] == 'Main model instructions\nPersonal instructions\n\n' + NEW
    assert body['messages'][1] == {'role': 'user', 'content': 'Question'}


@pytest.mark.asyncio
async def test_native_agent_receives_refreshed_system_instructions(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from open_webui.utils.agents import runner

    transport = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), raise_if_failed=lambda: None)
    output = SimpleNamespace(state={}, items=[], flush=AsyncMock())
    monkeypatch.setattr(runner.TerminalTransport, 'resolve', AsyncMock(return_value=transport))
    monkeypatch.setattr(runner, 'Output', lambda *args: output)
    monkeypatch.setattr(runner, 'upload_files_to_terminal', AsyncMock())
    monkeypatch.setattr(runner, 'PROVIDERS', {'test-provider': AsyncMock()})
    messages = request_messages(
        [
            {'role': 'system', 'content': 'Personal instructions\n\n' + OLD},
            {'role': 'user', 'content': 'Question'},
        ],
        owned(),
    )
    await runner.run(
        SimpleNamespace(),
        {'messages': messages},
        SimpleNamespace(id='pilot'),
        {'chat_id': 'saved-chat', 'message_id': 'response', 'initial_terminal_cwd': '/tmp/test'},
        {'agent': {'provider': 'test-provider', 'terminal_id': 'terminal', 'model': 'test-model'}},
    )
    assert transport.start.await_args.args[1]['instructions'] == 'Personal instructions\n\n' + NEW


@pytest.mark.asyncio
async def test_malformed_owned_request_rejects_before_model_lookup_or_chat_creation():
    from types import SimpleNamespace

    from fastapi import HTTPException
    from open_webui.main import chat_completion

    # An app without state cannot look up models, create chats or launch a title
    # request. Validation must reject the malformed marker before touching it.
    request = SimpleNamespace(app=object())
    form = {'model': 'unused', 'messages': [{'role': 'system', 'content': '<oxy_memory>unfinished'}]}
    with pytest.raises(HTTPException) as error:
        await chat_completion(request, form, SimpleNamespace(settings=owned()))
    assert error.value.status_code == 409
