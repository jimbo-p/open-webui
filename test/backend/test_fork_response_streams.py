from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis
from open_webui import tasks
from open_webui.utils.json_codec import dumps_bytes
from redis.exceptions import ConnectionError as RedisConnectionError


@pytest.fixture(autouse=True)
def isolated_local_task_registries(monkeypatch):
    monkeypatch.setattr(tasks, 'tasks', {})
    monkeypatch.setattr(tasks, 'item_tasks', {})
    monkeypatch.setattr(tasks, 'response_streams', {})


def message(text):
    return {'type': 'message', 'id': 'm', 'content': [{'type': 'output_text', 'text': text}]}


@pytest.mark.asyncio
async def test_native_stream_append_unicode_eviction_and_tool_transition():
    redis = FakeRedis(decode_responses=True)
    key = f'{tasks.REDIS_RESPONSE_STREAMS_KEY}:task'
    await tasks.redis_save_task(redis, 'task', 'chat')
    try:
        size = await tasks.save_response_stream(redis, 'task', 'chat', 'message', [message('Hello')])
        text = 'Hello'
        for suffix in ['\n"world" \\ 🧰', ' done']:
            delta = (
                dumps_bytes(
                    {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': suffix}
                )
                + b'\n'
            )
            assert await tasks.append_response_stream(redis, 'task', delta, size)
            size += len(delta)
            text += suffix
            streams = await tasks.get_response_streams_by_chat_id(redis, 'chat')
            assert streams == [{'chat_id': 'chat', 'message_id': 'message', 'content': text, 'output': [message(text)]}]
            assert await redis.ttl(key) > 0
        # A retry must not duplicate an already appended chunk.
        assert not await tasks.append_response_stream(redis, 'task', delta, size - len(delta))
        assert (await tasks.get_response_streams_by_chat_id(redis, 'chat'))[0]['content'] == text
        await redis.delete(key)
        assert not await tasks.append_response_stream(redis, 'task', delta, size)
        # The native handler takes a complete snapshot when append returns False.
        output = [message(text), {'type': 'function_call', 'arguments': '{"query":"well data"}'}]
        assert await tasks.save_response_stream(redis, 'task', 'chat', 'message', output) > 0
        assert (await tasks.get_response_streams_by_chat_id(redis, 'chat'))[0]['output'] == output
        await tasks.clear_response_stream(redis, 'task')
        assert await tasks.get_response_streams_by_chat_id(redis, 'chat') == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_native_legacy_hash_read_and_cleanup():
    redis = FakeRedis(decode_responses=True)
    await tasks.redis_save_task(redis, 'old-task', 'chat')
    snapshot = {'chat_id': 'chat', 'message_id': 'message', 'content': 'legacy', 'output': [message('legacy')]}
    try:
        await redis.hset(tasks.REDIS_RESPONSE_STREAMS_KEY, 'old-task', dumps_bytes(snapshot))
        assert await tasks.get_response_streams_by_chat_id(redis, 'chat') == [snapshot]
        await tasks.cleanup_task(redis, 'old-task', 'chat')
        assert not await redis.hexists(tasks.REDIS_RESPONSE_STREAMS_KEY, 'old-task')
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_non_redis_cleanup_removes_snapshot_and_preserves_unrelated_tasks():
    await tasks.save_response_stream(None, 'local-task', 'chat', 'message', [message('text')])
    await tasks.save_response_stream(None, 'other-task', 'other-chat', 'other-message', [message('other')])
    tasks.tasks['local-task'] = object()
    other_task = tasks.tasks['other-task'] = object()
    tasks.item_tasks['chat'] = ['local-task']
    tasks.item_tasks['other-chat'] = ['other-task']
    assert (await tasks.get_response_streams_by_chat_id(None, 'chat'))[0]['content'] == 'text'

    await tasks.cleanup_task(None, 'local-task', 'chat')
    assert 'local-task' not in tasks.tasks
    assert 'local-task' not in tasks.response_streams
    assert 'chat' not in tasks.item_tasks
    assert tasks.tasks == {'other-task': other_task}
    assert tasks.item_tasks == {'other-chat': ['other-task']}
    assert (await tasks.get_response_streams_by_chat_id(None, 'other-chat'))[0]['content'] == 'other'


@pytest.mark.asyncio
async def test_redis_cleanup_failure_does_not_retain_local_task_registry(monkeypatch):
    # Redis mode keeps snapshots in Redis, not in response_streams.
    redis = FakeRedis(decode_responses=True)
    other_task = object()
    tasks.tasks.update({'finished': object(), 'other-task': other_task})
    tasks.item_tasks.update({'chat': ['finished', 'other-task'], 'other-chat': ['other-task']})
    failing_cleanup = AsyncMock(side_effect=RedisConnectionError('Redis remains unavailable'))
    monkeypatch.setattr(tasks, 'redis_cleanup_task', failing_cleanup)
    try:
        # Simulate both main's finally and the done callback during the same outage.
        for _ in range(2):
            with pytest.raises(RedisConnectionError):
                await tasks.cleanup_task(redis, 'finished', 'chat')
            assert tasks.tasks == {'other-task': other_task}
            assert tasks.item_tasks == {'chat': ['other-task'], 'other-chat': ['other-task']}
            assert tasks.response_streams == {}
        assert failing_cleanup.await_count == 2
        failing_cleanup.assert_awaited_with(redis, 'finished', 'chat')
    finally:
        await redis.aclose()
