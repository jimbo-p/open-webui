"""Timer admission, durable delivery and scheduler-capacity regressions.

Every database used here is a disposable SQLite file under pytest's tmp_path.
PostgreSQL SKIP LOCKED and multiworker checks use the separate disposable probe.
"""

import asyncio
import copy
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from open_webui import tasks as task_module
from open_webui.models import chat_messages
from open_webui.models.chat_messages import ChatMessage
from open_webui.models.chats import Chat
from open_webui.socket.main import sio
from open_webui.utils import automations, subagents, timers


@pytest_asyncio.fixture
async def timer_db(monkeypatch, tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "timers.db"}')
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

    monkeypatch.setattr(timers, 'get_async_db', database)
    monkeypatch.setattr(chat_messages, 'get_async_db_context', database)
    monkeypatch.setattr(timers, '_timer_locks', {})
    monkeypatch.setattr(subagents, '_parent_locks', {})
    monkeypatch.setattr(task_module, 'tasks', {})
    monkeypatch.setattr(timers, 'has_active_tasks', AsyncMock(return_value=False))
    monkeypatch.setattr(sio, 'emit', AsyncMock())
    monkeypatch.setattr(
        timers.Users, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(id='owner', role='user'))
    )
    monkeypatch.setattr(
        timers.Chats, 'get_message_by_id_and_message_id', AsyncMock(return_value={'content': 'Continue'})
    )

    async def load_timer(timer_id):
        async with factory() as db:
            return await db.get(Chat, timer_id)

    monkeypatch.setattr(timers.Chats, 'get_chat_by_id', load_timer)
    monkeypatch.setattr(
        timers.Chats,
        'get_chat_by_id_and_user_id',
        AsyncMock(side_effect=AssertionError('The parent must not be loaded before admission')),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(redis=None, CHAT_COMPLETION_HANDLER=AsyncMock(return_value={'task_ids': []}))
    )

    async def seed(parent='parent', timer_id='timer', *, status='running', claim='claim', due=None):
        due = time.time_ns() - 10_000_000_000 if due is None else due
        meta = {
            'internal': True,
            'type': 'timer',
            'parent_chat_id': parent,
            'parent_message_id': None,
            'timer_task_message_id': 'prompt',
            'timer_at': due,
            'status': status,
            'timer_claim_id': claim,
            'cancel_on': ['chat.read'],
            'run': {'model_id': 'model', 'tool_ids': ['tools'], 'files': [{'id': 'original'}]},
        }
        async with factory() as db:
            if await db.get(Chat, parent) is None:
                db.add(Chat(id=parent, user_id='owner', title='Parent', chat={'history': {'messages': {}}}, meta={}))
            db.add(
                Chat(
                    id=timer_id,
                    user_id='owner',
                    title='Timer',
                    chat={},
                    meta=meta,
                    timer_at=due if status == 'pending' else None,
                )
            )
            await db.commit()
        return copy.deepcopy(meta)

    try:
        yield SimpleNamespace(engine=engine, factory=factory, app=app, seed=seed, load=load_timer)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_batch_is_fair_and_keeps_unadmitted_work_pending(timer_db):
    db = timer_db
    for index in range(20):
        await db.seed(timer_id=f'large-{index}', status='pending')
    await db.seed(parent='other-parent', timer_id='other', status='pending')
    claims = await timers.claim_due_timers(time.time_ns(), limit=2)
    assert len(claims) == 2
    assert 'other' in {timer_id for timer_id, _ in claims}
    async with db.factory() as session:
        rows = (
            (await session.execute(select(Chat.meta).where(Chat.meta['type'].as_string() == 'timer'))).scalars().all()
        )
    assert sum(meta['status'] == 'running' for meta in rows) == 2
    assert sum(meta['status'] == 'pending' for meta in rows) == 19
    assert await timers.claim_due_timers(time.time_ns(), limit=0) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['local', 'response'])
