"""Real row-lock ordering between a settings save and the dreaming publisher.

Opt in with MEMORY_PROMPT_TEST_DATABASE_URL pointing to a disposable PostgreSQL
database named memory_prompt_test_*. These tests create only their own user table.
"""

import asyncio
import copy
import os
import time

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from open_webui.models.users import User, Users
from open_webui.utils.memory_prompt import MemoryPromptError, replace_block

DATABASE_URL = os.getenv('MEMORY_PROMPT_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DATABASE_URL, reason='Requires an isolated PostgreSQL database'),
]
OLD = '<oxy_memory>old memory</oxy_memory>'
NEW = '<oxy_memory>published memory</oxy_memory>'


@pytest_asyncio.fixture
async def database(monkeypatch):
    url = make_url(DATABASE_URL)
    assert url.get_backend_name() == 'postgresql' and url.database.startswith('memory_prompt_test_')
    # Production session sharing is configurable. Exercise the explicit session
    # path here so a cached ORM identity predating publication really is reused.
    from open_webui.internal import db

    monkeypatch.setattr(db, 'DATABASE_ENABLE_SESSION_SHARING', True)
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        # Refuse an existing table rather than treating its name as ownership.
        await connection.run_sync(User.__table__.create)
    async with sessions() as session:
        session.add(
            User(
                id='pilot',
                name='Memory prompt test',
                email='memory-prompt@example.invalid',
                role='user',
                last_active_at=1,
                created_at=1,
                updated_at=1,
                settings={
                    'oxy_memory': {'managed': True, 'version': 1},
                    'ui': {'system': 'Original personal\n\n' + OLD, 'theme': 'light'},
                    'notifications': {'enabled': True},
                },
            )
        )
        await session.commit()
    try:
        yield engine, sessions
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(User.__table__.drop)
        await engine.dispose()


async def wait_for_lock(engine, pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        async with engine.connect() as connection:
            waiting = await connection.scalar(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {'pid': pid}
            )
        if waiting:
            return
        await asyncio.sleep(0.01)
    pytest.fail('Expected transaction did not wait for the PostgreSQL row lock')


def published_settings(current):
    return {**current, 'ui': {**current['ui'], 'system': replace_block(current['ui']['system'], NEW)}}


async def assert_final(sessions):
    async with sessions() as session:
        saved = (await session.get(User, 'pilot')).settings
    assert saved['ui'] == {'system': 'Edited personal\n\n' + NEW, 'theme': 'dark'}
    assert saved['notifications'] == {'enabled': True}
    assert saved['oxy_memory'] == {'managed': True, 'version': 1}


async def test_publisher_first_refetches_stale_orm_identity_before_user_save(database):
    engine, sessions = database
    async with sessions() as ui, sessions() as publisher:
        cached = await ui.get(User, 'pilot')
        assert OLD in cached.settings['ui']['system']
        ui_pid = await ui.scalar(text('SELECT pg_backend_pid()'))
        row = await publisher.scalar(select(User).where(User.id == 'pilot').with_for_update())
        row.settings = published_settings(row.settings)
        await publisher.flush()
        saving = asyncio.create_task(
            Users.update_user_settings_by_id(
                'pilot',
                {
                    'ui': {'system': 'Edited personal\n\n' + OLD, 'theme': 'dark'},
                    'oxy_memory': {'managed': False, 'version': 1},
                },
                db=ui,
            )
        )
        try:
            await wait_for_lock(engine, ui_pid)
            await publisher.commit()
            await asyncio.wait_for(saving, 5)
        finally:
            if not saving.done():
                saving.cancel()
                await asyncio.gather(saving, return_exceptions=True)
    await assert_final(sessions)


async def test_user_save_first_is_seen_by_publisher_waiting_on_same_row(database):
    engine, sessions = database
    async with sessions() as gate, sessions() as ui, sessions() as publisher:
        await ui.get(User, 'pilot')
        ui_pid = await ui.scalar(text('SELECT pg_backend_pid()'))
        publisher_pid = await publisher.scalar(text('SELECT pg_backend_pid()'))
        await gate.scalar(select(User).where(User.id == 'pilot').with_for_update())
        saving = asyncio.create_task(
            Users.update_user_settings_by_id(
                'pilot',
                {
                    'ui': {'system': 'Edited personal\n\n' + OLD, 'theme': 'dark'},
                },
                db=ui,
            )
        )

        async def publish():
            row = await publisher.scalar(select(User).where(User.id == 'pilot').with_for_update())
            row.settings = published_settings(row.settings)
            await publisher.commit()

        publishing = None
        try:
            await wait_for_lock(engine, ui_pid)
            publishing = asyncio.create_task(publish())
            await wait_for_lock(engine, publisher_pid)
            await gate.commit()
            await asyncio.wait_for(asyncio.gather(saving, publishing), 5)
        finally:
            pending = [task for task in (saving, publishing) if task is not None and not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    await assert_final(sessions)


async def test_malformed_user_edit_rolls_back_and_field_specific_save_preserves_prompt(database):
    _, sessions = database
    async with sessions() as session:
        with pytest.raises(MemoryPromptError):
            await Users.update_user_settings_by_id(
                'pilot', {'ui': {'system': 'Personal <oxy_memory>broken'}}, db=session
            )
        await session.rollback()
        await Users.update_user_settings_by_id('pilot', {'tools': {'valves': {'demo': {'safe': True}}}}, db=session)
    async with sessions() as session:
        saved = (await session.get(User, 'pilot')).settings
    assert saved['ui'] == {'system': 'Original personal\n\n' + OLD, 'theme': 'light'}
    assert saved['tools'] == {'valves': {'demo': {'safe': True}}}
    assert saved['oxy_memory'] == {'managed': True, 'version': 1}


@pytest.mark.parametrize(
    'clear,incoming',
    [
        (True, '</oxy_mem' + OLD + 'ory>'),
        (False, OLD + '</oxy_mem' + OLD + 'ory>'),
        (False, OLD + '<oxy_mem' + OLD + 'ory>personal</oxy_mem' + OLD + 'ory>'),
    ],
)
async def test_synthesized_marker_rejection_rolls_back_every_settings_field(database, clear, incoming):
    _, sessions = database
    async with sessions() as session:
        row = await session.get(User, 'pilot')
        if clear:
            row.settings = {**row.settings, 'ui': {**row.settings['ui'], 'system': 'Original personal'}}
            await session.commit()
        original = copy.deepcopy(row.settings)
        with pytest.raises(MemoryPromptError):
            await Users.update_user_settings_by_id(
                'pilot', {'ui': {'system': incoming, 'theme': 'must not save'}}, db=session
            )
        await session.rollback()
    async with sessions() as session:
        assert (await session.get(User, 'pilot')).settings == original
