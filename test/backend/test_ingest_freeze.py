"""Ingestion must not freeze the serving process (fork fix, 2026-11-25). Synthetic data only.

- LinearMarkdownHeaderTextSplitter returns exactly what langchain's MarkdownHeaderTextSplitter returns.
- Chunks split without add_start_index equal the old chunks minus the start_index key.
- Large inputs split in bounded time and without long GIL holds.
- Token counting and token splitting give the same results without a Python list of every token.
- The process-status endpoint never loads data.content; hash and meta updates never load the row.

Set FILE_STATUS_TEST_DATABASE_URL to a disposable PostgreSQL database to also run the status query there.
"""

import importlib.metadata
import json
import os
import random
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter, TokenTextSplitter
from open_webui.retrieval import markdown_splitter
from open_webui.retrieval.markdown_splitter import LinearMarkdownHeaderTextSplitter, iter_lines, keep_printable
from open_webui.retrieval.token_splitter import NumpyTokenTextSplitter, count_tokens

HEADERS = [
    ('#', 'Header 1'),
    ('##', 'Header 2'),
    ('###', 'Header 3'),
    ('####', 'Header 4'),
    ('#####', 'Header 5'),
    ('######', 'Header 6'),
]
ALPHABET = list('abZ1  #`~*') + ['é', '中', '😀', '́', ' ', '​', ' ', '﻿']
ALPHABET += ['\x00', '\x07', '\x0b', '\x1b', '\x7f', '\t', '\r']
FIXED_TEXTS = [
    '',
    '\n',
    '\n\n\n',
    '#',
    '# a',
    '#no-space\n####### seven',
    '# a\n## b\ntext\n\n### c\nx\n\n## d\n\n# e',
    '```\n# not a header\n\n```\n# header',
    '``` inline ``` text\n# h',
    '~~~\n```\n~~~\n## h\nbody',
    '```\nunclosed\n\n',
    '\x00\x07\n​\nbody',
    'x' * 5000 + '\x00' + 'y' * 5000 + '\n## after',
    '# a\r\n\r\nbody\r\n## b\r\nmore\r\n',
    '  ### indented\n\t# tab',
]


def random_words(rng, n=None):
    n = rng.randint(0, 30) if n is None else n
    return ''.join(rng.choice(ALPHABET) for _ in range(n))


def random_line(rng):
    kind = rng.random()
    if kind < 0.2:
        return rng.choice(['', ' ', '  ']) + '#' * rng.randint(1, 7) + rng.choice([' ', '', 'x', ' \t']) + random_words(rng)
    if kind < 0.3:
        return rng.choice(['```', '```python', '``` x ```', '```a```', '~~~', '~~~js', ' ```', '````', '**B**', '***C***'])
    if kind < 0.45:
        return rng.choice(['', ' ', '\t', '\r', '\x0b', '\x00', '​'])
    if kind < 0.5:
        return random_words(rng, rng.randint(50, 400))
    return random_words(rng)


def random_markdown(rng):
    newline = rng.choice(['\n', '\n', '\r\n'])
    text = newline.join(random_line(rng) for _ in range(rng.randint(0, 60)))
    return text + newline if rng.random() < 0.3 else text


def outcome(splitter, text):
    try:
        return [(doc.page_content, doc.metadata) for doc in splitter.split_text(text)]
    except Exception as e:  # identical failures count as identical output
        return type(e)


def without_start_index(docs):
    return [(doc.page_content, {k: v for k, v in doc.metadata.items() if k != 'start_index'}) for doc in docs]


def one_line_json(chars):
    rng = random.Random(1)
    records, size = [], 0
    while size < chars:
        record = json.dumps({'id': rng.randint(1, 10**9), 'note': random_words(rng, 40), 'ok': rng.random() > 0.5})
        records.append(record)
        size += len(record) + 1
    return '[' + ','.join(records) + ']'


def test_copied_split_text_matches_installed_langchain():
    # LinearMarkdownHeaderTextSplitter.split_text copies 1.1.2; re-diff it before moving this pin.
    assert importlib.metadata.version('langchain-text-splitters') == '1.1.2'


