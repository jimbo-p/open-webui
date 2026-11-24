"""Saved chats stay small enough to load, save and replay.

Only saved copies of tool results, citation excerpts and attached-file text are bounded; what people type, what models
write, answers, resolutions and hidden context are never changed. Every database here is a disposable SQLite file
under pytest's tmp_path.
"""

import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from open_webui import env
from open_webui.models import chat_messages, chats
from open_webui.models.chat_messages import ChatMessage, ChatMessages
from open_webui.models.chats import Chat, ChatForm, ChatImportForm, ChatModel, Chats
from open_webui.utils import chat_bounds, middleware
from open_webui.utils.chat_bounds import bound_chat, bound_message, bound_messages_map, shorten
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def small_limits(monkeypatch):
    for name, value in {
        'CHAT_STORAGE_BOUNDS': True,
        'TOOL_RESULT_MAX_CHARS': 4_000,
        'CHAT_MESSAGE_TOOL_OUTPUT_MAX_CHARS': 6_000,
        'CHAT_SOURCE_DOCUMENT_MAX_CHARS': 1_000,
        'CHAT_MESSAGE_SOURCES_MAX_CHARS': 2_500,
        'CHAT_INLINE_FILE_CONTENT_MAX_CHARS': 500,
        'CHAT_STORED_BULK_MAX_CHARS': 10_000,
        'CHAT_BOUND_PROTECTED_MESSAGES': 2,
        'CHAT_TRIM_PREVIEW_MIN_CHARS': 1_000,
        'CHAT_TRIM_OLD_PREVIEW_CHARS': 300,
    }.items():
        monkeypatch.setattr(env, name, value)
    monkeypatch.setitem(chat_bounds._compaction, 'enabled', False)


def tool_message(message_id, sizes, parent=None, role='assistant', **extra):
    output = []
    for index, size in enumerate(sizes):
        call_id = f'{message_id}-call-{index}'
        output.append({'type': 'function_call', 'call_id': call_id, 'name': 'view_file', 'arguments': '{}'})
        output.append(
            {
                'type': 'function_call_output',
                'call_id': call_id,
                'output': [{'type': 'input_text', 'text': f'<{index}>' + 'r' * size + f'</{index}>'}],
                'status': 'completed',
            }
        )
    output.append({'type': 'message', 'content': [{'type': 'output_text', 'text': 'The answer.'}]})
    return {
        'id': message_id,
        'parentId': parent,
        'childrenIds': [],
        'role': role,
        'content': 'The answer.',
        'output': output,
        'done': True,
        **extra,
    }


def chain(count, sizes, *, prefix=''):
    """A linear chat of ``count`` turns, each reply with tool results of ``sizes``."""
    messages, parent = {}, None
    for index in range(count):
        user_id, reply_id = f'{prefix}u{index}', f'{prefix}a{index}'
        messages[user_id] = {'id': user_id, 'parentId': parent, 'childrenIds': [reply_id], 'role': 'user'}
        if parent:
            messages[parent]['childrenIds'] = [user_id]
        messages[reply_id] = tool_message(reply_id, sizes, parent=user_id)
        parent = reply_id
    return messages


def output_texts(message):
    return [
        part['text']
        for item in message.get('output', [])
        if item.get('type') == 'function_call_output'
        for part in item['output']
    ]


def bulk(messages):
    return sum(len(text) for message in messages.values() for text in output_texts(message))


def test_defaults_bound_a_message_and_a_chat_far_above_any_model_context():
    assert env.CHAT_STORAGE_BOUNDS is True
    assert env.CHAT_MESSAGE_TOOL_OUTPUT_MAX_CHARS == 8_000_000
    assert env.CHAT_STORED_BULK_MAX_CHARS == 16_000_000
    assert env.CHAT_SOURCE_DOCUMENT_MAX_CHARS == 100_000
    assert env.CHAT_INLINE_FILE_CONTENT_MAX_CHARS == 1_000_000


