"""Retrieval embeds each query once per request, clipped to what the embedding model accepts."""

import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tiktoken
from open_webui.retrieval import utils as retrieval
from open_webui.retrieval.vector.main import SearchResult

USER = SimpleNamespace(id='user-1', role='user')


def tokens(text):
    return len(tiktoken.get_encoding('cl100k_base').encode(text, disallowed_special=()))


def long_prompt(words):
    return (
        'QUESTION-START ' + ' '.join(f'line{index} of pasted report text' for index in range(words)) + ' QUESTION-END'
    )


def test_short_queries_are_unchanged():
    query = 'What is the casing depth for well 12?'
    assert retrieval.clip_query_for_embedding(query) is query
    near_limit = long_prompt(900)
    assert tokens(near_limit) < retrieval.QUERY_EMBEDDING_MAX_TOKENS
    assert retrieval.clip_query_for_embedding(near_limit) == near_limit


@pytest.mark.parametrize('words', [3000, 400_000])
def test_long_queries_keep_their_start_and_end_within_the_limit(words):
    query = long_prompt(words)
    assert len(query) > 10 * 8192
    clipped = retrieval.clip_query_for_embedding(query)
    # Comfortably under the 8,192-token input limit of text-embedding-3 models, and most of the budget is used.
    assert 7900 <= tokens(clipped) <= 8100
    assert clipped.startswith('QUESTION-START') and clipped.endswith('QUESTION-END')


def test_character_bound_when_the_tokenizer_is_unavailable(monkeypatch):
    monkeypatch.setattr(retrieval.tiktoken, 'get_encoding', lambda name: (_ for _ in ()).throw(OSError('offline')))
    clipped = retrieval.clip_query_for_embedding('界' * 100_000)
    # Every token covers at least one UTF-8 byte, so this cannot exceed the token limit.
    assert len(clipped.encode()) <= retrieval.QUERY_EMBEDDING_MAX_TOKENS


@pytest.mark.asyncio
async def test_concurrent_and_repeated_queries_share_one_embedding_call():
    calls = []

    async def embed(texts, prefix=None):
        calls.append(list(texts))
        await asyncio.sleep(0.01)
        return [[float(len(text))] for text in texts]

    cached = retrieval.cache_query_embeddings(embed)
    assert retrieval.cache_query_embeddings(cached) is cached
    first, second, third = await asyncio.gather(cached(['a', 'bb']), cached('a'), cached(['bb', 'a', 'bb'], None))
    assert calls == [['a', 'bb']]
    assert first == [[1.0], [2.0]] and second == [1.0] and third == [[2.0], [1.0], [2.0]]
    # A different prefix is a different embedding.
    assert await cached('a', 'query: ') == [1.0]
    assert calls[-1] == ['a']


@pytest.mark.asyncio
async def test_a_failed_embedding_is_reused_and_a_cancelled_waiter_does_not_cancel_others():
    calls = []
    release = asyncio.Event()

    async def embed(texts, prefix=None):
        calls.append(list(texts))
        await release.wait()
        raise RuntimeError("400, message='Bad Request'")

    cached = retrieval.cache_query_embeddings(embed)
    waiting = asyncio.ensure_future(cached('question'))
    other = asyncio.ensure_future(cached('question'))
    await asyncio.sleep(0)
    waiting.cancel()
    release.set()
    with pytest.raises(RuntimeError):
        await other
    with pytest.raises(RuntimeError):
        await cached(['question'])
    assert calls == [['question']]