@pytest.mark.parametrize('step', [1, 2, 5, 64])
def test_line_and_printable_helpers_match_builtins(monkeypatch, step):
    monkeypatch.setattr(markdown_splitter, 'STEP_CHARS', step)
    rng = random.Random(step)
    for _ in range(500):
        text = ''.join(rng.choice(ALPHABET + ['\n', '\n']) for _ in range(rng.randint(0, 80)))
        assert list(iter_lines(text)) == text.split('\n')
        assert keep_printable(text) == ''.join(filter(str.isprintable, text))


@pytest.mark.parametrize('step', [3, 17, markdown_splitter.STEP_CHARS])
@pytest.mark.parametrize(
    'settings',
    [
        {'strip_headers': False},  # what save_docs_to_vector_db uses
        {'strip_headers': True},
        {'strip_headers': False, 'return_each_line': True},
        {'strip_headers': False, 'custom_header_patterns': {'**': 1, '***': 2}},
    ],
)
def test_markdown_splitter_matches_langchain(monkeypatch, step, settings):
    monkeypatch.setattr(markdown_splitter, 'STEP_CHARS', step)
    headers = HEADERS + ([('**', 'Bold 1'), ('***', 'Bold 2')] if 'custom_header_patterns' in settings else [])
    upstream = MarkdownHeaderTextSplitter(headers_to_split_on=headers, **settings)
    linear = LinearMarkdownHeaderTextSplitter(headers_to_split_on=headers, **settings)
    rng = random.Random(f'{step}-{sorted(settings)}')
    for text in FIXED_TEXTS + [random_markdown(rng) for _ in range(400)]:
        assert outcome(linear, text) == outcome(upstream, text), repr(text[:300])


@pytest.mark.parametrize(
    'make',
    [
        lambda flag: TokenTextSplitter(
            encoding_name='cl100k_base', chunk_size=60, chunk_overlap=12, add_start_index=flag, disallowed_special=()
        ),
        lambda flag: RecursiveCharacterTextSplitter(chunk_size=120, chunk_overlap=30, add_start_index=flag),
    ],
    ids=['token', 'character'],
)
def test_chunks_without_start_index_equal_old_chunks(make):
    rng = random.Random(7)
    docs = [
        Document(page_content=random_markdown(rng) * 3, metadata={'name': f'f{i}.md', 'file_id': f'f{i}', 'source': 's'})
        for i in range(30)
    ]
    docs.append(Document(page_content=one_line_json(20_000), metadata={'name': 'x.json', 'source': 'x.json'}))
    old = make(True).split_documents(docs)
    new = make(False).split_documents(docs)
    assert old and all('start_index' in doc.metadata for doc in old)
    assert all('start_index' not in doc.metadata for doc in new)
    assert without_start_index(new) == without_start_index(old)


def test_markdown_split_is_linear_in_blocks():
    # About 2M chars in 200k blank-line blocks and no headers: upstream's += takes about 20 s here
    # (quadratic), this splitter about 1.5 s (one Python step per line, as upstream).
    text = '\n\n'.join(f'p{i}'.ljust(8, 'x') for i in range(200_000))
    start = time.monotonic()
    chunks = LinearMarkdownHeaderTextSplitter(headers_to_split_on=HEADERS, strip_headers=False).split_text(text)
    assert time.monotonic() - start < 6.0
    assert [chunk.page_content for chunk in chunks] == [text.replace('\n\n', '  \n')]


def test_markdown_split_of_one_long_line_releases_the_gil():
    # 20M chars on one line with non-printables: upstream filters it in a single C call.
    text = ('x' * 999_999 + '\x00') * 20
    done = threading.Event()
    result = {}

    def split():
        result['chunks'] = LinearMarkdownHeaderTextSplitter(headers_to_split_on=HEADERS, strip_headers=False).split_text(text)
        done.set()

    worker = threading.Thread(target=split)
    worst_gap, last = 0.0, time.monotonic()
    worker.start()
    while not done.is_set():
        time.sleep(0.01)
        now = time.monotonic()
        worst_gap, last = max(worst_gap, now - last), now
    worker.join()
    assert worst_gap < 0.5
    assert [chunk.page_content for chunk in result['chunks']] == [text.replace('\x00', '')]