def test_shorten_keeps_start_and_end_says_what_was_cut_and_is_stable():
    text = 'A' * 3_000 + 'M' * 4_000 + 'Z' * 3_000
    short = shorten(text, 2_000)
    assert len(short) <= 2_000
    assert short.startswith('A' * 100) and short.endswith('Z' * 100)
    assert 'Saved copy shortened: ' in short and f'of {len(text):,} characters' in short
    assert shorten(short, 2_000) == short
    citation = shorten(text, 2_000, 'source')
    assert 'Citation excerpt shortened: ' in citation and 'Open the source' in citation


def test_shortening_again_keeps_the_original_length_and_the_removal_note_is_neutral():
    text = 'A' * 30_000 + 'Z' * 30_000
    twice = shorten(shorten(text, 20_000), 2_000)
    assert len(twice) <= 2_000 and f'of {len(text):,} characters' in twice
    assert twice.startswith('A' * 100) and twice.endswith('Z' * 100)
    removed = shorten(twice, 0)
    assert removed.startswith('[Saved copy removed: this tool result (60,000 characters)')
    assert 'old' not in removed and chat_bounds.has_bound_note(removed)
    assert not chat_bounds.has_bound_note('An ordinary result.')


def test_messages_within_their_limits_are_returned_as_they_are(small_limits):
    message = tool_message('a', [100, 200])
    assert bound_message(message) is message


def test_a_message_over_its_tool_budget_shortens_its_largest_results_only(small_limits):
    message = tool_message('a', [300, 3_000, 5_000])
    original = copy.deepcopy(message)
    bounded = bound_message(message)
    assert message == original  # the live object the tool loop keeps using is untouched
    small, middle, large = output_texts(bounded)
    assert small == output_texts(message)[0]
    assert sum(len(text) for text in output_texts(bounded)) <= 6_000
    assert len(middle) < 3_010 and len(large) <= 4_000
    assert 'Saved copy shortened' in large and large.startswith('<2>') and large.endswith('</2>')
    assert bounded['content'] == message['content']
    assert bounded['output'][-1] == message['output'][-1]  # the model's own words


def test_answers_resolutions_and_hidden_context_are_never_shortened(small_limits):
    answer = json.dumps({'status': 'answered', 'answers': {'q': 'typed ' * 1_000}})
    resolution = json.dumps({'action': 'approve', 'note': 'n' * 6_000})
    skill = 'Skill instructions. ' * 400
    message = tool_message('a', [5_000, 5_000])
    message['output'][:0] = [
        {'type': 'function_call', 'call_id': 'ask', 'name': 'ask_user', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'ask', 'output': [{'type': 'input_text', 'text': answer}]},
        {'type': 'function_call', 'call_id': 'agent', 'name': 'shell', 'agent_resolution': {'action': 'approve'}},
        {'type': 'function_call_output', 'call_id': 'agent', 'output': [{'type': 'input_text', 'text': resolution}]},
        *middleware.hidden_tool_result('view_skill', skill),
        *middleware.hidden_tool_result('list_available_skills', 'm' * 5_000),
        {
            'type': 'function_call_output',
            'call_id': 'x',
            'status': 'rejected',
            'output': [{'type': 'input_text', 'text': 'Error: ' + 'e' * 5_000}],
        },
    ]
    bounded = bound_message(message)
    assert output_texts(bounded)[:5] == output_texts(message)[:5]
    assert all('Saved copy shortened' in text for text in output_texts(bounded)[5:])
    # The chat budget leaves them alone too, even on the oldest message.
    messages = chain(4, [3_500])
    messages['a0'] = {**message, 'parentId': 'u0', 'id': 'a0'}
    bounded_map, changed = bound_messages_map(messages, 'a3')
    assert 'a0' in changed and output_texts(bounded_map['a0'])[:5] == output_texts(message)[:5]


