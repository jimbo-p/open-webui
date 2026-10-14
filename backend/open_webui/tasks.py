# tasks.py
import asyncio
import logging
from contextlib import suppress
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

from open_webui.env import REDIS_KEY_PREFIX, REDIS_RESPONSE_STREAM_TTL, REDIS_TASK_TTL
from open_webui.utils.json_codec import JSONCodec, dumps_bytes

log = logging.getLogger(__name__)

# A dictionary to keep track of active tasks
tasks: dict[str, asyncio.Task] = {}
item_tasks = {}
response_streams: dict[str, dict] = {}


REDIS_TASKS_KEY = f'{REDIS_KEY_PREFIX}:tasks'
REDIS_ITEM_TASKS_KEY = f'{REDIS_KEY_PREFIX}:tasks:item'
REDIS_RESPONSE_STREAM_KEY_PREFIX = f'{REDIS_KEY_PREFIX}:tasks:response_stream'
REDIS_PUBSUB_CHANNEL = f'{REDIS_KEY_PREFIX}:tasks:commands'
REDIS_PUBSUB_RECONNECT_INTERVAL = 1.0
REDIS_PUBSUB_MAX_RECONNECT_INTERVAL = 30.0


async def redis_task_command_listener(app):
    redis: Redis = app.state.redis
    reconnect_interval = REDIS_PUBSUB_RECONNECT_INTERVAL

    while True:
        pubsub = None
        try:
            # RedisCluster can't route a pubsub subscribe until initialize() fills its slot cache.
            await redis.initialize()

            pubsub = redis.pubsub()
            await pubsub.subscribe(REDIS_PUBSUB_CHANNEL)
            reconnect_interval = REDIS_PUBSUB_RECONNECT_INTERVAL

            async for message in pubsub.listen():
                if message['type'] != 'message':
                    continue
                try:
                    command = JSONCodec.loads(message['data'])
                    if command.get('action') != 'stop':
                        continue

                    local_task = tasks.get(command.get('task_id'))
                    if local_task:
                        local_task.cancel()
                except Exception as e:
                    log.exception(f'Error handling distributed task command: {e}')
            log.warning('Redis task command listener stopped. Retrying.')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f'Redis task command listener failed. Retrying: {e}')
        finally:
            if pubsub:
                with suppress(Exception):
                    await pubsub.aclose()

        await asyncio.sleep(reconnect_interval)
        reconnect_interval = min(reconnect_interval * 2, REDIS_PUBSUB_MAX_RECONNECT_INTERVAL)


async def redis_task_heartbeat(app):
    redis: Redis = app.state.redis
    while True:
        await asyncio.sleep(REDIS_TASK_TTL / 4)
        try:
            pipe = redis.pipeline(transaction=False)
            for task_id in list(tasks):
                # EXPIRE cannot recreate a task already removed by cleanup.
                pipe.expire(f'{REDIS_TASKS_KEY}:{task_id}', REDIS_TASK_TTL)
            await pipe.execute()
        except Exception:
            log.exception('Redis task heartbeat failed')


### ------------------------------
### REDIS-ENABLED HANDLERS
### ------------------------------


async def redis_save_task(redis: Redis, task_id: str, item_id: str | None):
    pipe = redis.pipeline(transaction=False)
    pipe.set(f'{REDIS_TASKS_KEY}:{task_id}', '1', ex=REDIS_TASK_TTL or None)
    pipe.hset(REDIS_TASKS_KEY, task_id, item_id or '')
    if item_id:
        pipe.sadd(f'{REDIS_ITEM_TASKS_KEY}:{item_id}', task_id)
    await pipe.execute()


async def redis_cleanup_task(redis: Redis, task_id: str, item_id: str | None):
    pipe = redis.pipeline(transaction=False)
    pipe.delete(f'{REDIS_TASKS_KEY}:{task_id}')
    pipe.hdel(REDIS_TASKS_KEY, task_id)
    pipe.delete(_stream_key(task_id))
    if item_id:
        pipe.srem(f'{REDIS_ITEM_TASKS_KEY}:{item_id}', task_id)
    await pipe.execute()