def prod_like_config(**overrides):
    return {
        'rag.enable_markdown_header_text_splitter': True,
        'rag.text_splitter': 'token',
        'rag.tiktoken_encoding_name': 'cl100k_base',
        'rag.chunk_size': 1400,
        'rag.chunk_overlap': 200,
        'rag.chunk_min_size_target': 700,
        'rag.embedding_engine': 'openai',
        'rag.embedding_model': 'text-embedding-3-small',
        **overrides,
    }


@pytest.fixture
def split_only(monkeypatch):
    """Run save_docs_to_vector_db up to the vector store and record the chunks it would embed."""
    from open_webui.routers import retrieval

    record = {'chunks': [], 'kwargs': [], 'split_text': 0.0, 'split_documents': 0.0}

    def recording(base):
        class Recording(base):
            def __init__(self, **kwargs):
                record['kwargs'].append(kwargs)
                super().__init__(**kwargs)

            def split_text(self, text):
                start = time.monotonic()
                chunks = super().split_text(text)
                record['split_text'] += time.monotonic() - start
                return chunks

            def split_documents(self, documents):
                start = time.monotonic()
                chunks = super().split_documents(documents)
                record['split_documents'] += time.monotonic() - start
                record['chunks'].append(chunks)
                return chunks

        return Recording

    RecordingTokenTextSplitter = recording(NumpyTokenTextSplitter)

    monkeypatch.setattr(retrieval, 'NumpyTokenTextSplitter', RecordingTokenTextSplitter)
    # An existing collection with add=False returns before any embedding call.
    monkeypatch.setattr(retrieval, 'get_vector_db_client', lambda: SimpleNamespace(has_collection=lambda **_: True))
    return SimpleNamespace(retrieval=retrieval, record=record, recording=recording)


def test_save_docs_chunks_match_the_upstream_pipeline(monkeypatch, split_only):
    retrieval, record = split_only.retrieval, split_only.record
    rng = random.Random(11)
    text = '\n\n'.join(random_markdown(rng) for _ in range(300)) + '\n\n' + one_line_json(30_000)
    docs = [Document(page_content=text, metadata={'name': 'n.md', 'file_id': 'f', 'source': 'n.md'})]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    assert retrieval.save_docs_to_vector_db(request, docs, 'c', prod_like_config()) is True
    new = record['chunks'].pop()
    assert record['kwargs'][-1]['add_start_index'] is False

    # The pre-fix pipeline: langchain's markdown and token splitters, list-based token counting,
    # and add_start_index=True.
    class OldTokenTextSplitter(split_only.recording(TokenTextSplitter)):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, 'add_start_index': True})

    def old_length_function(request, config):
        import tiktoken

        encoding = tiktoken.get_encoding(str(config['rag.tiktoken_encoding_name']))
        return lambda text: len(encoding.encode(text, disallowed_special=retrieval.TIKTOKEN_DISALLOWED_SPECIAL))

    monkeypatch.setattr(retrieval, 'LinearMarkdownHeaderTextSplitter', MarkdownHeaderTextSplitter)
    monkeypatch.setattr(retrieval, 'NumpyTokenTextSplitter', OldTokenTextSplitter)
    monkeypatch.setattr(retrieval, 'get_splitter_length_function', old_length_function)
    assert retrieval.save_docs_to_vector_db(request, docs, 'c', prod_like_config()) is True
    old = record['chunks'].pop()

    assert len(new) > 10 and all('start_index' in doc.metadata for doc in old)
    assert all('start_index' not in doc.metadata for doc in new)
    assert without_start_index(new) == without_start_index(old)


def test_save_docs_token_split_skips_the_start_index_search(split_only):
    retrieval, record = split_only.retrieval, split_only.record
    docs = [Document(page_content=one_line_json(20_000_000), metadata={'name': 'x.json', 'source': 'x.json'})]
    config = prod_like_config(**{'rag.enable_markdown_header_text_splitter': False})

    assert retrieval.save_docs_to_vector_db(SimpleNamespace(), docs, 'c', config) is True
    assert all('start_index' not in doc.metadata for doc in record['chunks'][-1])
    # With add_start_index=True, create_documents adds about 13 s of str.find on top of split_text here.
    assert record['split_documents'] - record['split_text'] < 1.0


def status(file_id='f', user_id='owner', state=None, error=None):
    from open_webui.models.files import FileStatusModel

    return FileStatusModel(id=file_id, user_id=user_id, status=state, error=error)


