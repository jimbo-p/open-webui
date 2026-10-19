import pytest
from fakeredis.aioredis import FakeRedis
from open_webui import tasks
from open_webui.utils.json_codec import dumps_bytes


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
            delta = dumps_bytes({'type': 'response.output_text.delta', 'output_index': 0,
                                 'content_index': 0, 'delta': suffix}) + b'\n'
            assert await tasks.append_response_stream(redis, 'task', delta, size)
            size += len(delta)
            text += suffix
            streams = await tasks.get_response_streams_by_chat_id(redis, 'chat')
            assert streams == [{'chat_id': 'chat', 'message_id': 'message', 'content': text,
                                'output': [message(text)]}]
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
async def test_non_redis_stream_and_failed_redis_cleanup_remove_local_state():
    await tasks.save_response_stream(None, 'local-task', 'chat', 'message', [message('text')])
    tasks.tasks['local-task'] = object()
    tasks.item_tasks['chat'] = ['local-task']
    assert (await tasks.get_response_streams_by_chat_id(None, 'chat'))[0]['content'] == 'text'

    class UnavailableRedis:
        def pipeline(self, **kwargs):
            raise ConnectionError('unavailable')

    with pytest.raises(ConnectionError):
        await tasks.cleanup_task(UnavailableRedis(), 'local-task', 'chat')
    assert 'local-task' not in tasks.tasks
    assert 'local-task' not in tasks.response_streams
    assert 'chat' not in tasks.item_tasks