async def redis_list_tasks(redis: Redis, item_id: str | None = None) -> list[str]:
    task_ids = list(
        await redis.smembers(f'{REDIS_ITEM_TASKS_KEY}:{item_id}')
        if item_id is not None
        else await redis.hkeys(REDIS_TASKS_KEY)
    )
    if not task_ids or REDIS_TASK_TTL == 0:
        return task_ids

    pipe = redis.pipeline(transaction=False)
    for task_id in task_ids:
        pipe.exists(f'{REDIS_TASKS_KEY}:{task_id}')

    active = []
    for task_id, exists in zip(task_ids, await pipe.execute()):
        if exists:
            active.append(task_id)
        else:
            task_item_id = item_id if item_id is not None else await redis.hget(REDIS_TASKS_KEY, task_id)
            await redis_cleanup_task(redis, task_id, task_item_id or None)
    return active


async def redis_send_command(redis: Redis, command: dict):
    command_json = dumps_bytes(command)
    # RedisCluster doesn't expose publish() directly, but the
    # PUBLISH command broadcasts across all cluster nodes server-side.
    if hasattr(redis, 'nodes_manager'):
        await redis.execute_command('PUBLISH', REDIS_PUBSUB_CHANNEL, command_json)
    else:
        await redis.publish(REDIS_PUBSUB_CHANNEL, command_json)


async def cleanup_task(redis, task_id: str, id=None):
    """
    Remove a completed or canceled task from the global `tasks` dictionary.
    """
    # local state first: it must go even when Redis is unreachable
    tasks.pop(task_id, None)  # Remove the task if it exists
    response_streams.pop(task_id, None)

    # If an ID is provided, remove the task from the item_tasks dictionary
    if id and task_id in item_tasks.get(id, []):
        item_tasks[id].remove(task_id)
        if not item_tasks[id]:  # If no tasks left for this ID, remove the entry
            item_tasks.pop(id, None)

    if redis:
        await redis_cleanup_task(redis, task_id, id)


async def create_task(redis, coroutine, id=None, task_id=None):
    """
    Create a new asyncio task and add it to the global task dictionary.
    """
    task_id = task_id or str(uuid4())  # Generate a unique ID for the task
    task = asyncio.create_task(coroutine)  # Create the task

    # Add a done callback for cleanup
    task.add_done_callback(lambda t: asyncio.create_task(cleanup_task(redis, task_id, id)))
    tasks[task_id] = task

    # If an ID is provided, associate the task with that ID
    if id:
        if item_tasks.get(id):
            item_tasks[id].append(task_id)
        else:
            item_tasks[id] = [task_id]

    if redis:
        await redis_save_task(redis, task_id, id)

    return task_id, task


async def list_tasks(redis):
    """
    List all currently active task IDs.
    """
    if redis:
        return await redis_list_tasks(redis)
    return list(tasks.keys())


async def list_task_ids_by_item_id(redis, id):
    """
    List all tasks associated with a specific ID.
    """
    if redis:
        return await redis_list_tasks(redis, id)
    return list(item_tasks.get(id, []))


def _stream_key(task_id: str) -> str:
    return f'{REDIS_RESPONSE_STREAM_KEY_PREFIX}:{task_id}'


def _escape_text(text: str) -> str:
    """Serialize text as a JSON string body, without the surrounding quotes."""
    # Escaping is per code point, so it distributes over APPEND and leaves no raw newline.
    return JSONCodec.dumps(text)[1:-1]


def _longest_text(node: Any) -> str:
    """Find the longest string leaf anywhere in node."""
    if isinstance(node, str):
        return node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    return max((_longest_text(child) for child in children), key=len, default='')


def _elide_text(node: Any, text: str, path: tuple[Any, ...] = ()) -> tuple[Any, list[list]]:
    """Copy node with every string leaf equal to text blanked, and collect their paths."""
    if isinstance(node, str):
        return ('', [list(path)]) if node == text else (node, [])
    if isinstance(node, dict):
        keys = list(node)
        values = [node[key] for key in keys]
    elif isinstance(node, list):
        keys = list(range(len(node)))
        values = node
    else:
        return node, []

    copies = []
    paths = []
    for key, value in zip(keys, values):
        copy, elided_paths = _elide_text(value, text, path + (key,))
        copies.append(copy)
        paths += elided_paths
    return (dict(zip(keys, copies)) if isinstance(node, dict) else copies), paths


def _restore_text(data: dict[str, Any], paths: list[list], text: str) -> None:
    """Put text back at every path _elide_text blanked."""
    for path in paths:
        node = data
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = text