@pytest.fixture
def status_router(monkeypatch):
    from open_webui.routers import files as file_router

    files = SimpleNamespace(
        get_file_by_id=AsyncMock(side_effect=AssertionError('the status endpoint must not load the file row')),
        get_file_status_by_id=AsyncMock(),
    )
    access = AsyncMock(return_value=False)
    monkeypatch.setattr(file_router, 'Files', files)
    monkeypatch.setattr(file_router, 'has_access_to_file', access)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(file_router.asyncio, 'sleep', no_sleep)
    return SimpleNamespace(router=file_router, files=files, access=access)


@pytest.mark.asyncio
async def test_status_stream_reads_only_status_fields(status_router):
    status_router.files.get_file_status_by_id.side_effect = [
        status(state='pending'),  # the endpoint's own lookup before streaming (owner check)
        status(state='pending'),
        status(state='pending'),
        status(state='processing'),
        status(state='failed'),
        status(state='failed', error='boom'),  # the one read that includes the error
    ]
    owner = SimpleNamespace(id='owner', role='user')

    response = await status_router.router.get_file_process_status('f', stream=True, user=owner)
    events = [json.loads(chunk.removeprefix('data: ')) async for chunk in response.body_iterator]

    assert events == [
        {'status': 'pending'},
        {'status': 'pending'},
        {'status': 'processing'},
        {'status': 'failed', 'error': 'boom'},
    ]
    status_router.files.get_file_by_id.assert_not_called()
    status_router.access.assert_not_called()
    calls = status_router.files.get_file_status_by_id.await_args_list
    assert [call.kwargs.get('include_error', False) for call in calls] == [False] * 5 + [True]


@pytest.mark.asyncio
async def test_status_without_stream_and_access_checks(status_router):
    get_status = status_router.files.get_file_status_by_id
    get_status.side_effect = None
    get_status.return_value = status(state=None)
    owner = SimpleNamespace(id='owner', role='user')
    admin = SimpleNamespace(id='admin', role='admin')
    other = SimpleNamespace(id='other', role='user')

    assert await status_router.router.get_file_process_status('f', stream=False, user=owner) == {'status': 'pending'}
    get_status.return_value = status(state='completed')
    assert await status_router.router.get_file_process_status('f', stream=False, user=admin) == {'status': 'completed'}
    status_router.access.assert_not_called()

    with pytest.raises(HTTPException) as denied:
        await status_router.router.get_file_process_status('f', stream=False, user=other)
    assert denied.value.status_code == 404
    status_router.access.assert_awaited_once_with('f', 'read', other)

    status_router.access.return_value = True
    assert await status_router.router.get_file_process_status('f', stream=False, user=other) == {'status': 'completed'}

    get_status.return_value = None
    with pytest.raises(HTTPException) as missing:
        await status_router.router.get_file_process_status('f', stream=False, user=owner)
    assert missing.value.status_code == 404
    status_router.files.get_file_by_id.assert_not_called()


DATABASES = ['sqlite']
if os.getenv('FILE_STATUS_TEST_DATABASE_URL'):
    DATABASES.append(os.environ['FILE_STATUS_TEST_DATABASE_URL'])