@pytest.mark.asyncio
async def test_a_skill_whose_saved_copy_was_shortened_is_loaded_again():
    tool = {'callable': AsyncMock(return_value='Skill instructions.')}
    call = {'id': 'call-1', 'type': 'function', 'function': {'name': 'view_skill', 'arguments': '{"id": "s1"}'}}
    history = [{'role': 'assistant', 'content': '', 'tool_calls': [call]}]
    whole = history + [{'role': 'tool', 'tool_call_id': 'call-1', 'content': 'Skill instructions.'}]
    assert await middleware.preload_skills(whole, {'s1'}, tool) == []
    shortened = history + [{'role': 'tool', 'tool_call_id': 'call-1', 'content': shorten('S' * 50_000, 2_000)}]
    reloaded = await middleware.preload_skills(shortened, {'s1'}, tool)
    assert [item['name'] for item in reloaded if item['type'] == 'function_call'] == ['view_skill']
    tool['callable'].assert_awaited_once_with(id='s1')


def test_citation_excerpts_and_copied_file_text_are_bounded_but_what_people_type_is_not(small_limits):
    file_item = {'type': 'file', 'id': 'f1', 'file': {'data': {'content': 'x' * 1_500}, 'meta': {'name': 'a.pdf'}}}
    message = {
        'id': 'u',
        'role': 'user',
        'content': 'typed ' * 2_000,
        'files': [
            {'type': 'file', 'id': 'f1', 'file': {'data': {'content': 'x' * 900}, 'meta': {'name': 'a.pdf'}}},
            {'type': 'text', 'file': {'data': {'content': 'pasted ' * 200}}},
        ],
        'sources': [
            {'source': {'id': 'f1', 'name': 'a.pdf'}, 'document': ['d' * 3_000, 'e' * 200], 'metadata': [{}, {}]},
            {'source': file_item, 'document': ['short'], 'metadata': [{}]},
        ],
    }
    bounded = bound_message(message)
    assert bounded['content'] == message['content']
    assert 'content' not in bounded['files'][0]['file']['data']  # read from the file record instead
    assert bounded['files'][0]['file']['meta'] == {'name': 'a.pdf'}
    assert bounded['files'][1] == message['files'][1]  # pasted text has no other copy
    document, small = bounded['sources'][0]['document']
    assert len(document) <= 1_000 and 'Citation excerpt shortened' in document and small == 'e' * 200
    assert 'content' not in bounded['sources'][1]['source']['file']['data']  # the citation's copy of the file
    assert message['sources'][1]['source']['file']['data']['content'] == 'x' * 1_500
    chat = {'title': 'T', 'files': [copy.deepcopy(message['files'][0])], 'history': {'messages': {}}}
    assert 'content' not in bound_chat(chat)['files'][0]['file']['data']
    assert chat['files'][0]['file']['data']['content'] == 'x' * 900


def test_request_files_are_deduplicated_by_id_even_when_one_copy_lost_its_text():
    with_text = {'type': 'file', 'id': 'f1', 'context': 'full', 'file': {'data': {'content': 'x'}}}
    without = {'type': 'file', 'id': 'f1', 'context': 'full', 'file': {'data': {}}}
    assert middleware.request_file_key(with_text) == middleware.request_file_key(without)
    assert middleware.request_file_key({**without, 'context': None}) != middleware.request_file_key(without)
    url = {'type': 'url', 'url': 'https://example.invalid'}
    assert middleware.request_file_key(url) == ('json', json.dumps(url, sort_keys=True))


def test_a_chat_over_budget_shortens_what_the_model_no_longer_reads_first(small_limits):
    messages = {
        'u1': {'id': 'u1', 'parentId': None, 'childrenIds': ['old', 'a1'], 'role': 'user', 'content': 'q1'},
        'old': tool_message('old', [3_500], parent='u1'),  # a regenerated reply on another branch
        'a1': tool_message('a1', [3_500], parent='u1', childrenIds=['u2']),
        'u2': {'id': 'u2', 'parentId': 'a1', 'childrenIds': ['a2'], 'role': 'user', 'content': 'q2'},
        'a2': tool_message('a2', [3_500], parent='u2'),
    }
    bounded, changed = bound_messages_map(messages, 'a2')
    assert 'old' in changed and bounded['a2'] is messages['a2']
    assert len(output_texts(bounded['old'])[0]) <= 1_000 < len(output_texts(bounded['a1'])[0])
    assert bulk(bounded) <= 7_500  # brought down to the watermark, not just under the budget
    assert bound_messages_map(bounded, 'a2') == (bounded, set())  # already within budget


