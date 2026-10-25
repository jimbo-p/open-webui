"""Release regressions retained when replacing backports with native upstream code."""

import io
import stat
import zipfile
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from open_webui.utils import context_compaction as compaction
from open_webui.utils.recurrence import _next_occurrences
from open_webui.utils.response import merge_usage
from open_webui.utils.skill_files import parse_import
from open_webui.utils.task import follow_up_generation_template, parse_follow_ups


def tool_rounds():
    messages = [{'role': 'user', 'content': 'Investigate the production data.'}]
    for index in range(3):
        messages.extend(
            [
                {
                    'role': 'assistant',
                    'tool_calls': [
                        {'id': str(index), 'type': 'function', 'function': {'name': 'query', 'arguments': '{}'}}
                    ],
                },
                {'role': 'tool', 'tool_call_id': str(index), 'content': 'Rows returned.'},
            ]
        )
    return messages


@pytest.mark.asyncio
async def test_long_tool_run_compacts_from_reported_usage_without_splitting_pairs(monkeypatch):
    messages = tool_rounds()
    messages[-2]['usage'] = {'prompt_tokens': 90000, 'completion_tokens': 1000}
    monkeypatch.setattr(
        compaction,
        '_load_config',
        AsyncMock(
            return_value={
                'enable': True,
                'token_threshold': 60000,
                'token_cap': 100000,
                'retention_percentage': 40,
                'prompt_template': '',
            }
        ),
    )
    summarize = AsyncMock(return_value='The investigation found three matching rows.')
    monkeypatch.setattr(compaction, '_generate_summary', summarize)
    kept, summary, did_compact = await compaction.compact_messages_for_request(None, None, messages, {}, 'model', {})
    assert did_compact and summary == summarize.return_value
    assert kept[0]['role'] == 'assistant' and kept[0]['tool_calls']
    calls = {call['id'] for message in kept for call in message.get('tool_calls', [])}
    assert {message['tool_call_id'] for message in kept if message['role'] == 'tool'} == calls


def test_tool_context_counts_fresh_results_and_uses_latest_call_not_cumulative_billing():
    messages = tool_rounds()
    usage = merge_usage(
        {'prompt_tokens': 80000, 'completion_tokens': 1000}, {'prompt_tokens': 20000, 'completion_tokens': 1000}
    )
    messages[-2]['usage'] = usage
    assert not compaction._exceeds_token_threshold(messages, '', None, 60000)
    messages[-1]['content'] = 'x' * 200000
    assert compaction._exceeds_token_threshold(messages, '', None, 60000)


def test_tool_context_without_provider_usage_still_estimates_and_can_be_disabled():
    messages = tool_rounds()
    messages[-1]['content'] = 'x' * 200000
    assert compaction._exceeds_token_threshold(messages, '', None, 40000)
    assert not compaction._exceeds_token_threshold(messages, '', None, 0)


@pytest.mark.parametrize(
    'frequency,extra,second_date',
    [
        ('DAILY', '', datetime(2026, 10, 13, 18, 30)),
        ('WEEKLY', ';BYDAY=MO', datetime(2026, 10, 19, 18, 30)),
        ('MONTHLY', ';BYMONTHDAY=12', datetime(2026, 11, 12, 18, 30)),
    ],
)
def test_calendar_schedules_keep_users_first_run_and_exact_minute(frequency, extra, second_date):
    rule = f'RRULE:FREQ={frequency};BYHOUR=18;BYMINUTE=30{extra}'
    # The user's local clock is behind the server. No default dateutil anchor may replace it.
    first = datetime(2026, 10, 12, 18, 30)
    assert _next_occurrences(rule, datetime(2026, 10, 12, 18, 29, 47), 2) == [first, second_date]
    assert _next_occurrences(rule, first.replace(second=1), 1) == [second_date]


def skill_zip(link_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('repo-main/skills/demo/SKILL.md', '---\nname: demo\n---\nUse the data tool.')
        link = zipfile.ZipInfo(link_path)
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, '../../outside')
    return output.getvalue()


@pytest.mark.parametrize('directory', [None, 'skills/demo/'])
def test_repository_skill_discovery_ignores_unrelated_links(directory):
    packages = parse_import(skill_zip('repo-main/README-link'), 'repo.zip', discover_skills=True, directory=directory)
    assert len(packages) == 1
    assert [file['path'] for file in packages[0]['files']] == ['SKILL.md']


@pytest.mark.parametrize(
    'discover,link_path',
    [
        (False, 'repo-main/README-link'),
        (True, 'repo-main/skills/demo/link'),
        (False, 'repo-main/skills/demo/link'),
    ],
)
def test_skill_import_keeps_rejecting_links_in_content_and_uploaded_archives(discover, link_path):
    with pytest.raises(ValueError, match='links or special files'):
        parse_import(skill_zip(link_path), 'repo.zip', discover_skills=discover)