@pytest.mark.asyncio
@pytest.mark.parametrize('url', DATABASES, ids=lambda url: url.split(':', 1)[0])
async def test_status_query_does_not_load_data_content(monkeypatch, tmp_path, url):
    from open_webui.internal import db as internal_db
    from open_webui.models.files import File, Files
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    monkeypatch.setattr(internal_db, 'DATABASE_ENABLE_SESSION_SHARING', True)
    decoded = []

    def counting_loads(value):
        decoded.append(len(value))
        return json.loads(value)

    schema = f'file_status_{uuid.uuid4().hex[:10]}'
    postgres = url.startswith('postgresql')
    if not postgres:
        url = f'sqlite+aiosqlite:///{tmp_path}/status.db'
    connect_args = {'options': f'-csearch_path={schema}'} if postgres else {}
    if postgres:
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_async_engine(url, json_deserializer=counting_loads, connect_args=connect_args)
    statements = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda _c, _cur, statement, *_: statements.append(statement))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(File.__table__.create)
        async with AsyncSession(engine) as db:
            data = {'content': 'C' * 2_000_000, 'status': 'failed', 'error': 'boom', 'custom': 1}
            db.add(File(id='f', user_id='owner', filename='x.txt', data=data, meta={}, created_at=0, updated_at=0))
            await db.commit()

        async with AsyncSession(engine) as db:
            statements.clear()
            decoded.clear()
            found = await Files.get_file_status_by_id('f', db=db)
            with_error = await Files.get_file_status_by_id('f', include_error=True, db=db)
            missing = await Files.get_file_status_by_id('nope', db=db)
        assert (found.id, found.user_id, found.status, found.error) == ('f', 'owner', 'failed', None)
        assert (with_error.status, with_error.error) == ('failed', 'boom')
        assert missing is None
        assert decoded == []  # the JSON data column was never transferred and decoded
        selects = [statement for statement in statements if 'FROM file' in statement]
        assert [statement.count('file.data') for statement in selects] == [1, 2, 1]
        assert ('file.data ->>' if postgres else 'JSON_EXTRACT(file.data') in selects[0]

        async with AsyncSession(engine) as db:
            full = await Files.get_file_by_id('f', db=db)
        assert full.data['content'] and decoded  # control: the full read does decode data
    finally:
        await engine.dispose()
        if postgres:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            await admin.dispose()


# --- token counting and token splitting without a Python list of every token ---

TOKEN_TEXTS = FIXED_TEXTS + [
    'plain words ' * 500,
    one_line_json(50_000),
    'emoji 😀😀 and 中文 and combining é' * 300,
    'special <|endoftext|> and <|fim_prefix|> markers ' * 50,
]


@pytest.mark.parametrize('chunk_size,chunk_overlap', [(1400, 200), (60, 12), (5, 4), (1, 0)])
def test_numpy_token_splitter_matches_langchain(chunk_size, chunk_overlap):
    kwargs = dict(encoding_name='cl100k_base', chunk_size=chunk_size, chunk_overlap=chunk_overlap, disallowed_special=())
    upstream = TokenTextSplitter(**kwargs)
    numpy_splitter = NumpyTokenTextSplitter(**kwargs)
    rng = random.Random(f'tok-{chunk_size}')
    for text in TOKEN_TEXTS + [random_markdown(rng) for _ in range(150)]:
        assert numpy_splitter.split_text(text) == upstream.split_text(text), repr(text[:200])
    docs = [Document(page_content=text, metadata={'i': i}) for i, text in enumerate(TOKEN_TEXTS)]
    assert without_start_index(numpy_splitter.split_documents(docs)) == without_start_index(upstream.split_documents(docs))


def test_numpy_token_splitter_keeps_langchain_errors():
    special = 'a <|endoftext|> b'
    for kwargs in ({}, {'disallowed_special': 'all'}):
        with pytest.raises(ValueError):
            TokenTextSplitter(encoding_name='cl100k_base', **kwargs).split_text(special)
        with pytest.raises(ValueError):
            NumpyTokenTextSplitter(encoding_name='cl100k_base', **kwargs).split_text(special)
    allowed = dict(encoding_name='cl100k_base', allowed_special='all', chunk_size=3, chunk_overlap=1)
    assert NumpyTokenTextSplitter(**allowed).split_text(special) == TokenTextSplitter(**allowed).split_text(special)
    with pytest.raises(ValueError):
        NumpyTokenTextSplitter(encoding_name='cl100k_base', chunk_size=5, chunk_overlap=5).split_text('x y z')


def test_count_tokens_matches_encode():
    import tiktoken

    encoding = tiktoken.get_encoding('cl100k_base')
    rng = random.Random(3)
    for text in TOKEN_TEXTS + [random_markdown(rng) for _ in range(100)]:
        assert count_tokens(encoding, text, disallowed_special=()) == len(encoding.encode(text, disallowed_special=()))