def test_the_newest_messages_of_the_current_branch_are_never_shortened_for_the_chat(small_limits):
    messages = chain(4, [3_500])
    bounded, changed = bound_messages_map(messages, 'a3')
    assert 'a3' not in changed and 'u3' not in changed
    assert bounded['a3'] is messages['a3']
    assert changed and changed <= {'a0', 'a1', 'a2'}
    assert bulk(bounded) <= 10_000


def test_a_stale_current_id_still_protects_the_newest_turns(small_limits):
    messages = chain(5, [3_500])
    bounded, changed = bound_messages_map(messages, 'a2')  # another tab saved before a3 and a4 existed
    assert 'a4' not in changed and bounded['a4'] is messages['a4']
    assert 'a3' in changed  # it is no longer among the newest two


def test_other_replies_to_the_newest_prompt_are_protected(small_limits):
    messages = chain(4, [3_500])
    messages['u3']['childrenIds'] = ['a3', 'b3']
    messages['b3'] = tool_message('b3', [1_000], parent='u3')  # a multi-model reply; currentId is the other one
    bounded, changed = bound_messages_map(messages, 'a3')
    assert 'b3' not in changed and bounded['b3'] is messages['b3']


def test_every_tier_gets_the_preview_pass_before_any_copy_is_cut_further(small_limits):
    messages = chain(4, [3_500])
    messages['u1']['childrenIds'].append('b1')
    messages['b1'] = tool_message('b1', [3_500], parent='u1')  # an older regeneration, off the current branch
    bounded, changed = bound_messages_map(messages, 'a3')
    assert bulk(bounded) <= 7_500
    assert not any(text.startswith('[Saved copy removed') for m in bounded.values() for text in output_texts(m))
    assert all(len(output_texts(bounded[key])[0]) >= 900 for key in ('a0', 'a1', 'a2'))  # the preview, not less
    assert len(output_texts(bounded['b1'])[0]) <= 300


def test_a_long_chat_of_many_medium_results_shortens_its_oldest_first(small_limits):
    messages = chain(20, [990, 990])  # each result below the 1,000-character preview
    bounded, changed = bound_messages_map(messages, 'a19')
    assert bulk(bounded) <= 7_500
    assert output_texts(bounded['a0'])[0].startswith('[Saved copy removed: this tool result (997 characters)')
    assert 'Saved copy shortened' in output_texts(bounded['a18'])[0]
    assert bounded['a19'] is messages['a19']  # the newest stays whole
    assert bound_messages_map(bounded, 'a19') == (bounded, set())


def test_a_chat_that_fits_after_short_previews_keeps_its_newer_results_whole(small_limits):
    messages = chain(6, [990, 990])
    bounded, changed = bound_messages_map(messages, 'a5')
    assert bulk(bounded) <= 7_500
    assert changed == {'a0', 'a1', 'a2', 'a3'}  # oldest first, and only as far as needed
    assert bounded['a4'] is messages['a4'] and bounded['a5'] is messages['a5']