async def save_response_stream(
    redis,
    task_id: str | None,
    chat_id: str | None,
    message_id: str | None,
    content: str,
    output: list,
    state: dict[str, Any],
):
    if not task_id or not chat_id or not message_id:
        return

    data = {
        'chat_id': chat_id,
        'message_id': message_id,
        'content': content,
        'output': output,
    }

    if redis:
        text = content or _longest_text(data)
        elided, paths = _elide_text(data, text)
        head = JSONCodec.dumps({**elided, 'paths': paths})

        key = _stream_key(task_id)
        appending = state.get('head') == head and text.startswith(state['text'])
        pipe = redis.pipeline()
        if appending:
            delta = _escape_text(text[len(state['text']) :])
            size = state['size'] + len(delta.encode('utf-8'))
            pipe.append(key, delta)
        else:
            value = f'{head}\n{_escape_text(text)}'
            size = len(value.encode('utf-8'))
            pipe.set(key, value)
        if REDIS_RESPONSE_STREAM_TTL > 0:
            pipe.expire(key, REDIS_RESPONSE_STREAM_TTL)
        results = await pipe.execute()

        # An unexpected length means the key was evicted or already received this delta.
        if appending and results[0] != size:
            value = f'{head}\n{_escape_text(text)}'
            size = len(value.encode('utf-8'))
            await redis.set(key, value, ex=REDIS_RESPONSE_STREAM_TTL if REDIS_RESPONSE_STREAM_TTL > 0 else None)
        state.update(head=head, text=text, size=size)
    else:
        response_streams[task_id] = data


async def get_response_streams_by_chat_id(redis, chat_id: str) -> list[dict]:
    task_ids = await list_task_ids_by_item_id(redis, chat_id)
    if not task_ids:
        return []

    if redis:
        values = await asyncio.gather(*(redis.get(_stream_key(task_id)) for task_id in task_ids))
        streams = []
        for value in values:
            if not value:
                continue
            try:
                head, _, text = value.partition('\n')
                data = JSONCodec.loads(head)
                _restore_text(data, data.pop('paths'), JSONCodec.loads('"' + text + '"'))
            except Exception:
                continue
            if data.get('chat_id') == chat_id:
                streams.append(data)
        return streams

    return [
        stream for task_id in task_ids if (stream := response_streams.get(task_id)) and stream.get('chat_id') == chat_id
    ]


async def clear_response_stream(redis, task_id: str | None):
    if not task_id:
        return
    if redis:
        await redis.delete(_stream_key(task_id))
    else:
        response_streams.pop(task_id, None)


async def stop_task(redis, task_id: str):
    """
    Cancel a running task and remove it from the global task list.
    """
    if redis:
        # Look up the item_id before cleanup so we can remove the set entry too
        item_id = await redis.hget(REDIS_TASKS_KEY, task_id)
        # PUBSUB: All instances check if they have this task, and stop if so.
        await redis_send_command(
            redis,
            {
                'action': 'stop',
                'task_id': task_id,
            },
        )
        # Always clean Redis directly — hdel/srem are idempotent, safe even
        # if the done_callback on the owning process also fires cleanup.
        await redis_cleanup_task(redis, task_id, item_id or None)
        return {'status': True, 'message': f'Task {task_id} stopped.'}

    task = tasks.pop(task_id, None)
    if not task:
        return {'status': False, 'message': f'Task with ID {task_id} not found.'}

    task.cancel()  # Request task cancellation
    try:
        await task  # Wait for the task to handle the cancellation
    except asyncio.CancelledError:
        # Task successfully canceled
        return {'status': True, 'message': f'Task {task_id} successfully stopped.'}

    if task.cancelled() or task.done():
        return {'status': True, 'message': f'Task {task_id} successfully cancelled.'}

    return {'status': True, 'message': f'Cancellation requested for {task_id}.'}


async def stop_item_tasks(redis: Redis, item_id: str):
    """
    Stop all tasks associated with a specific item ID.
    """
    task_ids = await list_task_ids_by_item_id(redis, item_id)
    if not task_ids:
        return {'status': True, 'message': f'No tasks found for item {item_id}.'}

    # Cleanup mutates the local task list while cancellation is awaited.
    for task_id in list(task_ids):
        # A task that already finished needs no stopping; continue with the rest.
        await stop_task(redis, task_id)

    return {'status': True, 'message': f'All tasks for item {item_id} stopped.'}


async def has_active_tasks(redis, chat_id: str) -> bool:
    """Check if a chat has any active tasks."""
    task_ids = await list_task_ids_by_item_id(redis, chat_id)
    return len(task_ids) > 0
