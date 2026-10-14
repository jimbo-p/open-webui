import pytest
from fakeredis.aioredis import FakeRedis
from open_webui import tasks


@pytest.mark.asyncio
async def test_incremental_stream_roundtrip_eviction_and_tool_transition():
    redis = FakeRedis(decode_responses=True)
    state = {}
    await tasks.redis_save_task(redis, 'task', 'chat')
    try:
        for text in ['Hello', 'Hello\n"world" \\ 🧰', 'Hello\n"world" \\ 🧰 done']:
            output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]
            await tasks.save_response_stream(redis, 'task', 'chat', 'message', text, output, state)
            streams = await tasks.get_response_streams_by_chat_id(redis, 'chat')
            assert streams == [{'chat_id': 'chat', 'message_id': 'message', 'content': text, 'output': output}]
            assert await redis.ttl(tasks._stream_key('task')) > 0
            # The next append must recover if Redis evicted the key.
            if text == 'Hello':
                await redis.delete(tasks._stream_key('task'))

        output = [{'type': 'function_call', 'arguments': '{"query":"well data"}'}]
        await tasks.save_response_stream(redis, 'task', 'chat', 'message', '', output, state)
        assert (await tasks.get_response_streams_by_chat_id(redis, 'chat'))[0]['output'] == output
        await tasks.clear_response_stream(redis, 'task')
        assert await tasks.get_response_streams_by_chat_id(redis, 'chat') == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_non_redis_stream_and_failed_redis_cleanup_remove_local_state():
    await tasks.save_response_stream(None, 'local-task', 'chat', 'message', 'text', [], {})
    assert tasks.response_streams['local-task']['content'] == 'text'
    tasks.tasks['local-task'] = object()
    tasks.item_tasks['chat'] = ['local-task']

    class UnavailableRedis:
        def pipeline(self, **kwargs):
            raise ConnectionError('unavailable')

    with pytest.raises(ConnectionError):
        await tasks.cleanup_task(UnavailableRedis(), 'local-task', 'chat')
    assert 'local-task' not in tasks.tasks
    assert 'local-task' not in tasks.response_streams
    assert 'chat' not in tasks.item_tasks