@pytest.fixture
def items(monkeypatch):
    config = {
        'rag.bypass_embedding_and_retrieval': False,
        'rag.full_context_char_threshold': 0,
        'rag.enable_hybrid_search': False,
        'rag.embedding_query_prefix': None,
    }

    async def get_many(*keys):
        return {key: config.get(key) for key in keys}

    searched = []

    def query_doc(collection_name, query_embedding, k, user=None):
        searched.append((collection_name, query_embedding))
        return SearchResult(
            ids=[[collection_name]],
            documents=[[f'chunk from {collection_name}']],
            metadatas=[[{'file_id': collection_name.removeprefix('file-'), 'name': collection_name}]],
            distances=[[0.5]],
        )

    async def get_file(file_id):
        return SimpleNamespace(id=file_id, user_id=USER.id, data={'content': 'report text'}, filename=f'{file_id}.txt')

    monkeypatch.setattr(retrieval.Config, 'get_many', get_many)
    monkeypatch.setattr(retrieval.Files, 'get_file_by_id', get_file)
    monkeypatch.setattr(retrieval.Files, 'get_file_metadatas_by_ids', AsyncMock(return_value=[]))
    monkeypatch.setattr(retrieval, 'filter_accessible_collections', AsyncMock(side_effect=lambda names, user: names))
    monkeypatch.setattr(retrieval, 'query_doc', query_doc)
    return SimpleNamespace(files=[{'type': 'file', 'id': f'f{index}'} for index in range(6)], searched=searched)


async def sources_for(items, queries, embed):
    return await retrieval.get_sources_from_items(
        request=None,
        items=[dict(item) for item in items.files],
        queries=queries,
        embedding_function=lambda query, prefix: embed(query, prefix),
        k=5,
        reranking_function=None,
        k_reranker=5,
        r=0.0,
        hybrid_bm25_weight=0.5,
        hybrid_search=False,
        user=USER,
    )


@pytest.mark.asyncio
async def test_each_attached_file_reuses_the_same_query_embedding(items):
    embed = AsyncMock(side_effect=lambda texts, prefix: [[0.1, 0.2] for _ in texts])
    sources = await sources_for(items, ['casing depth'], embed)
    assert embed.await_count == 1
    assert [collection for collection, _ in items.searched] == [f'file-f{index}' for index in range(6)]
    assert all(vector == [0.1, 0.2] for _, vector in items.searched)
    assert len(sources) == 6


@pytest.mark.asyncio
async def test_a_long_prompt_is_clipped_before_it_is_embedded(items):
    seen = []

    async def embed(texts, prefix):
        seen.extend(texts)
        return [[0.3] for _ in texts]

    sources = await sources_for(items, [long_prompt(5000)], embed)
    assert len(seen) == 1 and tokens(seen[0]) <= 8100
    assert len(sources) == 6


@pytest.mark.asyncio
async def test_a_failing_query_embedding_is_called_and_logged_once(items, caplog):
    embed = AsyncMock(side_effect=RuntimeError("400, message='Bad Request', url='/proxy/v1/embeddings'"))
    with caplog.at_level(logging.ERROR, logger=retrieval.log.name):
        sources = await sources_for(items, ['casing depth'], embed)
    assert sources == []
    assert embed.await_count == 1
    assert items.searched == []
    assert len([record for record in caplog.records if record.levelno >= logging.ERROR]) == 1


@pytest.mark.asyncio
async def test_a_long_query_is_tokenized_once_for_all_items_and_off_the_event_loop(items, monkeypatch):
    clip = retrieval.clip_query_for_embedding
    threads = []

    def counted_clip(query, *args):
        threads.append(threading.get_ident())
        return clip(query, *args)

    monkeypatch.setattr(retrieval, 'clip_query_for_embedding', counted_clip)
    items.files = [{'type': 'file', 'id': f'f{index}'} for index in range(200)]
    embed = AsyncMock(side_effect=lambda texts, prefix: [[0.1] for _ in texts])
    sources = await sources_for(items, [long_prompt(50_000), 'casing depth'], embed)
    # One tokenization in a worker thread for the long query; the short one is never tokenized.
    assert len(threads) == 1 and threads[0] != threading.get_ident()
    assert embed.await_count == 1
    assert len(sources) == 200
