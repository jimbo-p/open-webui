"""Hidden skill and knowledge manifests are sent once per chat and again only when they change."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from open_webui.models import skills as skills_model
from open_webui.utils import middleware
from open_webui.utils.misc import get_last_tool_result, get_tool_results

MODEL_ID = 'gpt-test'
SKILL = SimpleNamespace(id='well-file', name='Well file', description='Reads well files.', is_active=True)
KNOWLEDGE = [{'type': 'collection', 'id': 'kb-1', 'name': 'Drilling policies'}]


def skills_manifest(*skills):
    entries = ''.join(
        f'<skill>\n<id>{s.id}</id>\n<name>{s.name}</name>\n<description>{s.description}</description>\n</skill>\n'
        for s in skills
    )
    return f'<available_skills>\n{entries}</available_skills>'


def knowledge_manifest():
    tag = '<knowledge type="collection" id="kb-1" name="Drilling policies" source="model"/>'
    return f'<attached_knowledge>\n{tag}\n</attached_knowledge>'


def assistant(message_id, text, *hidden):
    """An assistant reply as stored: this turn's hidden context items, then the reply."""
    output = [item for name, result in hidden for item in middleware.hidden_tool_result(name, result)]
    output.append(
        {
            'type': 'message',
            'id': f'msg-{message_id}',
            'status': 'completed',
            'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}],
        }
    )
    return {'id': message_id, 'role': 'assistant', 'model': MODEL_ID, 'content': text, 'output': output}


def user(message_id, text):
    return {'id': message_id, 'role': 'user', 'content': text}


@pytest.fixture
def chat(monkeypatch):
    """Run process_chat_payload for a temporary native-tools chat with external I/O stubbed out."""
    config = {
        'skills.lazy_loading.enable': False,
        'user.permissions': {},
        'memories.enable': True,
        'memories.system_context.enable': True,
        'task.model.default': '',
        'task.model.external': '',
    }
    state = SimpleNamespace(skills=[SKILL], summarized=None, compact=None, view_skill_calls=[])

    async def config_get(key, default=None):
        return config.get(key, default)

    async def config_get_many(*keys):
        return {key: config.get(key) for key in keys}

    async def view_skill(id):
        state.view_skill_calls.append(id)
        return f'# {id}\nFollow the well file steps.'

    async def builtin_tools(*args, **kwargs):
        return {'view_skill': {'spec': {'name': 'view_skill', 'parameters': {}}, 'callable': view_skill}}

    async def compact(request, form_data, user_, messages, metadata, model_id, models, system_prompt=''):
        state.summarized = copy.deepcopy(form_data['messages']) if form_data else None
        if state.compact is None:
            return messages, None, []
        count, summary = state.compact
        return messages[count:], summary, messages[:count]

    async def get_skills(user_id, ids=None):
        return list(state.skills)

    monkeypatch.setattr(middleware.Config, 'get', config_get)
    monkeypatch.setattr(middleware.Config, 'get_many', config_get_many)
    monkeypatch.setattr(skills_model.Skills, 'get_skills', get_skills)
    monkeypatch.setattr(middleware, 'get_filter_functions', AsyncMock(return_value=[]))
    monkeypatch.setattr(middleware, 'get_sorted_filters', lambda *args: [])
    monkeypatch.setattr(middleware, 'get_filter_context', lambda request: None)
    monkeypatch.setattr(
        middleware, 'process_pipeline_inlet_filter', AsyncMock(side_effect=lambda request, form_data, *a: form_data)
    )
    monkeypatch.setattr(
        middleware, 'process_filter_functions', AsyncMock(side_effect=lambda **kwargs: (kwargs['form_data'], {}))
    )
    monkeypatch.setattr(
        middleware, 'convert_url_images_to_base64', AsyncMock(side_effect=lambda form_data, user=None: form_data)
    )
    monkeypatch.setattr(middleware, 'get_event_emitter', AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(middleware, 'get_event_call', AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(middleware, 'get_system_oauth_token', AsyncMock(return_value=None))
    monkeypatch.setattr(middleware, 'has_permission', AsyncMock(return_value=True))
    monkeypatch.setattr(middleware, 'get_terminal_connections', AsyncMock(return_value=[]))
    monkeypatch.setattr(middleware, 'filter_user_terminal_tools', AsyncMock())
    monkeypatch.setattr(middleware, 'apply_tool_search', AsyncMock(return_value=set()))
    monkeypatch.setattr(middleware, 'get_builtin_tools', builtin_tools)
    monkeypatch.setattr(
        middleware, 'apply_summary_checkpoint', AsyncMock(side_effect=lambda messages: (messages, None))
    )
    monkeypatch.setattr(middleware, 'compact_messages_for_request', compact)
    monkeypatch.setattr(
        middleware,
        'preload_memory',
        AsyncMock(
            return_value=middleware.hidden_tool_result('recall_memories', '<memories>Prefers SI units.</memories>')
        ),
    )

    model = {
        'id': MODEL_ID,
        'name': 'Test model',
        'info': {
            'meta': {
                'capabilities': {'file_context': False, 'citations': True},
                'builtinTools': {},
                'knowledge': KNOWLEDGE,
            }
        },
    }
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(MODELS={MODEL_ID: model})), state=SimpleNamespace()
    )
    account = SimpleNamespace(id='user-1', role='user', name='Test User', email='user@example.com')

    async def run(messages, *, skill_ids=None, features=None):
        form_data = {
            'model': MODEL_ID,
            'messages': copy.deepcopy(messages),
            'stream': True,
            'skill_ids': skill_ids or [],
            'features': features or {},
        }
        metadata = {
            'chat_id': 'local:chat-1',
            'user_message_id': messages[-1]['id'],
            'message_id': 'reply',
            'session_id': 'session-1',
            'user_id': account.id,
            'params': {},
            'variables': {},
        }
        form_data, metadata, _, paused = await middleware.process_chat_payload(
            request, form_data, account, metadata, model
        )
        assert not paused
        return form_data['messages'], metadata.get('initial_output') or []

    state.run = run
    return state


