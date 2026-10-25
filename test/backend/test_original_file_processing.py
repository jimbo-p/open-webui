"""Original-only uploads retain their blobs without entering extraction or embedding."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from open_webui.models.files import FileModel
from open_webui.routers import files as file_router
from open_webui.routers import retrieval


def stored_file(file_id, filename, content_type='application/octet-stream', user_id='owner'):
    return FileModel(
        id=file_id,
        user_id=user_id,
        filename=filename,
        path=f'/originals/{file_id}',
        hash=f'original-hash-{file_id}',
        data={'status': 'processing', 'content': 'previous content', 'custom': 'preserve'},
        meta={'content_type': content_type},
        created_at=0,
        updated_at=0,
    )


@pytest.fixture
def processing(monkeypatch):
    records = {}
    operations = []
    db = SimpleNamespace(commit=AsyncMock())

    async def update_data(file_id, data, **kwargs):
        operations.append(('data', file_id, dict(data)))
        records[file_id].data = {**records[file_id].data, **data}
        return records[file_id]

    async def update_hash(file_id, value, **kwargs):
        records[file_id].hash = value
        return records[file_id]

    async def update_file(id, form_data, **kwargs):
        records[id].hash = form_data.hash
        records[id].data = {**records[id].data, **form_data.data}
        return records[id]

    files = SimpleNamespace(
        get_file_by_id=AsyncMock(side_effect=lambda file_id, **kwargs: records.get(file_id)),
        update_file_data_by_id=AsyncMock(side_effect=update_data),
        update_file_hash_by_id=AsyncMock(side_effect=update_hash),
        update_file_by_id=AsyncMock(side_effect=update_file),
    )
    config = SimpleNamespace(
        get_many=AsyncMock(return_value={}),
        get=AsyncMock(side_effect=lambda key, *args: [] if key == 'audio.stt.supported_content_types' else None),
    )
    for module in (retrieval, file_router):
        monkeypatch.setattr(module, 'Files', files)
        monkeypatch.setattr(module, 'Config', config)

    @asynccontextmanager
    async def database():
        yield db

    monkeypatch.setattr(retrieval, 'get_async_db', database)
    monkeypatch.setattr(retrieval, 'has_access_to_file', AsyncMock(return_value=False))
    access = AsyncMock()
    monkeypatch.setattr(retrieval, '_validate_collection_access', access)
    loader = Mock(side_effect=AssertionError('Excluded files must not reach a loader'))
    monkeypatch.setattr(retrieval, 'build_loader_from_config', loader)
    monkeypatch.setattr(
        retrieval,
        'Storage',
        SimpleNamespace(get_file=Mock(side_effect=AssertionError('Original blob must not be opened'))),
    )
    vector = SimpleNamespace(
        query=AsyncMock(side_effect=AssertionError('Excluded files must not query vectors')),
        delete_collection=AsyncMock(side_effect=AssertionError('Excluded files must not delete vectors')),
    )
    monkeypatch.setattr(retrieval, 'ASYNC_VECTOR_DB_CLIENT', vector)
    save_docs = Mock(return_value=True)
    monkeypatch.setattr(retrieval, 'save_docs_to_vector_db', save_docs)
    monkeypatch.setattr(retrieval, 'has_duplicate_content', Mock(return_value=False))

    async def run_sync(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(retrieval, 'run_in_threadpool', run_sync)
    events = AsyncMock()
    monkeypatch.setattr(retrieval, 'publish_event', events)
    return SimpleNamespace(
        records=records,
        operations=operations,
        db=db,
        files=files,
        access=access,
        loader=loader,
        vector=vector,
        save_docs=save_docs,
        events=events,
        request=SimpleNamespace(),
        user=SimpleNamespace(id='owner', role='user'),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'filename,content_type,reason',
    [
        ('report.XLS', 'application/vnd.ms-excel', 'xls'),
        ('workbook.xlsx', 'application/octet-stream', 'xlsx'),
        ('analysis.dxp', 'application/octet-stream', 'dxp'),
        ('analysis', 'application/vnd.spotfire.dxp; charset=utf-8', 'application/vnd.spotfire.dxp'),
    ],
)
async def test_index_excluded_file_keeps_original_and_defers_completion(processing, filename, content_type, reason):
    p = processing
    file = p.records['file'] = stored_file('file', filename, content_type)
    result = await retrieval.index_file(
        p.request,
        retrieval.ProcessFileForm(file_id=file.id, content='Do not index this replacement', collection_name='kb'),
        p.user,
        p.db,
    )

    assert result['status'] is True and result['collection_name'] is None
    assert result['content'] == file.data['content']
    assert 'stored as the original blob (id file)' in file.data['content']
    assert file.data['skip_extraction_reason'] == reason
    assert file.data['skip_extraction'] is True
    assert file.data['status'] == 'processing'
    assert file.data['custom'] == 'preserve'
    assert file.path == '/originals/file' and file.hash == 'original-hash-file'
    p.access.assert_awaited_once_with(['kb'], p.user, access_type='write')
    p.loader.assert_not_called()
    p.save_docs.assert_not_called()
    p.vector.query.assert_not_awaited()
    p.vector.delete_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_processing_marks_excluded_file_completed(processing):
    p = processing
    file = p.records['file'] = stored_file('file', 'analysis.dxp')
    result = await retrieval.process_file(p.request, retrieval.ProcessFileForm(file_id=file.id), p.user, p.db)
    assert result['status'] is True
    assert file.data['status'] == 'completed' and file.data['error'] is None
    assert 'status' not in p.operations[0][2]
    assert p.operations[-1] == ('data', file.id, {'status': 'completed', 'error': None})
    p.save_docs.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('link_fails', [False, True])
async def test_upload_completion_waits_for_durable_knowledge_link(processing, monkeypatch, link_fails):
    p = processing
    file = p.records['file'] = stored_file('file', 'report.xls', 'application/vnd.ms-excel')

    async def link(**kwargs):
        assert file.data['status'] == 'processing'
        p.operations.append(('link', file.id, kwargs['knowledge_id']))
        if link_fails:
            raise RuntimeError('Knowledge link failed')
        return SimpleNamespace(id='link')

    knowledge = SimpleNamespace(
        get_knowledge_by_id=AsyncMock(return_value=SimpleNamespace(id='kb', user_id=p.user.id)),
        add_file_to_knowledge_by_id=AsyncMock(side_effect=link),
    )
    monkeypatch.setattr(file_router, 'Knowledges', knowledge)
    monkeypatch.setattr(file_router, '_cleanup_local_cache', Mock())
    await file_router.process_uploaded_file(
        p.request,
        SimpleNamespace(content_type='application/vnd.ms-excel'),
        '/temporary/upload',
        file,
        {'knowledge_id': 'kb'},
        p.user,
        db=p.db,
    )

    knowledge.add_file_to_knowledge_by_id.assert_awaited_once()
    assert p.operations[-2] == ('link', file.id, 'kb')
    assert file.data['status'] == ('failed' if link_fails else 'completed')
    if link_fails:
        assert 'Knowledge link failed' in file.data['error']
        assert not any(data.get('status') == 'completed' for kind, _, data in p.operations if kind == 'data')
    assert file.path == '/originals/file' and file.hash == 'original-hash-file'
    p.save_docs.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('include_searchable_file', [False, True])
async def test_batch_excludes_original_only_files_but_indexes_searchable_text(processing, include_searchable_file):
    p = processing
    for file in (stored_file('xls', 'report.xls'), stored_file('dxp', 'analysis.dxp')):
        p.records[file.id] = file
    if include_searchable_file:
        p.records['text'] = stored_file('text', 'report.txt', 'text/plain')
        p.records['text'].data['content'] = 'Searchable operational report'
    form = retrieval.BatchProcessFilesForm(files=list(p.records.values()), collection_name='kb')
    response = await retrieval.process_files_batch(p.request, form, p.user, p.db)

    assert response.errors == []
    assert {result.file_id: result.status for result in response.results} == {
        file_id: 'completed' for file_id in p.records
    }
    for file_id in ('xls', 'dxp'):
        assert p.records[file_id].data['skip_extraction'] is True
        assert p.records[file_id].data['status'] == 'completed'
        assert p.records[file_id].hash == f'original-hash-{file_id}'
    if include_searchable_file:
        p.save_docs.assert_called_once()
        docs = p.save_docs.call_args.args[1]
        assert len(docs) == 1 and docs[0].page_content == 'Searchable operational report'
        assert docs[0].metadata['file_id'] == 'text'
    else:
        p.save_docs.assert_not_called()
    p.loader.assert_not_called()


@pytest.mark.asyncio
async def test_excluded_file_does_not_bypass_ownership_checks(processing):
    p = processing
    file = p.records['file'] = stored_file('file', 'report.xls', user_id='someone-else')
    before = file.model_dump()
    with pytest.raises(HTTPException) as rejected:
        await retrieval.index_file(p.request, retrieval.ProcessFileForm(file_id=file.id), p.user, p.db)
    assert rejected.value.status_code == 404
    assert file.model_dump() == before
    p.files.update_file_data_by_id.assert_not_awaited()
    p.events.assert_not_awaited()

    response = await retrieval.process_files_batch(
        p.request,
        retrieval.BatchProcessFilesForm(files=[file], collection_name='kb'),
        p.user,
        p.db,
    )
    assert response.results == []
    assert len(response.errors) == 1 and 'not file owner' in response.errors[0].error
    assert file.model_dump() == before
    p.files.update_file_data_by_id.assert_not_awaited()
    p.save_docs.assert_not_called()