async def test_busy_parent_queues_without_loading_parent_json(timer_db, monkeypatch, busy):
    db = timer_db
    original = await db.seed()
    reads = []

    def record_sql(connection, cursor, statement, parameters, context, many):
        if 'SELECT' in statement.upper() and 'chat.chat' in statement and 'parent' in parameters:
            reads.append(statement)

    event.listen(db.engine.sync_engine, 'before_cursor_execute', record_sql)
    lock = None
    if busy == 'local':
        lock = subagents._parent_locks['parent'] = asyncio.Lock()
        await lock.acquire()
    else:
        monkeypatch.setattr(timers, 'has_active_tasks', AsyncMock(return_value=True))
    before = time.time_ns()
    try:
        await timers.execute_due_timer(db.app, 'timer', 'claim')
    finally:
        if lock:
            lock.release()
    row = await db.load('timer')
    assert not reads
    assert row.meta['status'] == 'pending'
    assert row.timer_at > before
    for key in ('timer_at', 'run', 'cancel_on', 'parent_chat_id'):
        assert row.meta[key] == original[key]
    db.app.state.CHAT_COMPLETION_HANDLER.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_claim_cannot_rearm_error_or_deliver(timer_db):
    db = timer_db
    await db.seed(claim='new-claim')
    await timers.rearm_due_timer('timer', 'old-claim')
    assert not await timers._set_timer_state('timer', 'error', claim_id='old-claim', timer_error='stale')
    await timers.execute_due_timer(db.app, 'timer', 'old-claim')
    row = await db.load('timer')
    parent = await db.load('parent')
    assert row.meta['status'] == 'running' and row.meta['timer_claim_id'] == 'new-claim'
    assert parent.chat['history']['messages'] == {}
    db.app.state.CHAT_COMPLETION_HANDLER.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_failure_cannot_overwrite_a_replacement_claim(timer_db, monkeypatch):
    db = timer_db
    await db.seed()

    async def owner_was_removed(*args):
        async with db.factory() as session:
            row = await session.get(Chat, 'timer')
            row.meta = {**row.meta, 'timer_claim_id': 'replacement'}
            await session.commit()
        return None

    monkeypatch.setattr(timers.Users, 'get_user_by_id', owner_was_removed)
    await timers.execute_due_timer(db.app, 'timer', 'claim')
    row = await db.load('timer')
    assert row.meta['status'] == 'running'
    assert row.meta['timer_claim_id'] == 'replacement'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['upsert', 'socket', 'handler', 'cancel'])
async def test_delivery_failures_are_atomic_or_visible_without_replay(timer_db, monkeypatch, failure):
    db = timer_db
    await db.seed()
    if failure == 'upsert':
        monkeypatch.setattr(timers.ChatMessages, 'upsert_message', AsyncMock(side_effect=RuntimeError('upsert fault')))
    elif failure == 'socket':
        monkeypatch.setattr(sio, 'emit', AsyncMock(side_effect=RuntimeError('socket fault')))
    elif failure == 'handler':
        db.app.state.CHAT_COMPLETION_HANDLER.side_effect = RuntimeError('handler fault')
    else:
        db.app.state.CHAT_COMPLETION_HANDLER.side_effect = asyncio.CancelledError()

    await timers.execute_due_timer(db.app, 'timer', 'claim')
    row = await db.load('timer')
    parent = await db.load('parent')
    messages = parent.chat['history']['messages']
    if failure == 'upsert':
        assert row.meta['status'] == 'pending'
        assert messages == {}
    else:
        assert row.meta['status'] == 'error'
        assistants = [message for message in messages.values() if message['role'] == 'assistant']
        assert len(messages) == 2 and len(assistants) == 1
        assert assistants[0]['done'] is True and assistants[0]['error']['content']
        saved = await timers.ChatMessages.get_message_by_id(f'parent-{assistants[0]["id"]}')
        assert saved.done is True and saved.error
        await timers.rearm_due_timer('timer', 'claim')
        assert (await db.load('timer')).meta['status'] == 'error'
    assert len(parent.chat['history']['messages']) <= 2


