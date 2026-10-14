from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from open_webui.models import config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(config.Config.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def get_db():
        async with factory() as session:
            yield session

    monkeypatch.setattr(config, 'get_async_db', get_db)
    monkeypatch.setattr(config, '_snapshot', None)
    monkeypatch.setattr(config, 'CONFIG_CACHE_TTL', 60)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_external_transaction_invalidates_only_after_commit(sessions):
    await config.Config.upsert({'test.value': {'items': [1]}})
    assert await config.Config.get('test.value') == {'items': [1]}
    generation = config._cache_generation
    async with sessions() as session:
        await config.Config.upsert({'test.value': {'items': [2]}}, db=session)
        assert config._cache_generation == generation
        assert await config.Config.get('test.value') == {'items': [1]}
        await session.commit()
    assert config._cache_generation > generation
    assert await config.Config.get('test.value') == {'items': [2]}
    value = await config.Config.get('test.value')
    value['items'].append(3)
    assert await config.Config.get('test.value') == {'items': [2]}


@pytest.mark.asyncio
async def test_rollback_keeps_committed_snapshot(sessions):
    await config.Config.upsert({'test.value': 'original'})
    assert await config.Config.get('test.value') == 'original'
    generation = config._cache_generation
    async with sessions() as session:
        await config.Config.upsert({'test.value': 'aborted'}, db=session)
        await session.rollback()
    assert config._cache_generation == generation
    assert await config.Config.get('test.value') == 'original'


@pytest.mark.asyncio
async def test_savepoint_does_not_consume_outer_commit_invalidation(sessions):
    await config.Config.upsert({'test.value': 'original'})
    assert await config.Config.get('test.value') == 'original'
    generation = config._cache_generation
    async with sessions() as session:
        await config.Config.upsert({'test.value': 'updated'}, db=session)
        async with session.begin_nested():
            pass
        assert config._cache_generation == generation
        await session.commit()
    assert config._cache_generation > generation
    assert await config.Config.get('test.value') == 'updated'


@pytest.mark.asyncio
async def test_expired_snapshot_refills_and_deleted_keys_disappear(sessions, monkeypatch):
    await config.Config.upsert({'test.value': 'original'})
    assert await config.Config.get('test.value') == 'original'
    generation, _, values = config._snapshot
    monkeypatch.setattr(config, '_snapshot', (generation, 0, values))
    assert await config.Config.get('test.value') == 'original'
    assert config._snapshot[1] > 0
    await config.Config.delete('test.value')
    assert await config.Config.get('test.value', 'fallback') == 'fallback'