def hidden_names(output):
    return [item['name'] for item in output if item.get('type') == 'function_call']


@pytest.mark.asyncio
async def test_first_turn_sends_each_manifest_once(chat):
    messages, output = await chat.run([user('u1', 'Hello')])
    assert hidden_names(output) == ['list_available_skills', 'list_attached_knowledge']
    assert get_tool_results(messages, 'list_available_skills') == [skills_manifest(SKILL)]
    assert get_tool_results(messages, 'list_attached_knowledge') == [knowledge_manifest()]


@pytest.mark.asyncio
async def test_later_turns_replay_the_saved_manifest_without_adding_another(chat):
    history = [
        user('u1', 'Hello'),
        assistant(
            'a1',
            'Hi there.',
            ('list_available_skills', skills_manifest(SKILL)),
            ('list_attached_knowledge', knowledge_manifest()),
        ),
        user('u2', 'What changed?'),
        assistant('a2', 'Nothing yet.'),
        user('u3', 'Thanks'),
    ]
    messages, output = await chat.run(history)
    assert hidden_names(output) == []
    assert get_tool_results(messages, 'list_available_skills') == [skills_manifest(SKILL)]
    assert get_tool_results(messages, 'list_attached_knowledge') == [knowledge_manifest()]
    # The replayed copy stays where it was, so the cached prompt prefix is unchanged.
    assert messages[-1] == {'role': 'user', 'content': 'Thanks'}


@pytest.mark.asyncio
async def test_the_next_request_starts_with_this_request(chat):
    # Saved hidden context replays exactly as it was sent, so the cached prompt prefix stays valid.
    first, output = await chat.run([user('u1', 'Hello')], features={'memory': True})
    reply = assistant('a1', 'Hi there.')
    reply['output'] = [*copy.deepcopy(output), *reply['output']]
    second, output = await chat.run([user('u1', 'Hello'), reply, user('u2', 'Again')], features={'memory': True})
    assert second[: len(first)] == first
    # Only this turn's memory recall is new.
    assert hidden_names(output) == ['recall_memories']
    assert len(get_tool_results(second, 'list_available_skills')) == 1