def test_steady_growth_does_not_reshorten_the_chat_on_every_save(small_limits):
    messages = chain(4, [3_500])
    bounded, changed = bound_messages_map(messages, 'a3')
    assert changed == {'a0', 'a1', 'a2'} and bulk(bounded) <= 7_500
    grown = copy.deepcopy(bounded)
    grown['a3']['childrenIds'] = ['u4']
    grown['u4'] = {'id': 'u4', 'parentId': 'a3', 'childrenIds': ['a4'], 'role': 'user'}
    grown['a4'] = tool_message('a4', [100], parent='u4')
    assert bound_messages_map(grown, 'a4') == (grown, set())  # still under the budget: nothing is rewritten
    grown['a4'] = tool_message('a4', [3_500], parent='u4')
    regrown, changed = bound_messages_map(grown, 'a4')
    assert changed == {'a0', 'a3'}  # only the reply that left the protected tail, then the oldest
    assert regrown['a1'] is grown['a1'] and regrown['a2'] is grown['a2']
    assert 'of 3,507 characters' in output_texts(regrown['a0'])[0]


def test_a_reply_still_being_written_and_the_message_being_saved_keep_their_results(small_limits):
    messages = chain(2, [3_500])
    messages['u0']['childrenIds'] += ['live', 'saving', 'other']
    messages['live'] = tool_message('live', [3_500], parent='u0', done=False)
    messages['saving'] = tool_message('saving', [3_500], parent='u0')
    messages['other'] = tool_message('other', [3_500], parent='u0')
    bounded, changed = bound_messages_map(messages, 'a1', keep={'saving'})
    assert changed == {'other', 'a0'}
    assert bounded['live'] is messages['live'] and bounded['saving'] is messages['saving']


def test_compacted_history_is_shortened_first_only_while_compaction_is_on(small_limits, monkeypatch):
    messages = chain(3, [3_500])
    messages['a1']['contextSummary'] = 'Earlier turns, summarized.'
    bounded, _ = bound_messages_map(messages, 'a2')
    assert len(output_texts(bounded['a0'])[0]) == len(output_texts(bounded['a1'])[0])  # both still replayed
    monkeypatch.setitem(chat_bounds._compaction, 'enabled', True)
    bounded, _ = bound_messages_map(messages, 'a2')
    assert len(output_texts(bounded['a0'])[0]) <= 1_000 < len(output_texts(bounded['a1'])[0])
    assert bounded['a2'] is messages['a2']


def test_the_chat_model_serves_and_saves_bounded_chats_and_the_switch_turns_it_off(small_limits, monkeypatch):
    history = {'messages': {'a': tool_message('a', [3_000, 5_000])}, 'currentId': 'a'}
    chat = {'title': 'T', 'history': history, 'messages': [history['messages']['a']]}
    model = ChatModel(id='c', user_id='owner', title='T', chat=chat, created_at=0, updated_at=0)
    assert sum(len(text) for text in output_texts(model.chat['history']['messages']['a'])) <= 6_000
    assert model.chat['messages'][0] == model.chat['history']['messages']['a']
    assert bound_chat(model.chat) == model.chat
    fields = {'id': 'c', 'user_id': 'owner', 'title': 'T', 'chat': chat, 'created_at': 0, 'updated_at': 0}
    assert ChatModel.model_validate(fields, context={'bounded': True}).chat == chat  # save paths bound already
    monkeypatch.setattr(env, 'CHAT_STORAGE_BOUNDS', False)
    assert ChatModel(id='c', user_id='owner', title='T', chat=chat, created_at=0, updated_at=0).chat == chat


def test_a_bounding_error_keeps_the_message(small_limits, monkeypatch):
    monkeypatch.setattr(chat_bounds, '_slots', lambda message: 1 / 0)
    message = tool_message('a', [9_000])
    assert bound_message(message) is message


