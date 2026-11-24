"""Saved chats stay small enough to load, save and replay.

Only saved copies of tool results, citation excerpts and attached-file text are bounded; what people type and what
models write is never changed. Every database here is a disposable SQLite file under pytest's tmp_path.
"""

import copy
import json
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from open_webui import env
from open_webui.models import chat_messages, chats
from open_webui.models.chat_messages import ChatMessage, ChatMessages
from open_webui.models.chats import Chat, ChatModel, Chats
from open_webui.utils import chat_bounds
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
        'CHAT_TRIM_PREVIEW_MIN_CHARS': 400,
        'CHAT_TRIM_OLD_PREVIEW_CHARS': 300,
    }.items():
        monkeypatch.setattr(env, name, value)


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


def output_texts(message):
    return [
        part['text']
        for item in message.get('output', [])
        if item.get('type') == 'function_call_output'
        for part in item['output']
    ]


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


def test_citation_excerpts_and_copied_file_text_are_bounded_but_what_people_type_is_not(small_limits):
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
        ],
    }
    bounded = bound_message(message)
    assert bounded['content'] == message['content']
    assert 'content' not in bounded['files'][0]['file']['data']  # read from the file record instead
    assert bounded['files'][0]['file']['meta'] == {'name': 'a.pdf'}
    assert bounded['files'][1] == message['files'][1]  # pasted text has no other copy
    document, small = bounded['sources'][0]['document']
    assert len(document) <= 1_000 and 'Citation excerpt shortened' in document and small == 'e' * 200


def test_a_chat_over_budget_shortens_what_the_model_no_longer_reads_first(small_limits):
    messages = {
        'u1': {'id': 'u1', 'parentId': None, 'childrenIds': ['old', 'a1'], 'role': 'user', 'content': 'q1'},
        'old': tool_message('old', [3_500], parent='u1'),  # a regenerated reply on another branch
        'a1': tool_message('a1', [3_500], parent='u1'),
        'u2': {'id': 'u2', 'parentId': 'a1', 'childrenIds': ['a2'], 'role': 'user', 'content': 'q2'},
        'a2': tool_message('a2', [3_500], parent='u2'),
    }
    bounded, changed = bound_messages_map(messages, 'a2')
    assert changed == {'old'}
    assert 'Saved copy shortened' in output_texts(bounded['old'])[0]
    assert bounded['a1'] is messages['a1'] and bounded['a2'] is messages['a2']
    assert bound_messages_map(bounded, 'a2') == (bounded, set())  # already within budget


def test_the_newest_messages_of_the_current_branch_are_never_shortened_for_the_chat(small_limits):
    messages, parent = {}, None
    for index in range(4):
        user_id, reply_id = f'u{index}', f'a{index}'
        messages[user_id] = {'id': user_id, 'parentId': parent, 'childrenIds': [reply_id], 'role': 'user'}
        messages[reply_id] = tool_message(reply_id, [3_500], parent=user_id)
        parent = reply_id
    bounded, changed = bound_messages_map(messages, 'a3')
    assert 'a3' not in changed and 'u3' not in changed
    assert bounded['a3'] is messages['a3']
    assert changed and changed <= {'a0', 'a1', 'a2'}
    assert sum(len(text) for message in bounded.values() for text in output_texts(message)) <= 10_000


def test_a_long_chat_of_many_medium_results_shortens_its_oldest_first(small_limits):
    messages, parent = {}, None
    for index in range(20):
        user_id, reply_id = f'u{index}', f'a{index}'
        messages[user_id] = {'id': user_id, 'parentId': parent, 'childrenIds': [reply_id], 'role': 'user'}
        messages[reply_id] = tool_message(reply_id, [350, 350], parent=user_id)  # each below the 400 preview
        parent = reply_id
    bounded, changed = bound_messages_map(messages, 'a19')
    assert sum(len(text) for message in bounded.values() for text in output_texts(message)) <= 10_000
    assert output_texts(bounded['a0'])[0].startswith('[Saved copy removed: ')
    assert 'Saved copy shortened' in output_texts(bounded['a18'])[0]
    assert bounded['a19'] is messages['a19']  # the newest stays whole
    assert bound_messages_map(bounded, 'a19') == (bounded, set())


def test_a_chat_that_fits_after_short_previews_keeps_its_newer_results_whole(small_limits):
    messages, parent = {}, None
    for index in range(14):
        user_id, reply_id = f'u{index}', f'a{index}'
        messages[user_id] = {'id': user_id, 'parentId': parent, 'childrenIds': [reply_id], 'role': 'user'}
        messages[reply_id] = tool_message(reply_id, [390, 390], parent=user_id)
        parent = reply_id
    bounded, changed = bound_messages_map(messages, 'a13')
    assert sum(len(text) for message in bounded.values() for text in output_texts(message)) <= 10_000
    assert changed and max(int(key[1:]) for key in changed) < 8  # only the oldest needed shortening
    assert all(bounded[f'a{index}'] is messages[f'a{index}'] for index in range(8, 14))


def test_a_reply_still_being_written_and_the_message_being_saved_keep_their_results(small_limits):
    messages = {
        'u0': {'id': 'u0', 'parentId': None, 'childrenIds': ['live', 'saving', 'a0'], 'role': 'user', 'content': 'q'},
        'live': tool_message('live', [3_500], parent='u0', done=False),
        'saving': tool_message('saving', [3_500], parent='u0'),
        'a0': tool_message('a0', [3_500], parent='u0'),
        'a1': tool_message('a1', [3_500], parent='u0'),
    }
    bounded, changed = bound_messages_map(messages, 'a1', keep={'saving'})
    assert changed == {'a0'}
    assert bounded['live'] is messages['live'] and bounded['saving'] is messages['saving']


