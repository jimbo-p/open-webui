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
from open_webui.models.files import File
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
        'CHAT_BOUND_PROTECTED_TURNS': 1,
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
    """A linear chat of ``count`` turns, each reply with tool results of ``sizes``, timestamps in order."""
    messages, parent = {}, None
    for index in range(count):
        user_id, reply_id = f'{prefix}u{index}', f'{prefix}a{index}'
        messages[user_id] = {
            'id': user_id,
            'parentId': parent,
            'childrenIds': [reply_id],
            'role': 'user',
            'timestamp': 2 * index + 1,
        }
        if parent:
            messages[parent]['childrenIds'] = [user_id]
        messages[reply_id] = tool_message(reply_id, sizes, parent=user_id, timestamp=2 * index + 2)
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
    assert env.CHAT_BOUND_PROTECTED_TURNS == 2


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
    assert 'Run the tool again' not in twice + removed  # no instruction to repeat a tool with side effects
    assert not chat_bounds.has_bound_note('An ordinary result.')
    assert not chat_bounds.has_bound_note('A skill that quotes the marker [Saved copy shortened: here].')


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
    assert bound_message(message)['files'][0] == message['files'][0]  # nobody checked the file record holds it
    assert bound_message(message, droppable_files={'f1': 899})['files'][0] == message['files'][0]  # not the same text
    bounded = bound_message(message, droppable_files={'f1': 900})
    assert bounded['content'] == message['content']
    assert 'content' not in bounded['files'][0]['file']['data']  # read from the file record instead
    assert bounded['files'][0]['file']['data'][chat_bounds.OMITTED_MARK] is True
    assert bounded['files'][0]['file']['meta'] == {'name': 'a.pdf'}
    assert bounded['files'][1] == message['files'][1]  # pasted text has no other copy
    document, small = bounded['sources'][0]['document']
    assert len(document) <= 1_000 and 'Citation excerpt shortened' in document and small == 'e' * 200
    assert 'content' not in bounded['sources'][1]['source']['file']['data']  # the citation's copy of the file
    assert message['sources'][1]['source']['file']['data']['content'] == 'x' * 1_500
    chat = {'title': 'T', 'files': [copy.deepcopy(message['files'][0])], 'history': {'messages': {}}}
    assert bound_chat(chat) is chat
    assert 'content' not in bound_chat(chat, droppable_files={'f1': 900})['files'][0]['file']['data']
    assert chat['files'][0]['file']['data']['content'] == 'x' * 900


def test_a_web_page_copied_into_every_citation_is_dropped_there_but_kept_as_the_attachment(small_limits):
    page = {
        'type': 'text',
        'name': 'https://example.invalid',
        'context': 'full',
        'file': {'data': {'content': 'p' * 1_500}},
    }
    message = tool_message('a', [100])
    message['sources'] = [{'source': copy.deepcopy(page), 'document': ['excerpt'], 'metadata': [{}]}]
    bounded = bound_message(message)
    assert 'content' not in bounded['sources'][0]['source']['file']['data']
    assert bounded['sources'][0]['document'] == ['excerpt']
    user = {'id': 'u', 'role': 'user', 'content': 'q', 'files': [page]}
    assert bound_message(user) is user  # the attachment itself has no other copy


def test_request_files_are_deduplicated_by_id_even_when_one_copy_lost_its_text():
    with_text = {'type': 'file', 'id': 'f1', 'context': 'full', 'file': {'data': {'content': 'x'}}}
    without = {'type': 'file', 'id': 'f1', 'context': 'full', 'file': {'data': {}}}
    assert middleware.request_file_key(with_text) == middleware.request_file_key(without)
    assert middleware.request_file_key({**without, 'context': None}) != middleware.request_file_key(without)
    url = {'type': 'url', 'url': 'https://example.invalid'}
    assert middleware.request_file_key(url) == ('json', json.dumps(url, sort_keys=True))