@pytest.mark.asyncio
async def test_post_handoff_error_preserves_live_response(timer_db):
    db = timer_db
    await db.seed()
    finish = asyncio.Event()
    response = asyncio.create_task(finish.wait())
    task_module.tasks['response'] = response

    async def handler(request, form_data, **kwargs):
        await timers.ChatMessages.upsert_message(form_data['id'], 'parent', 'owner', {'meta': {'task_id': 'response'}})
        raise RuntimeError('error after registration')

    db.app.state.CHAT_COMPLETION_HANDLER.side_effect = handler
    try:
        assert await timers.execute_due_timer(db.app, 'timer', 'claim') == ['response']
        assert not response.done()
        assert (await db.load('timer')).meta['status'] == 'completed'
        assert len((await db.load('parent')).chat['history']['messages']) == 2
    finally:
        finish.set()
        await response


@pytest.mark.asyncio
async def test_cancelled_dispatcher_still_reserves_capacity_for_live_response(monkeypatch):
    finish = asyncio.Event()
    prepared = asyncio.Event()
    response = asyncio.create_task(finish.wait())
    monkeypatch.setattr(task_module, 'tasks', {'response': response})

    async def prepare(*args):
        prepared.set()
        return ['response']

    monkeypatch.setattr(timers, 'execute_due_timer', prepare)
    rearm = AsyncMock()
    monkeypatch.setattr(timers, 'rearm_due_timer', rearm)
    dispatcher = asyncio.create_task(automations._execute_timer_dispatch(None, 'timer', 'claim'))
    occupied = {dispatcher}
    dispatcher.add_done_callback(occupied.discard)
    await prepared.wait()
    await asyncio.sleep(0)
    dispatcher.cancel()
    await asyncio.sleep(0.01)
    assert len(occupied) == 1
    assert not dispatcher.done() and not response.cancelled()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(dispatcher, 1)
    assert response.done() and not response.cancelled()
    rearm.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_claims_only_free_slots_and_keeps_other_schedule_checks_running(monkeypatch):
    finish = asyncio.Event()
    response_tasks = {}
    claims = []

    async def claim(now, limit):
        claims.append(limit)
        result = []
        for _ in range(limit):
            timer_id = f'timer-{len(response_tasks)}'
            response_tasks[timer_id] = asyncio.create_task(finish.wait())
            result.append((timer_id, 'claim'))
        return result

    async def prepare(app, timer_id, claim_id):
        return [timer_id]

    async def short_sleep(seconds):
        await asyncio.sleep(0.001)

    shim = SimpleNamespace(
        create_task=asyncio.create_task,
        gather=asyncio.gather,
        shield=asyncio.shield,
        CancelledError=asyncio.CancelledError,
        Task=asyncio.Task,
        sleep=short_sleep,
    )
    config = AsyncMock(return_value=False)
    monkeypatch.setattr(automations, 'asyncio', shim)
    monkeypatch.setattr(automations, 'TIMER_MAX_CONCURRENT', 2)
    monkeypatch.setattr(automations.Config, 'get', config)
    monkeypatch.setattr(timers, 'claim_due_timers', claim)
    monkeypatch.setattr(timers, 'execute_due_timer', prepare)
    monkeypatch.setattr(task_module, 'tasks', response_tasks)
    scheduler = asyncio.create_task(automations.scheduler_worker_loop(SimpleNamespace()))
    try:
        for _ in range(30):
            await asyncio.sleep(0.001)
        assert claims == [2]
        assert len(response_tasks) == 2
        assert {call.args[0] for call in config.await_args_list} >= {'automations.enable', 'calendar.enable'}
    finally:
        scheduler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scheduler
        finish.set()
        await asyncio.gather(*response_tasks.values())
        await asyncio.sleep(0.01)