def test_compacted_history_is_shortened_before_replayed_history(small_limits):
    messages = {
        'u0': {'id': 'u0', 'parentId': None, 'childrenIds': ['a0'], 'role': 'user', 'content': 'q0'},
        'a0': tool_message('a0', [3_500], parent='u0'),
        'u1': {'id': 'u1', 'parentId': 'a0', 'childrenIds': ['a1'], 'role': 'user', 'content': 'q1'},
        'a1': tool_message('a1', [3_500], parent='u1', contextSummary='Earlier turns, summarized.'),
        'u2': {'id': 'u2', 'parentId': 'a1', 'childrenIds': ['a2'], 'role': 'user', 'content': 'q2'},
        'a2': tool_message('a2', [3_500], parent='u2'),
    }
    bounded, changed = bound_messages_map(messages, 'a2')
    assert changed == {'a0'}  # before the checkpoint: the model reads the summary instead


def test_the_chat_model_serves_and_saves_bounded_chats_and_the_switch_turns_it_off(small_limits, monkeypatch):
    history = {'messages': {'a': tool_message('a', [3_000, 5_000])}, 'currentId': 'a'}
    chat = {'title': 'T', 'history': history, 'messages': [history['messages']['a']]}
    model = ChatModel(id='c', user_id='owner', title='T', chat=chat, created_at=0, updated_at=0)
    texts = output_texts(model.chat['history']['messages']['a'])
    assert sum(len(text) for text in texts) <= 6_000
    assert model.chat['messages'][0] == model.chat['history']['messages']['a']
    assert bound_chat(model.chat) == model.chat
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
    yield factory
    await engine.dispose()


async def seed_chat(factory, messages, current_id):
    async with factory() as session:
        session.add(
            Chat(
                id='c',
                user_id='owner',
                title='T',
                chat={'title': 'T', 'history': {'messages': messages, 'currentId': current_id}},
                created_at=0,
                updated_at=0,
            )
        )
        for message_id, message in messages.items():
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


async def stored(factory):
    async with factory() as session:
        chat = await session.get(Chat, 'c')
        rows = {row.id[2:]: row for row in (await session.execute(ChatMessage.__table__.select())).all()}
        return chat.chat, rows


@pytest.mark.asyncio
async def test_saving_a_reply_heals_an_oversized_chat_in_both_stores(small_limits, chat_db, monkeypatch):
    # Seed an oversized chat as it was saved before the bounds existed.
    monkeypatch.setattr(env, 'CHAT_STORAGE_BOUNDS', False)
    messages, parent = {}, None
    for index in range(3):
        user_id, reply_id = f'u{index}', f'a{index}'
        messages[user_id] = {'id': user_id, 'parentId': parent, 'childrenIds': [reply_id], 'role': 'user'}
        messages[reply_id] = tool_message(reply_id, [3_900, 3_900], parent=user_id)
        parent = reply_id
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
    bulk = sum(len(text) for message in history.values() for text in output_texts(message))
    assert bulk <= 10_000
    for message_id in ('a0', 'a1', 'a2'):
        row_texts = [
            part['text']
            for item in rows[message_id].output
            if item['type'] == 'function_call_output'
            for part in item['output']
        ]
        assert row_texts == output_texts(history[message_id])  # the replay store matches the chat
    assert rows['a3'].output == history['a3']['output']

    replay = await ChatMessages.get_messages_map_by_chat_id('c')
    assert sum(len(json.dumps(message)) for message in replay.values()) < 20_000


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
    row_texts = [
        part['text'] for item in rows['a0'].output if item['type'] == 'function_call_output' for part in item['output']
    ]
    assert row_texts == output_texts(chat['history']['messages']['a0'])


def test_the_one_off_repair_plans_the_same_bounds_for_both_stores(small_limits):
    from open_webui.utils.chat_bounds_repair import plan, rows_to_messages

    messages = {
        'u0': {'id': 'u0', 'parentId': None, 'childrenIds': ['a0'], 'role': 'user', 'content': 'q'},
        'a0': tool_message('a0', [5_000, 5_000], parent='u0'),
    }
    chat = {'title': 'T', 'history': {'messages': messages, 'currentId': 'a0'}}
    rows = [
        ('c-u0', None, 'user', None, None, None, None, 1),
        ('c-a0', 'u0', 'assistant', json.dumps(messages['a0']['output']), None, None, None, 2),
    ]
    row_messages = rows_to_messages('c', rows)
    assert set(row_messages) == {'u0', 'a0'} and row_messages['a0']['parentId'] == 'u0'
    bounded_chat, row_updates = plan(chat, row_messages, 'a0')
    assert bounded_chat['history']['messages']['a0']['output'] == row_updates['a0']['output']
    assert set(row_updates) == {'a0'} and set(row_updates['a0']) == {'output'}
    assert plan(
        bounded_chat,
        rows_to_messages(
            'c', [('c-a0', 'u0', 'assistant', json.dumps(row_updates['a0']['output']), None, None, None, 2)]
        ),
        'a0',
    ) == (None, {})