def test_token_split_of_a_large_text_releases_the_gil():
    # 30M chars of one-line JSON: encode() would hold the GIL for about 0.5 s building the token
    # list and again freeing it; the numpy path keeps every hold short.
    import tiktoken

    text = one_line_json(30_000_000)
    encoding = tiktoken.get_encoding('cl100k_base')
    done = threading.Event()
    result = {}

    def work():
        result['count'] = count_tokens(encoding, text, disallowed_special=())
        result['chunks'] = NumpyTokenTextSplitter(
            encoding_name='cl100k_base', chunk_size=1400, chunk_overlap=200, disallowed_special=()
        ).split_text(text)
        done.set()

    worker = threading.Thread(target=work)
    worst_gap, last = 0.0, time.monotonic()
    worker.start()
    while not done.is_set():
        time.sleep(0.01)
        now = time.monotonic()
        worst_gap, last = max(worst_gap, now - last), now
    worker.join()
    assert worst_gap < 0.5
    assert result['count'] > 1_000_000 and len(result['chunks']) > 900


# --- hash and meta updates do not load the row ---


@pytest.mark.asyncio
@pytest.mark.parametrize('url', DATABASES, ids=lambda url: url.split(':', 1)[0])
async def test_hash_and_meta_updates_do_not_load_data(monkeypatch, tmp_path, url):
    from open_webui.internal import db as internal_db
    from open_webui.models.files import File, Files
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    monkeypatch.setattr(internal_db, 'DATABASE_ENABLE_SESSION_SHARING', True)
    decoded = []

    def counting_loads(value):
        decoded.append(len(value))
        return json.loads(value)

    schema = f'file_upd_{uuid.uuid4().hex[:10]}'
    postgres = url.startswith('postgresql')
    if not postgres:
        url = f'sqlite+aiosqlite:///{tmp_path}/upd.db'
    connect_args = {'options': f'-csearch_path={schema}'} if postgres else {}
    if postgres:
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_async_engine(url, json_deserializer=counting_loads, connect_args=connect_args)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(File.__table__.create)
        async with AsyncSession(engine) as db:
            db.add(
                File(
                    id='f',
                    user_id='owner',
                    filename='x.txt',
                    data={'content': 'C' * 2_000_000, 'status': 'completed'},
                    meta={'name': 'x.txt', 'size': 1},
                    created_at=0,
                    updated_at=0,
                )
            )
            await db.commit()

        async with AsyncSession(engine) as db:
            decoded.clear()
            assert await Files.update_file_hash_by_id('f', 'abc', db=db) is True
            assert await Files.update_file_metadata_by_id('f', {'collection_name': 'file-f'}, db=db) is True
            assert await Files.update_file_hash_by_id('nope', 'abc', db=db) is False
            assert await Files.update_file_metadata_by_id('nope', {'a': 1}, db=db) is False
        # Only the small meta column was read back, never the data column.
        assert decoded and max(decoded) < 1_000

        async with AsyncSession(engine) as db:
            full = await Files.get_file_by_id('f', db=db)
        assert full.hash == 'abc' and full.updated_at > 0
        assert full.meta == {'name': 'x.txt', 'size': 1, 'collection_name': 'file-f'}
        assert full.data == {'content': 'C' * 2_000_000, 'status': 'completed'}
    finally:
        await engine.dispose()
        if postgres:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(len(DATABASES) < 2, reason='needs FILE_STATUS_TEST_DATABASE_URL (PostgreSQL)')
async def test_status_falls_back_when_postgres_cannot_read_the_json(monkeypatch):
    # PostgreSQL's ->> rejects a json value holding a \u0000 escape anywhere; the status must stay readable.
    from open_webui.internal import db as internal_db
    from open_webui.models.files import File, Files
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    monkeypatch.setattr(internal_db, 'DATABASE_ENABLE_SESSION_SHARING', True)
    url = DATABASES[1]
    schema = f'file_nul_{uuid.uuid4().hex[:10]}'
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_async_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(File.__table__.create)
        async with AsyncSession(engine) as db:
            data = {'content': 'a\x00b', 'status': 'failed', 'error': 'boom'}
            db.add(File(id='f', user_id='owner', filename='x.txt', data=data, meta={}, created_at=0, updated_at=0))
            await db.commit()
        async with AsyncSession(engine) as db:
            found = await Files.get_file_status_by_id('f', db=db)
            with_error = await Files.get_file_status_by_id('f', include_error=True, db=db)
        assert (found.user_id, found.status, found.error) == ('owner', 'failed', None)
        assert (with_error.status, with_error.error) == ('failed', 'boom')
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        await admin.dispose()