@pytest.mark.asyncio
async def test_follow_up_default_bounds_context_and_custom_template_keeps_full_text():
    messages = [{'role': 'user', 'content': 'original objective'}]
    messages += [{'role': 'assistant', 'content': f'intermediate {index}'} for index in range(6)]
    messages.append(
        {
            'role': 'assistant',
            'content': [
                {'type': 'text', 'text': 'latest answer ' + 'x' * 2000},
                {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/image'}},
            ],
        }
    )
    template = '{{MESSAGES}}'
    default = await follow_up_generation_template(template, messages, use_default=True)
    custom = await follow_up_generation_template(template, messages)
    assert 'original objective' in default and 'latest answer' in default
    assert 'intermediate 0' not in default and 'intermediate 5' in default
    assert 'x' * 1001 not in default and 'x' * 2000 in custom
    assert 'image_url' not in default and 'example.invalid' not in default
    assert isinstance(messages[-1]['content'], list)


def test_follow_up_parser_rejects_non_strings_and_deduplicates_suggestions():
    assert parse_follow_ups('```json\n{"follow_ups":[" Continue ","Continue","", "Compare"]}\n```') == [
        'Continue',
        'Compare',
    ]
    assert parse_follow_ups('{"follow_ups":["Continue", {}]}') == []
    assert parse_follow_ups('{"follow_ups":"Continue"}') == []
    assert parse_follow_ups('not JSON') == []


@pytest.mark.asyncio
async def test_native_tool_checkpoint_survives_incremental_stream_persistence():
    from fakeredis.aioredis import FakeRedis
    from open_webui import tasks

    redis = FakeRedis(decode_responses=True)
    output = [
        {'type': 'function_call', 'call_id': 'old', 'name': 'query', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'old', 'output': 'Earlier result'},
        {'type': 'function_call', 'call_id': 'kept', 'name': 'query', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'kept', 'output': 'Current result'},
        {'type': 'open_webui:context_compaction', 'call_id': 'kept', 'summary': 'Earlier findings'},
    ]
    await tasks.redis_save_task(redis, 'checkpoint-task', 'checkpoint-chat')
    try:
        for content in ('The answer', 'The answer is ready.'):
            reply = {'type': 'message', 'content': [{'type': 'output_text', 'text': content}]}
            await tasks.save_response_stream(redis, 'checkpoint-task', 'checkpoint-chat', 'reply', output + [reply])
        saved = (await tasks.get_response_streams_by_chat_id(redis, 'checkpoint-chat'))[0]
        messages = [{'role': 'user', 'content': 'Continue the investigation'}, {'role': 'assistant', **saved}]
        kept, summary = compaction._apply_latest_summary_checkpoint(messages)
        assert summary == 'Earlier findings'
        assert kept[-1]['content'] == 'The answer is ready.'
        assert kept[-1]['output'] == output[2:4] + [reply]
        assert saved['output'] == output + [reply]
    finally:
        await tasks.cleanup_task(redis, 'checkpoint-task', 'checkpoint-chat')
        await redis.aclose()


@pytest.mark.asyncio
async def test_native_dispatch_history_survives_incremental_stream_persistence():
    import json

    from fakeredis.aioredis import FakeRedis
    from open_webui import tasks
    from open_webui.utils.misc import convert_output_to_messages
    from open_webui.utils.tool_search import resolve_tool_call

    provider_function = {
        'name': 'call_tool',
        'arguments': json.dumps({'tool_name': 'lookup', 'params': '{"query":"well data"}'}),
    }
    call = {'id': 'dispatch-1', 'function': dict(provider_function)}
    tools = {'call_tool': {'tool_id': 'builtin:call_tool'}, 'lookup': {'tool_id': 'mcp:lookup'}}
    assert resolve_tool_call(call, tools) is None
    assert call['function']['name'] == 'lookup'
    assert json.loads(call['function']['arguments']) == {'query': 'well data'}
    # The execution path resolves again after approval; this must remain idempotent.
    assert resolve_tool_call(call, tools) is None
    assert call['provider_function'] == provider_function
    output = [
        {
            'type': 'function_call',
            'id': call['id'],
            'call_id': call['id'],
            **call['function'],
            'provider_function': call['provider_function'],
            'status': 'pending',
        }
    ]
    redis = FakeRedis(decode_responses=True)
    await tasks.redis_save_task(redis, 'dispatch-task', 'dispatch-chat')
    try:
        await tasks.save_response_stream(redis, 'dispatch-task', 'dispatch-chat', 'reply', output)
        output[0]['status'] = 'completed'
        output.append(
            {
                'type': 'function_call_output',
                'call_id': call['id'],
                'output': [{'type': 'input_text', 'text': 'Matching rows'}],
                'status': 'completed',
            }
        )
        await tasks.save_response_stream(redis, 'dispatch-task', 'dispatch-chat', 'reply', output)
        saved = (await tasks.get_response_streams_by_chat_id(redis, 'dispatch-chat'))[0]['output']
        assert saved == output
        messages = convert_output_to_messages(saved)
        replay_call = next(m for m in messages if m.get('tool_calls'))['tool_calls'][0]
        assert replay_call['function'] == provider_function
        result = next(m for m in messages if m['role'] == 'tool')
        assert result['tool_call_id'] == replay_call['id'] == call['id']
        assert 'Matching rows' in result['content']
    finally:
        await tasks.cleanup_task(redis, 'dispatch-task', 'dispatch-chat')
        await redis.aclose()