@pytest_asyncio.fixture
async def chat_db(monkeypatch, tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "chats.db"}')
    async with engine.begin() as connection:
        await connection.run_sync(Chat.__table__.create)
        await connection.run_sync(ChatMessage.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def database(db=None):
        if db is not None:
            yield db
        else:
            async with factory() as session:
                yield session

    monkeypatch.setattr(chats, 'get_async_db', database)
    monkeypatch.setattr(chats, 'get_async_db_context', database)
    monkeypatch.setattr(chat_messages, 'get_async_db_context', database)
    monkeypatch.setattr(Chats, 'require_chat_creation_permission', AsyncMock(return_value=None))
    yield factory
    await engine.dispose()


async def seed_chat(factory, messages, current_id, *, rows=None, chat=None):
    async with factory() as session:
        body = chat if chat is not None else {'title': 'T', 'history': {'messages': messages, 'currentId': current_id}}
        session.add(Chat(id='c', user_id='owner', title='T', chat=body, created_at=0, updated_at=0))
        for message_id, message in messages.items():
            if rows is not None and message_id not in rows:
                continue
            session.add(
                ChatMessage(
                    id=f'c-{message_id}',
                    chat_id='c',
                    user_id='owner',
                    role=message['role'],
                    parent_id=message.get('parentId'),
                    content=message.get('content'),
                    output=message.get('output'),
                    created_at=0,
                    updated_at=0,
                )
            )
        await session.commit()


async def stored(factory, chat_id='c'):
    async with factory() as session:
        chat = await session.get(Chat, chat_id)
        rows = {
            row.id[len(chat_id) + 1 :]: row
            for row in (await session.execute(ChatMessage.__table__.select())).all()
            if row.chat_id == chat_id
        }
        return chat.chat, rows


def row_texts(row):
    return [
        part['text'] for item in row.output or [] if item['type'] == 'function_call_output' for part in item['output']
    ]


@pytest.mark.asyncio
async def test_saving_a_reply_heals_an_oversized_chat_in_both_stores(small_limits, chat_db, monkeypatch):
    # Seed an oversized chat as it was saved before the bounds existed.
    monkeypatch.setattr(env, 'CHAT_STORAGE_BOUNDS', False)
    messages = chain(3, [3_900, 3_900])
    messages['a2']['childrenIds'] = ['u3']
    messages['u3'] = {'id': 'u3', 'parentId': 'a2', 'childrenIds': ['a3'], 'role': 'user', 'content': 'q'}
    await seed_chat(chat_db, messages, 'u3')
    monkeypatch.setattr(env, 'CHAT_STORAGE_BOUNDS', True)

    reply = tool_message('a3', [3_000], parent='u3')
    live = copy.deepcopy(reply)
    saved = await Chats.upsert_message_to_chat_by_id_and_message_id('c', 'a3', reply)
    assert reply == live and saved is not None

    chat, rows = await stored(chat_db)
    history = chat['history']['messages']
    assert output_texts(history['a3']) == output_texts(reply)
    assert bulk(history) <= 10_000
    for message_id in ('a0', 'a1', 'a2'):
        assert row_texts(rows[message_id]) == output_texts(history[message_id])  # the replay store matches
    assert rows['a3'].output == history['a3']['output']

    replay = await ChatMessages.get_messages_map_by_chat_id('c')
    assert sum(len(json.dumps(message)) for message in replay.values()) < 20_000


@pytest.mark.asyncio
async def test_a_message_without_a_row_gets_no_partial_row(small_limits, chat_db, monkeypatch):
    monkeypatch.setattr(env, 'CHAT_STORAGE_BOUNDS', False)
    messages = chain(4, [3_500])
    await seed_chat(chat_db, messages, 'a3', rows={'u0', 'u1', 'a1', 'u2', 'a2', 'u3', 'a3'})  # a0 has none
    monkeypatch.setattr(env, 'CHAT_STORAGE_BOUNDS', True)
    assert await Chats.update_chat_by_id('c', {'models': ['m']}) is not None
    chat, rows = await stored(chat_db)
    assert 'a0' not in rows  # left for the backfill, which writes the whole bounded message
    assert 'Saved copy shortened' in output_texts(chat['history']['messages']['a0'])[0]
    assert row_texts(rows['a1']) == output_texts(chat['history']['messages']['a1'])
    replay = await Chats.get_messages_map_by_chat_id('c')
    assert replay['a0']['role'] == 'assistant' and replay['a0']['parentId'] == 'u0'


@pytest.mark.asyncio
@pytest.mark.parametrize('bounds', [True, False])
async def test_a_chat_without_history_can_still_be_renamed(small_limits, chat_db, monkeypatch, bounds):
    monkeypatch.setattr(env, 'CHAT_STORAGE_BOUNDS', bounds)
    await seed_chat(chat_db, {}, None, chat={'title': 't'})
    updated = await Chats.update_chat_by_id('c', {'title': 'Renamed'})
    assert updated is not None and updated.title == 'Renamed'


@pytest.mark.asyncio
async def test_the_frontend_saving_a_whole_chat_back_stays_bounded(small_limits, chat_db):
    messages = {
        'u0': {'id': 'u0', 'parentId': None, 'childrenIds': ['a0'], 'role': 'user', 'content': 'q'},
        'a0': tool_message('a0', [100], parent='u0'),
    }
    await seed_chat(chat_db, messages, 'a0')
    oversized = copy.deepcopy(messages)
    oversized['a0'] = tool_message('a0', [5_000, 5_000], parent='u0')
    updated = await Chats.update_chat_by_id(
        'c', {'history': {'messages': oversized, 'currentId': 'a0'}, 'messages': list(oversized.values())}
    )
    assert updated is not None
    chat, rows = await stored(chat_db)
    assert sum(len(text) for text in output_texts(chat['history']['messages']['a0'])) <= 6_000
    assert chat['messages'][1] == chat['history']['messages']['a0']
    assert row_texts(rows['a0']) == output_texts(chat['history']['messages']['a0'])


@pytest.mark.asyncio
async def test_forked_and_imported_chats_write_rows_from_the_bounded_chat(small_limits, chat_db):
    messages = chain(6, [3_500])
    body = {'title': 'Fork', 'history': {'messages': messages, 'currentId': 'a5'}}
    forked = await Chats.insert_new_chat('f', 'owner', ChatForm(chat=copy.deepcopy(body)))
    chat, rows = await stored(chat_db, 'f')
    assert forked is not None and bulk(chat['history']['messages']) <= 7_500
    for message_id in chat['history']['messages']:
        assert row_texts(rows[message_id]) == output_texts(chat['history']['messages'][message_id])
    [imported] = await Chats.import_chats('owner', [ChatImportForm(chat=copy.deepcopy(body))])
    chat, rows = await stored(chat_db, imported.id)
    assert bulk(chat['history']['messages']) <= 7_500
    for message_id in chat['history']['messages']:
        assert row_texts(rows[message_id]) == output_texts(chat['history']['messages'][message_id])


def test_the_one_off_repair_plans_the_same_bounds_for_both_stores(small_limits):
    from open_webui.utils.chat_bounds_repair import plan, rows_to_messages

    messages = {
        'u0': {'id': 'u0', 'parentId': None, 'childrenIds': ['a0'], 'role': 'user', 'content': 'q'},
        'a0': tool_message('a0', [5_000, 5_000], parent='u0'),
    }
    chat = {'title': 'T', 'history': {'messages': messages, 'currentId': 'a0'}}
    rows = [
        ('c-u0', None, 'user', None, None, None, None, 1, True),
        ('c-a0', 'u0', 'assistant', json.dumps(messages['a0']['output']).encode(), None, None, None, 2, True),
    ]
    row_messages = rows_to_messages('c', rows)
    assert set(row_messages) == {'u0', 'a0'} and row_messages['a0']['parentId'] == 'u0'
    bounded_chat, row_updates = plan(chat, row_messages, 'a0')
    assert bounded_chat['history']['messages']['a0']['output'] == row_updates['a0']['output']
    assert set(row_updates) == {'a0'} and set(row_updates['a0']) == {'output'}
    again = [('c-a0', 'u0', 'assistant', json.dumps(row_updates['a0']['output']), None, None, None, 2, True)]
    assert plan(bounded_chat, rows_to_messages('c', again), 'a0') == (None, {})