@pytest.mark.asyncio
async def test_a_changed_catalog_appends_one_new_copy(chat):
    history = [
        user('u1', 'Hello'),
        assistant(
            'a1',
            'Hi there.',
            ('list_available_skills', skills_manifest(SKILL)),
            ('list_attached_knowledge', knowledge_manifest()),
        ),
        user('u2', 'Any new skills?'),
    ]
    added = SimpleNamespace(id='core-log', name='Core log', description='Summarizes core logs.', is_active=True)
    chat.skills = [added, SKILL]
    messages, output = await chat.run(history)
    assert hidden_names(output) == ['list_available_skills']
    current = skills_manifest(added, SKILL)
    assert get_tool_results(messages, 'list_available_skills') == [skills_manifest(SKILL), current]
    assert get_last_tool_result(messages, 'list_available_skills') == current


@pytest.mark.asyncio
async def test_compaction_turn_keeps_exactly_one_manifest_and_does_not_summarize_it(chat):
    history = [
        user('u1', 'Hello'),
        assistant(
            'a1',
            'Hi there.',
            ('list_available_skills', skills_manifest(SKILL)),
            ('list_attached_knowledge', knowledge_manifest()),
        ),
        user('u2', 'Continue'),
        assistant('a2', 'Continuing.'),
        user('u3', 'Summarize so far'),
    ]
    # The summary replaces the first exchange, which held the only manifest copies.
    chat.compact = (2, 'The user greeted the assistant.')
    messages, output = await chat.run(history)
    assert hidden_names(output) == ['list_available_skills', 'list_attached_knowledge']
    assert get_tool_results(messages, 'list_available_skills') == [skills_manifest(SKILL)]
    assert get_tool_results(messages, 'list_attached_knowledge') == [knowledge_manifest()]
    assert any('[CONVERSATION SUMMARY]' in (m.get('content') or '') for m in messages if m['role'] == 'system')
    # This turn's copy is added after compaction, so the summarizer saw only the replayed one.
    assert get_tool_results(chat.summarized, 'list_available_skills') == [skills_manifest(SKILL)]


@pytest.mark.asyncio
async def test_explicit_skill_activation_loads_once_and_again_after_compaction(chat):
    first, output = await chat.run([user('u1', 'Use the well file skill')], skill_ids=['well-file'])
    assert hidden_names(output) == ['list_available_skills', 'list_attached_knowledge', 'view_skill']
    assert chat.view_skill_calls == ['well-file']

    saved = assistant('a1', 'Loaded.')
    saved['output'] = [*output, *saved['output']]
    history = [user('u1', 'Use the well file skill'), saved, user('u2', 'Next well')]
    _, output = await chat.run(history, skill_ids=['well-file'])
    assert hidden_names(output) == []
    assert chat.view_skill_calls == ['well-file']

    # Once compaction summarizes the earlier load away, the activated skill is loaded again for this turn.
    chat.compact = (2, 'The well file skill was used.')
    messages, output = await chat.run(history, skill_ids=['well-file'])
    assert hidden_names(output) == ['list_available_skills', 'list_attached_knowledge', 'view_skill']
    assert chat.view_skill_calls == ['well-file', 'well-file']
    assert len(get_tool_results(messages, 'view_skill')) == 1


@pytest.mark.asyncio
async def test_memory_recall_keeps_its_place_before_compaction(chat):
    messages, output = await chat.run([user('u1', 'Hello')], features={'memory': True})
    assert hidden_names(output) == ['recall_memories', 'list_available_skills', 'list_attached_knowledge']
    tool_names = [call['function']['name'] for m in messages for call in m.get('tool_calls') or []]
    assert tool_names == ['recall_memories', 'list_available_skills', 'list_attached_knowledge']
    # Unchanged for memory: this turn's recall is still part of what compaction sees.
    assert get_tool_results(chat.summarized, 'recall_memories') == ['<memories>Prefers SI units.</memories>']
    assert get_tool_results(chat.summarized, 'list_available_skills') == []