def test_a_chat_over_budget_shortens_what_the_model_no_longer_reads_first(small_limits):
    messages = {
        'u1': {'id': 'u1', 'parentId': None, 'childrenIds': ['old', 'a1'], 'role': 'user', 'timestamp': 1},
        'old': tool_message('old', [3_500], parent='u1', timestamp=2),  # the reply before a regeneration
        'a1': tool_message('a1', [3_500], parent='u1', childrenIds=['u2'], timestamp=3),
        'u2': {'id': 'u2', 'parentId': 'a1', 'childrenIds': ['a2'], 'role': 'user', 'timestamp': 4},
        'a2': tool_message('a2', [3_500], parent='u2', timestamp=5),
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


def test_off_branch_copies_are_previews_before_replayed_results_are_touched_and_notes_only_last(small_limits):
    messages = chain(4, [3_500])
    messages['u1']['childrenIds'].insert(0, 'b1')
    messages['b1'] = tool_message('b1', [3_500], parent='u1', timestamp=3.5)  # an older regeneration
    bounded, changed = bound_messages_map(messages, 'a3')
    assert bulk(bounded) <= 10_000
    old_attempt = output_texts(bounded['b1'])[0]
    assert 'Saved copy shortened' in old_attempt and not old_attempt.startswith('[Saved copy removed')
    assert all(len(output_texts(bounded[key])[0]) >= 900 for key in ('a0', 'a1', 'a2'))  # replayed: previews
    assert bounded['a3'] is messages['a3']


def test_replayed_results_never_go_below_the_short_preview(small_limits):
    messages = chain(20, [990, 990])  # each result below the 1,000-character preview
    bounded, changed = bound_messages_map(messages, 'a19')
    texts = [text for key in messages if key != 'a19' for text in output_texts(bounded[key])]
    assert texts and all('Saved copy shortened' in text and len(text) <= 300 for text in texts)
    assert not any(text.startswith('[Saved copy removed') for text in texts)  # no note for what the model reads
    assert bulk(bounded) > 10_000  # so this chat stays over budget, by design
    assert bounded['a19'] is messages['a19']  # the newest stays whole
    assert bound_messages_map(bounded, 'a19') == (bounded, set())


def test_a_chat_that_fits_after_short_previews_keeps_its_newer_results_whole(small_limits):
    messages = chain(6, [990, 990])
    bounded, changed = bound_messages_map(messages, 'a5')
    assert bulk(bounded) <= 10_000
    assert changed == {'a0', 'a1'}  # oldest first, and only as far as needed
    assert all(bounded[f'a{index}'] is messages[f'a{index}'] for index in range(2, 6))


def grow(messages, turn, sizes, **extra):
    """``messages`` with one more turn (prompt and reply) on the single branch."""
    messages, parent = dict(messages), (f'a{turn - 1}' if turn else None)
    if parent:
        messages[parent] = {**messages[parent], 'childrenIds': [f'u{turn}']}
    messages[f'u{turn}'] = {
        'id': f'u{turn}',
        'parentId': parent,
        'childrenIds': [f'a{turn}'],
        'role': 'user',
        'timestamp': 2 * turn + 1,
    }
    messages[f'a{turn}'] = tool_message(f'a{turn}', sizes, parent=f'u{turn}', timestamp=2 * turn + 2, **extra)
    return messages


def grow_and_check(turns, sizes, budget):
    """Save a growing chat once per turn (as the reply's final save does) and check every save: within the budget,
    a newer reply's results never shorter than an older one's, and a result cut again only by a real step down.
    Returns how many older replies each save rewrote."""
    messages, rewrites = {}, []
    for turn in range(turns):
        messages = grow(messages, turn, sizes)
        before = {key: output_texts(message) for key, message in messages.items()}
        messages, changed = bound_messages_map(messages, f'a{turn}', keep={f'a{turn}'})
        assert bulk(messages) <= budget
        lengths = [len(output_texts(messages[f'a{index}'])[0]) for index in range(turn + 1)]
        assert lengths == sorted(lengths), lengths
        for key in changed - {f'a{turn}'}:
            for old, new in zip(before[key], output_texts(messages[key])):
                if chat_bounds.has_bound_note(old) and new != old:
                    assert len(new) <= len(old) * 0.8
        rewrites.append(len(changed - {f'a{turn}'}))
    return messages, rewrites


def test_steady_growth_cuts_the_results_leaving_protection_and_re_cuts_older_ones_only_by_steps(small_limits):
    messages = chain(4, [3_500])
    bounded, changed = bound_messages_map(messages, 'a3')
    assert changed and bulk(bounded) <= 10_000
    grown = grow(bounded, 4, [100])
    assert bound_messages_map(grown, 'a4') == (grown, set())  # still under the budget: nothing is rewritten
    _, rewrites = grow_and_check(10, [1_800], 10_000)
    assert sum(rewrites) <= 2 * 10  # older results are cut again only when the cap steps down


def test_long_growth_at_default_limits_never_leaves_a_newer_result_shorter_than_an_older_one(monkeypatch):
    monkeypatch.setitem(chat_bounds._compaction, 'enabled', False)
    messages, rewrites = grow_and_check(20, [1_000_000] * 3, env.CHAT_STORED_BULK_MAX_CHARS)
    lengths = [len(output_texts(messages[f'a{turn}'])[0]) for turn in range(20)]
    assert lengths[-2:] == [1_000_000 + 7] * 2  # the newest two turns stay whole
    assert min(lengths) >= 100_000  # an even cut, not a floor for the turns that just left protection
    assert max(rewrites) <= 20


def test_a_reply_still_being_written_and_the_message_being_saved_keep_their_results(small_limits):
    messages = chain(2, [2_400])
    messages['u0']['childrenIds'] += ['other', 'live', 'saving']
    messages['other'] = tool_message('other', [2_400], parent='u0', timestamp=1.5)  # an older regeneration
    messages['live'] = tool_message('live', [2_400], parent='u0', done=False, timestamp=5)
    messages['saving'] = tool_message('saving', [2_400], parent='u0', timestamp=6)
    bounded, changed = bound_messages_map(messages, 'a1', keep={'saving'})
    assert changed == {'other'}  # the unread copy goes first, and that is enough
    for key in ('live', 'saving', 'a1'):
        assert bounded[key] is messages[key]


def test_compacted_history_is_shortened_first_only_while_compaction_is_on(small_limits, monkeypatch):
    messages = chain(5, [3_500])
    messages['a2']['contextSummary'] = 'Earlier turns, summarized.'
    bounded, _ = bound_messages_map(messages, 'a4')
    assert len(output_texts(bounded['a1'])[0]) == len(output_texts(bounded['a2'])[0])  # all still replayed
    monkeypatch.setitem(chat_bounds._compaction, 'enabled', True)
    bounded, _ = bound_messages_map(messages, 'a4')
    before, after = [len(output_texts(bounded[key])[0]) for key in ('a0', 'a1')], len(output_texts(bounded['a2'])[0])
    assert max(before) <= 300 < after  # before the checkpoint goes first...
    texts = [text for key in ('a0', 'a1', 'a2') for text in output_texts(bounded[key])]
    assert not any(text.startswith('[Saved copy removed') for text in texts)  # ...but never to a note


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
        await connection.run_sync(File.__table__.create)
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
    monkeypatch.setitem(chats._compaction_checked, 'at', float('inf'))  # no config table in these databases
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
    assert forked is not None and bulk(chat['history']['messages']) <= 10_000
    for message_id in chat['history']['messages']:
        assert row_texts(rows[message_id]) == output_texts(chat['history']['messages'][message_id])
    [imported] = await Chats.import_chats('owner', [ChatImportForm(chat=copy.deepcopy(body))])
    chat, rows = await stored(chat_db, imported.id)
    assert bulk(chat['history']['messages']) <= 10_000
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


def test_citation_excerpts_go_before_any_tool_result_the_model_replays(small_limits):
    messages = chain(4, [800])
    for index in range(4):
        reply = messages[f'a{index}']
        reply['sources'] = [
            {'source': {'id': f's{index}'}, 'document': ['d' * 1_000, 'e' * 1_000], 'metadata': [{}, {}]}
        ]
    bounded, changed = bound_messages_map(messages, 'a3')
    assert all(output_texts(bounded[key]) == output_texts(messages[key]) for key in messages)  # results whole
    assert 'Citation excerpt shortened' in bounded['a0']['sources'][0]['document'][0]
    assert bounded['a3'] is messages['a3']


def test_a_tab_left_on_another_branch_cannot_expose_the_newest_turns(small_limits):
    messages = chain(4, [3_500])
    messages['u1']['childrenIds'].insert(0, 'b1')
    messages['b1'] = tool_message('b1', [3_500], parent='u1', childrenIds=['ub2'], timestamp=3.1)
    messages['ub2'] = {'id': 'ub2', 'parentId': 'b1', 'childrenIds': ['bb2'], 'role': 'user', 'timestamp': 3.2}
    messages['bb2'] = tool_message('bb2', [3_500], parent='ub2', timestamp=3.3)
    bounded, changed = bound_messages_map(messages, 'bb2')  # the stale tab's current id
    assert 'a3' not in changed and bounded['a3'] is messages['a3']
    assert not output_texts(bounded['bb2'])[0].startswith('[Saved copy removed')  # it is replayed there: a preview


def test_the_reply_to_the_prompt_before_it_was_edited_is_protected(small_limits):
    messages = chain(4, [3_500])
    messages['a2']['childrenIds'].append('u3e')
    messages['u3e'] = {'id': 'u3e', 'parentId': 'a2', 'childrenIds': ['a3e'], 'role': 'user', 'timestamp': 9}
    messages['a3e'] = tool_message('a3e', [3_500], parent='u3e', timestamp=10)
    bounded, changed = bound_messages_map(messages, 'a3e')
    assert 'a3' not in changed and bounded['a3'] is messages['a3']
    assert changed and changed <= {'a0', 'a1', 'a2'}


def test_large_protected_turns_leave_older_results_as_previews_not_notes(small_limits):
    messages = chain(4, [5_000])
    messages['a3'] = tool_message('a3', [2_900, 2_900], parent='u3', timestamp=8)
    messages['u3']['childrenIds'] = ['b3', 'a3']
    messages['b3'] = tool_message('b3', [2_900], parent='u3', timestamp=7.5)  # the previous regeneration
    bounded, changed = bound_messages_map(messages, 'a3')
    assert bulk(bounded) <= 10_000
    for key in ('a0', 'a1', 'a2'):
        text = output_texts(bounded[key])[0]
        assert 'Saved copy shortened' in text and not text.startswith('[Saved copy removed')


def test_a_message_with_more_results_than_its_budget_allows_previews_for_keeps_its_newest_whole(small_limits):
    message = tool_message('a', [990] * 10)
    bounded = bound_message(message)
    texts = output_texts(bounded)
    assert sum(len(text) for text in texts) <= 6_000
    assert texts[-4:] == output_texts(message)[-4:]
    assert 'Saved copy shortened' in texts[0]


def test_a_full_copy_saved_back_by_the_frontend_keeps_the_shortened_copy(small_limits):
    messages = chain(4, [3_500])
    bounded, _ = bound_messages_map(messages, 'a3')
    stored, full = bounded['a0'], messages['a0']
    kept = chat_bounds.keep_saved_shortening(stored, {**full, 'meta': {'rating': 1}})
    assert output_texts(kept) == output_texts(stored) and kept['meta'] == {'rating': 1}
    edited = copy.deepcopy(full)
    edited['output'][1]['output'][0]['text'] = 'a different result'
    assert chat_bounds.keep_saved_shortening(stored, edited) is edited  # a real change is taken as it is


def test_an_unhashable_id_in_the_messages_list_does_not_break_the_chat(small_limits):
    chat = {'title': 'T', 'messages': [{'id': ['x'], 'role': 'user', 'content': 'q'}], 'history': {'messages': {}}}
    assert bound_chat(chat) is chat
    assert ChatModel(id='c', user_id='o', title='T', chat=chat, created_at=0, updated_at=0).chat == chat


@pytest.mark.asyncio
async def test_saving_a_full_copy_back_does_not_undo_the_bounds(small_limits, chat_db):
    messages = chain(4, [3_500])
    bounded, _ = bound_messages_map(messages, 'a3')
    await seed_chat(chat_db, bounded, 'a3')
    full = copy.deepcopy(messages)  # what a tab that streamed the turns still holds
    assert await Chats.update_chat_by_id('c', {'history': {'messages': full, 'currentId': 'a3'}}) is not None
    chat, rows = await stored(chat_db)
    for key in ('a0', 'a1', 'a2'):
        assert output_texts(chat['history']['messages'][key]) == output_texts(bounded[key])


@pytest.mark.asyncio
async def test_attachment_text_is_dropped_only_when_its_file_record_holds_it(small_limits, chat_db):
    def attachment(file_id):
        return {'type': 'file', 'id': file_id, 'file': {'data': {'content': 'x' * 900}}}

    messages = {
        'u0': {'id': 'u0', 'parentId': None, 'childrenIds': [], 'role': 'user', 'content': 'q',
               'files': [attachment('kept-here'), attachment('elsewhere')]},
    }  # fmt: skip
    async with chat_db() as session:
        session.add(File(id='kept-here', user_id='owner', filename='a.pdf', data={'content': 'x' * 900}, meta={},
                         created_at=0, updated_at=0))  # fmt: skip
        await session.commit()
    await seed_chat(chat_db, messages, 'u0')
    assert await Chats.update_chat_by_id('c', {'title': 'Renamed'}) is not None
    chat, _ = await stored(chat_db)
    kept, elsewhere = chat['history']['messages']['u0']['files']
    assert 'content' not in kept['file']['data'] and kept['file']['data'][chat_bounds.OMITTED_MARK] is True
    assert elsewhere['file']['data']['content'] == 'x' * 900  # an imported chat's only copy


def test_the_repair_gives_rows_the_chat_decisions_even_with_a_stale_current_id_or_a_missing_row(small_limits):
    from open_webui.utils.chat_bounds_repair import ChatPlan, rows_to_messages

    messages = chain(5, [3_500])
    chat = {'title': 'T', 'history': {'messages': messages, 'currentId': 'a2'}}  # a stale tab's current id
    rows = [
        (f'c-{key}', m.get('parentId'), m['role'], json.dumps(m['output']) if 'output' in m else None,
         None, None, None, m['timestamp'], True)
        for key, m in messages.items()
        if key != 'u2'  # a message whose row was never written
    ]  # fmt: skip
    plan = ChatPlan(chat, 'a2')
    bounded = plan.bounded_chat['history']['messages']
    assert bounded['a4'] is messages['a4']  # the newest turn, whatever the current id says
    updates = plan.row_updates(rows_to_messages('c', rows))
    assert 'a4' not in updates and 'a3' in updates
    for key, columns in updates.items():
        assert columns['output'] == bounded[key]['output']  # the same decisions as the chat JSON


def test_regenerating_two_turns_back_keeps_the_turns_the_user_was_just_in(small_limits):
    messages = chain(6, [1_400])
    messages['u3']['childrenIds'] = ['a3', 'a3r']
    messages['a3r'] = tool_message('a3r', [2_000], parent='u3', timestamp=20)  # the regeneration, newest of all
    bounded, changed = bound_messages_map(messages, 'a3r')
    for key in ('a3', 'a4', 'a5', 'a3r'):  # everything under the fork the user is working at
        assert bounded[key] is messages[key]
    assert changed and changed <= {'a0', 'a1', 'a2'}


def test_editing_an_earlier_prompt_keeps_every_reply_under_it(small_limits):
    messages = chain(4, [1_800])
    messages['u2']['childrenIds'] = ['a2', 'm2']
    messages['m2'] = tool_message('m2', [1_800], parent='u2', timestamp=5.5)  # another model's reply
    messages['a1']['childrenIds'].append('u2e')
    messages['u2e'] = {'id': 'u2e', 'parentId': 'a1', 'childrenIds': ['a2e'], 'role': 'user', 'timestamp': 20}
    messages['a2e'] = tool_message('a2e', [1_800], parent='u2e', timestamp=21)
    bounded, changed = bound_messages_map(messages, 'a2e')
    for key in ('a2', 'm2', 'a3', 'a2e'):
        assert bounded[key] is messages[key]


def test_protected_turns_over_the_budget_leave_replayed_results_as_previews(small_limits):
    messages = chain(6, [1_500])
    messages['a5'] = tool_message('a5', [4_000, 4_000], parent='u5', timestamp=12)  # at the per-message limit...
    messages['u5']['childrenIds'] = ['a5r', 'a5']
    messages['a5r'] = tool_message('a5r', [4_000, 2_000], parent='u5', timestamp=11, done=False)  # ...and streaming
    bounded, changed = bound_messages_map(messages, 'a5')
    texts = [text for key in ('a0', 'a1', 'a2', 'a3', 'a4') for text in output_texts(bounded[key])]
    assert not any(text.startswith('[Saved copy removed') for text in texts)
    assert all(len(text) >= 250 for text in texts)


def test_many_regenerations_lose_protection_oldest_first_not_the_history(small_limits):
    messages = chain(4, [1_000])
    messages['u3']['childrenIds'] = []
    for index in range(6):
        key = f'r{index}'
        messages[key] = tool_message(key, [3_900], parent='u3', timestamp=10 + index)
        messages['u3']['childrenIds'].append(key)
    del messages['a3']
    bounded, changed = bound_messages_map(messages, 'r5')
    assert bounded['r5'] is messages['r5']  # recent work takes at most the watermark share
    assert 'r0' in changed  # the oldest attempts go first
    for key in ('a0', 'a1', 'a2'):  # the conversation the model replays keeps its results
        assert not output_texts(bounded[key])[0].startswith('[Saved copy removed')


def test_small_copies_of_a_page_in_every_citation_are_not_kept(small_limits):
    page = {'type': 'text', 'name': 'https://example.invalid', 'file': {'data': {'content': 'p' * 50}}}
    search = {'type': 'web_search', 'docs': [{'content': 'q' * 50, 'metadata': {'source': 'x'}}]}
    message = tool_message('a', [10])
    message['sources'] = [
        {'source': copy.deepcopy(page), 'document': ['excerpt'], 'metadata': [{}]},
        {'source': copy.deepcopy(search), 'document': ['hit'], 'metadata': [{}]},
    ]
    bounded = bound_message(message)
    assert 'content' not in bounded['sources'][0]['source']['file']['data']
    assert bounded['sources'][1]['source']['docs'] == [{'metadata': {'source': 'x'}}]
    assert [source['document'] for source in bounded['sources']] == [['excerpt'], ['hit']]


def test_a_preview_a_tab_was_served_earlier_keeps_the_shorter_stored_copy(small_limits):
    text = 'A' * 5_000 + 'Z' * 5_000
    served, now = shorten(text, 3_000), shorten(text, 1_000)
    stored = tool_message('a', [1])
    stored['output'][1]['output'][0]['text'] = now
    incoming = copy.deepcopy(stored)
    incoming['output'][1]['output'][0]['text'] = served
    assert output_texts(chat_bounds.keep_saved_shortening(stored, incoming)) == [now]


@pytest.mark.asyncio
async def test_the_messages_list_never_holds_more_than_the_history(small_limits, chat_db):
    full = chain(4, [3_500])
    bounded, _ = bound_messages_map(full, 'a3')
    await seed_chat(chat_db, bounded, 'a3')
    # A tab that streamed the turns posts its full copies, history and list alike (saveChatHandler).
    posted = copy.deepcopy(full)
    body = {'history': {'messages': posted, 'currentId': 'a3'}, 'messages': list(copy.deepcopy(full).values())}
    assert await Chats.update_chat_by_id('c', body) is not None
    chat, _ = await stored(chat_db)
    history = chat['history']['messages']
    assert bulk(history) == bulk(bounded)
    assert all(message == history[message['id']] for message in chat['messages'])


@pytest.mark.asyncio
async def test_attachment_text_stays_unless_the_owner_holds_the_same_file(small_limits, chat_db):
    def attachment(file_id):
        return {'type': 'file', 'id': file_id, 'file': {'data': {'content': 'x' * 900}}}

    messages = {
        'u0': {'id': 'u0', 'parentId': None, 'childrenIds': [], 'role': 'user', 'content': 'q',
               'files': [attachment('theirs'), attachment('other-text'), attachment('mine')]},
    }  # fmt: skip
    async with chat_db() as session:
        for file_id, owner, text in (('theirs', 'someone-else', 'x' * 900), ('other-text', 'owner', 'y' * 10),
                                     ('mine', 'owner', 'x' * 900)):  # fmt: skip
            session.add(File(id=file_id, user_id=owner, filename='a', data={'content': text}, meta={}, created_at=0,
                             updated_at=0))  # fmt: skip
        await session.commit()
    await seed_chat(chat_db, messages, 'u0')
    assert await Chats.update_chat_by_id('c', {'title': 'Renamed'}) is not None
    chat, _ = await stored(chat_db)
    theirs, other_text, mine = chat['history']['messages']['u0']['files']
    assert theirs['file']['data']['content'] == 'x' * 900  # a clone's owner cannot read another user's file
    assert other_text['file']['data']['content'] == 'x' * 900  # the record holds different text
    assert 'content' not in mine['file']['data']


@pytest.mark.asyncio
async def test_saves_read_the_compaction_setting_first(small_limits, chat_db, monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr(chats, 'refresh_compaction_flag', refresh)
    await seed_chat(chat_db, chain(1, [10]), 'a0')
    await Chats.update_chat_by_id('c', {'title': 'T'})
    await Chats.upsert_message_to_chat_by_id_and_message_id('c', 'a0', {'content': 'x'})
    assert refresh.await_count == 2


# Round-4 scenarios


def test_an_interrupted_reply_left_unfinished_is_not_protected_ahead_of_recent_work(small_limits, monkeypatch):
    monkeypatch.setattr(env, 'CHAT_BOUND_PROTECTED_TURNS', 2)
    messages = chain(5, [1_500])
    messages['a1'] = tool_message('a1', [3_000, 2_900], parent='u1', done=False, timestamp=4)  # cut off long ago
    messages['a1']['childrenIds'] = ['u2']
    layout = chat_bounds.chat_layout(messages, 'a4')
    assert 'a1' not in layout.protected and {'a3', 'a4'} <= layout.protected
    bounded, _ = bound_messages_map(messages, 'a4')
    assert bounded['a3'] is messages['a3']  # the previous turn the model replays next keeps its result
    still_running = copy.deepcopy(messages)
    still_running['a4']['done'] = False
    assert 'a4' in chat_bounds.chat_layout(still_running, 'a4').protected


def test_regenerating_an_early_turn_of_a_long_chat_keeps_the_previous_attempt_and_the_branch_it_continues(
    small_limits,
):
    messages = chain(12, [700])
    messages['u2']['childrenIds'] = ['a2', 'a2b']
    messages['a2b'] = tool_message('a2b', [1_000], parent='u2', timestamp=30)
    bounded, changed = bound_messages_map(messages, 'a2b')
    assert bounded['a2'] is messages['a2']  # the previous attempt, at the fork
    assert bounded['a0'] is messages['a0'] and bounded['a1'] is messages['a1']  # what the new branch replays
    assert bulk(bounded) <= 10_000
    for key in changed:  # only deeper turns of the abandoned branch, and never to a note
        assert key not in ('a0', 'a1', 'a2', 'a2b')
        assert not output_texts(bounded[key])[0].startswith('[Saved copy removed')


def test_editing_a_prompt_before_the_checkpoint_never_replays_notes(small_limits, monkeypatch):
    monkeypatch.setitem(chat_bounds._compaction, 'enabled', True)
    messages = chain(8, [1_400])
    messages['u5']['contextSummary'] = 'Earlier turns, summarized.'
    bounded, _ = bound_messages_map(messages, 'a7')
    for key in ('a0', 'a1', 'a2', 'a3', 'a4'):  # before the checkpoint: a preview at most, never a note
        assert not output_texts(bounded[key])[0].startswith('[Saved copy removed')
    # The user edits u2: the new branch has no checkpoint and replays a0 and a1 as they are stored.
    bounded['a1']['childrenIds'].append('u2e')
    bounded['u2e'] = {'id': 'u2e', 'parentId': 'a1', 'childrenIds': [], 'role': 'user', 'timestamp': 40}
    edited, _ = bound_messages_map(bounded, 'u2e')
    assert all(len(output_texts(edited[key])[0]) >= 250 for key in ('a0', 'a1'))


def test_off_branch_copies_become_notes_only_when_previews_leave_the_chat_over_budget(small_limits):
    messages = chain(3, [3_000])
    for index in range(3):  # three older attempts at the first answer
        key = f'r{index}'
        messages[key] = tool_message(key, [3_000], parent='u0', timestamp=1.1 + index / 10)
        messages['u0']['childrenIds'].insert(0, key)
    bounded, _ = bound_messages_map(messages, 'a2')
    attempts = [output_texts(bounded[f'r{index}'])[0] for index in range(3)]
    assert all('Saved copy shortened' in text for text in attempts)  # previews suffice: no notes
    many = chain(2, [3_000])
    for index in range(40):
        key = f'r{index}'
        many[key] = tool_message(key, [3_000], parent='u0', timestamp=1.1 + index / 100)
    noted, _ = bound_messages_map(many, 'a1')
    texts = [output_texts(noted[f'r{index}'])[0] for index in range(40)]
    assert any(text.startswith('[Saved copy removed') for text in texts)  # still over with previews: oldest go
    assert texts[-1].startswith('[Saved copy removed') is False  # the newest attempts last
    assert bulk(noted) <= 10_000


@pytest.mark.asyncio
async def test_an_unreadable_file_record_never_makes_a_chat_unsavable(small_limits, chat_db):
    from sqlalchemy import text

    attachment = {'type': 'file', 'id': 'f1', 'file': {'data': {'content': 'x' * 900}}}
    messages = {'u0': {'id': 'u0', 'parentId': None, 'childrenIds': [], 'role': 'user', 'files': [attachment]}}
    await seed_chat(chat_db, messages, 'u0')
    async with chat_db() as session:
        await session.execute(text('DROP TABLE file'))  # the attachment check cannot run
        await session.commit()
    assert await Chats.update_chat_by_id('c', {'title': 'Renamed'}) is not None
    chat, _ = await stored(chat_db)
    assert chat['title'] == 'Renamed'
    assert chat['history']['messages']['u0']['files'][0]['file']['data']['content'] == 'x' * 900


# Round-5 scenarios


def regenerated_after_interruption(dead_done, dead_sizes):
    """30 turns (2 x 150,000 each), a checkpoint on u26, and the newest prompt's reply r0 (``dead_sizes``) cut off
    and left stored with ``done=dead_done``; the user regenerated it and r1 (2 x 4,000,000) finished later."""
    messages = chain(30, [150_000] * 2)
    messages['u26']['contextSummary'] = 'Earlier turns, summarized.'
    messages['a29']['childrenIds'] = ['u30']
    messages['u30'] = {'id': 'u30', 'parentId': 'a29', 'childrenIds': ['r0', 'r1'], 'role': 'user', 'timestamp': 61}
    messages['r0'] = tool_message('r0', dead_sizes, parent='u30', timestamp=62, done=dead_done)
    messages['r1'] = tool_message('r1', [4_000_000] * 2, parent='u30', timestamp=400)
    return messages


def test_a_reply_cut_off_and_then_regenerated_is_ranked_as_a_reply_at_the_fork(monkeypatch):
    monkeypatch.setitem(chat_bounds._compaction, 'enabled', True)
    messages = regenerated_after_interruption(False, [4_000_000] * 2)
    assert 'r0' not in chat_bounds._running(messages, sorted(messages, key=lambda key: messages[key]['timestamp']), ())
    for keep in ({'r1'}, ()):  # the new reply's final save, then the tab's save
        bounded, _ = bound_messages_map(messages, 'r1', keep=keep)
        assert bounded['r1'] is messages['r1']  # the answer the user is on keeps its results
        for key in ('a26', 'a27', 'a28', 'a29'):  # what the model reads after the checkpoint
            assert bounded[key] is messages[key]
        assert bulk({'r0': bounded['r0']}) < 8_000_000  # the dead attempt is no longer kept whole
        finished, _ = bound_messages_map(regenerated_after_interruption(True, [4_000_000] * 2), 'r1', keep=keep)
        assert {key: output_texts(value) for key, value in bounded.items()} == {
            key: output_texts(value) for key, value in finished.items()
        }  # exactly as if it had finished
    smaller = regenerated_after_interruption(False, [1_000_000] * 2)
    bounded, changed = bound_messages_map(smaller, 'r1')
    assert changed and bounded['r0'] is smaller['r0']  # ranked as a reply at the fork, it fits and is kept


def test_replies_of_several_models_started_together_stay_running(small_limits):
    messages = chain(4, [1_500])
    messages['u3']['childrenIds'] = ['a3', 'm3']
    messages['a3'] = tool_message('a3', [3_000, 1_000], parent='u3', timestamp=8, done=False)  # still streaming
    messages['m3'] = tool_message('m3', [3_000, 1_000], parent='u3', timestamp=9)  # the other model, saved now
    bounded, _ = bound_messages_map(messages, 'm3', keep={'m3'})
    assert bounded['a3'] is messages['a3'] and bounded['m3'] is messages['m3']
    messages['m3']['timestamp'] = 30  # a regeneration, started long after a3 stopped
    regenerated, _ = bound_messages_map(messages, 'm3', keep={'m3'})
    assert regenerated['a3'] is not messages['a3']


@pytest.mark.asyncio
async def test_a_result_the_tool_loop_already_capped_is_not_cut_again(small_limits, monkeypatch):
    monkeypatch.setattr(middleware, 'TOOL_RESULT_MAX_CHARS', env.TOOL_RESULT_MAX_CHARS)
    monkeypatch.setattr(env, 'CHAT_MESSAGE_TOOL_OUTPUT_MAX_CHARS', 2 * env.TOOL_RESULT_MAX_CHARS)
    capped, _, _ = await middleware.process_tool_result(None, 'query_rows', 'q' * 6_000, 'external')
    assert len(capped) > env.TOOL_RESULT_MAX_CHARS  # its own note comes after the kept characters

    def with_results(count):
        message = tool_message('a', [0] * count)
        for item in message['output']:
            if item['type'] == 'function_call_output':
                item['output'][0]['text'] = capped
        return message

    one, two, three = with_results(1), with_results(2), with_results(3)
    assert bound_message(one) is one
    assert bound_message(two) is two  # two capped results fill the message's limit, their notes included
    assert bound_message(three) is not three  # over it: cut as before
    assert len(output_texts(bound_message(tool_message('b', [4_400])))[0]) <= env.TOOL_RESULT_MAX_CHARS
