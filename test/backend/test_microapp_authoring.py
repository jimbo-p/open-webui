import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from open_webui.utils import microapp_authoring as jobs

JOB_ID = '3cb40e22-d0be-4413-a831-2b094846ad34'
URL = 'https://tool.example.test/mcpo/micro-app-authoring'


def receipt(job_id=JOB_ID):
    return {'success': True, 'job_id': job_id, 'phase': 'queued'}, None


def status(phase, cursor, *, done=False, **extra):
    return {'success': True, 'phase': phase, 'cursor': cursor, 'done': done, **extra}, None


@pytest.fixture(autouse=True)
def immediate_poll(monkeypatch):
    monkeypatch.setattr(jobs, '_POLL_INTERVAL', 0)


@pytest.fixture
def connection():
    return {'info': {'id': jobs.SERVER_ID}, 'url': URL, 'auth_type': 'session'}


@pytest.fixture
def openapi():
    operations = (jobs.SUBMIT, jobs.STATUS, jobs.CANCEL)
    return {
        'id': jobs.SERVER_ID,
        'url': URL,
        'specs': [{'name': operation, 'parameters': {'type': 'object', 'properties': {}}} for operation in operations],
        'openapi': {
            'paths': {
                '/elsewhere': {'get': {'operationId': jobs.STATUS}},
                **{f'/{operation}': {'post': {'operationId': operation}} for operation in operations},
            },
        },
    }


def test_exact_path_wins_over_duplicate_operation(openapi, connection):
    result = jobs.resolve_route(openapi, connection, jobs.STATUS)
    assert result['openapi']['paths'] == {'/microapp_job_status': {'post': {'operationId': jobs.STATUS}}}
    assert '/elsewhere' in openapi['openapi']['paths']


@pytest.mark.parametrize(
    'changes',
    [
        {'info': {'id': 'another-server'}},
        {'auth_type': 'bearer'},
        {'url': 'https://other.example.test/mcpo/micro-app-authoring'},
    ],
)
def test_refuses_another_connection(openapi, connection, changes):
    assert jobs.resolve_route(openapi, {**connection, **changes}, jobs.SUBMIT) is None


@pytest.mark.parametrize(
    'url', [f'{URL}?destination=elsewhere', f'{URL}#elsewhere', '//tool.example.test/micro-app-authoring']
)
def test_refuses_base_urls_with_nondestination_components(openapi, connection, url):
    assert jobs.resolve_route({**openapi, 'url': url}, {**connection, 'url': url}, jobs.STATUS) is None


def test_refuses_unrecognized_and_noncanonical_operations(openapi, connection):
    assert jobs.resolve_route(openapi, connection, 'elsewhere') is None
    del openapi['openapi']['paths']['/microapp_job_status']
    assert jobs.resolve_route(openapi, connection, jobs.STATUS) is None


def test_request_identity_is_stable_and_scoped_to_user_turn_and_arguments():
    metadata = {'chat_id': 'test-chat', 'message_id': 'test-message'}
    request_id = jobs.request_id('user', metadata, {'prompt': 'edit', 'slug': 'app'})
    assert request_id == jobs.request_id('user', metadata, {'slug': 'app', 'prompt': 'edit'})
    assert request_id != jobs.request_id('user', metadata, {'slug': 'other', 'prompt': 'edit'})
    assert request_id != jobs.request_id('another-user', metadata, {'slug': 'app', 'prompt': 'edit'})
    assert request_id != jobs.request_id('user', {**metadata, 'message_id': 'next'}, {'slug': 'app', 'prompt': 'edit'})
    assert jobs.request_id('user', {}, {}) != jobs.request_id('user', {}, {})


@pytest.mark.asyncio
async def test_tracks_phase_and_returns_terminal_result_using_the_same_job():
    server = AsyncMock(
        side_effect=[
            receipt(),
            status('editing', 5, details='confidential data'),
            status('editing', 5),
            status('authentication_required', 6),
            status('checking', 7),
            status('publishing', 8),
            status('succeeded', 9, done=True, result={'published': True, 'job_id': 'different'}),
        ]
    )
    emit = AsyncMock()
    result, _ = await jobs.track_job(server, {'prompt': 'requested change'}, emit)

    assert server.await_args_list == [
        call(jobs.SUBMIT, {'prompt': 'requested change'}),
        *[call(jobs.STATUS, {'job_id': JOB_ID, 'cursor': cursor}) for cursor in (0, 5, 5, 6, 7, 8)],
    ]
    assert result == {'success': True, 'job_id': JOB_ID, 'phase': 'succeeded', 'published': True}
    assert [event.args[0] for event in emit.await_args_list] == [
        {'type': 'status', 'data': {'description': description, 'done': done}}
        for description, done in (
            ('Micro-app work queued', False),
            ('Editing the micro-app', False),
            ('Micro-app work is waiting for authentication', False),
            ('Checking the micro-app', False),
            ('Publishing the micro-app', False),
            ('Micro-app work complete', True),
        )
    ]


@pytest.mark.asyncio
async def test_heartbeat_and_emitter_error_do_not_end_a_running_job(monkeypatch):
    monkeypatch.setattr(jobs, '_HEARTBEAT_INTERVAL', 0)
    server = AsyncMock(side_effect=[receipt(), status('queued', 0), status('failed', 1, done=True)])
    emit = AsyncMock(side_effect=[OSError(), None, None])
    result, _ = await jobs.track_job(server, {}, emit)
    assert emit.await_count == 3
    assert result == {'success': False, 'error': 'Micro-app work failed', 'job_id': JOB_ID, 'phase': 'failed'}


@pytest.mark.asyncio
async def test_rejected_submission_is_never_retried():
    server = AsyncMock(return_value=({'success': False, 'error': 'App unavailable'}, None))
    emit = AsyncMock()
    result, _ = await jobs.track_job(server, {}, emit)
    assert result['error'] == 'App unavailable'
    server.assert_awaited_once_with(jobs.SUBMIT, {})
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_job_id_is_never_used_in_a_request():
    server = AsyncMock(return_value=receipt('https://untrusted.example/test'))
    result, _ = await jobs.track_job(server, {}, None)
    assert 'invalid job receipt' in result['error']
    server.assert_awaited_once_with(jobs.SUBMIT, {})


@pytest.mark.asyncio
async def test_transient_poll_errors_leave_a_checkable_receipt():
    server = AsyncMock(side_effect=[receipt(), status('editing', 1), *[({'error': 'transport'}, None)] * 3])
    emit = AsyncMock()
    result, _ = await jobs.track_job(server, {}, emit)
    assert result['job_id'] == JOB_ID and result['phase'] == 'editing' and result['done'] is False
    assert server.await_args_list[2:] == [call(jobs.STATUS, {'job_id': JOB_ID, 'cursor': 1})] * 3
    assert emit.await_args.args[0]['data']['done'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_status', [status('unknown', 2), status('publishing', 0), status('succeeded', 2)])
async def test_rejects_unknown_phases_replayed_cursors_and_inconsistent_terminal_states(bad_status):
    server = AsyncMock(side_effect=[receipt(), status('editing', 1), bad_status])
    result, _ = await jobs.track_job(server, {}, None)
    assert result['job_id'] == JOB_ID and result['phase'] == 'editing' and result['done'] is False
    assert server.await_count == 3


@pytest.mark.asyncio
async def test_poll_deadline_returns_a_receipt_without_cancelling_the_job(monkeypatch):
    monkeypatch.setattr(jobs, '_OBSERVE_TIMEOUT', 0)
    server = AsyncMock(return_value=receipt())
    result, _ = await jobs.track_job(server, {}, None)
    assert result['job_id'] == JOB_ID and result['done'] is False
    server.assert_awaited_once_with(jobs.SUBMIT, {})


@pytest.mark.asyncio
async def test_task_cancellation_detaches_observation_without_cancelling_the_job():
    server = AsyncMock(side_effect=[receipt(), asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await jobs.track_job(server, {}, None)
    assert server.await_args_list == [call(jobs.SUBMIT, {}), call(jobs.STATUS, {'job_id': JOB_ID, 'cursor': 0})]


@pytest.mark.asyncio
@pytest.mark.parametrize('server_id', [jobs.SERVER_ID, 'different-server'])
async def test_resolved_openapi_tool_limits_progress_to_the_pilot_connection(
    monkeypatch, openapi, connection, server_id
):
    from open_webui.utils import tools

    openapi['id'] = connection['info']['id'] = server_id
    connection['forward_cookies'] = True
    connection['headers'] = {'x-microapp-request-id': 'configured-id'}
    request = SimpleNamespace(
        state=SimpleNamespace(token=SimpleNamespace(credentials='test-session')), cookies={'x': 'y'}
    )
    user = SimpleNamespace(id='test-user', email='worker@example.test', name='Test', role='user')
    emit = AsyncMock()
    execute = AsyncMock(side_effect=[receipt(), status('succeeded', 1, done=True)])
    monkeypatch.setattr(tools, 'ENABLE_TOOL_SERVERS', True)
    monkeypatch.setattr(tools.Groups, 'get_groups_by_member_id', AsyncMock(return_value=[]))
    monkeypatch.setattr(tools.Config, 'get', AsyncMock(return_value=[connection]))
    monkeypatch.setattr(tools, 'get_tool_servers', AsyncMock(return_value=[openapi]))
    monkeypatch.setattr(tools, 'has_connection_access', AsyncMock(return_value=True))
    monkeypatch.setattr(tools, 'execute_tool_server', execute)
    metadata = {'chat_id': 'test-chat', 'message_id': 'test-message'}
    resolved = await tools.get_tools(
        request, [f'server:{server_id}'], user, {'__event_emitter__': emit, '__metadata__': metadata}
    )

    result, _ = await resolved[jobs.SUBMIT]['callable'](prompt='edit')

    requests = [invocation.kwargs for invocation in execute.await_args_list]
    assert requests[0]['headers']['Authorization'] == 'Bearer test-session'
    if server_id == jobs.SERVER_ID:
        assert result['phase'] == 'succeeded'
        assert [request['name'] for request in requests] == [jobs.SUBMIT, jobs.STATUS]
        assert requests[0]['headers'][jobs.REQUEST_ID_HEADER] == jobs.request_id(user.id, metadata, {'prompt': 'edit'})
        assert 'x-microapp-request-id' not in requests[0]['headers']
        for request in requests:
            assert request['url'] == URL
            assert request['cookies'] == {}
            assert request['allow_redirects'] is False
            assert list(request['server_data']['openapi']['paths']) == [f'/{request["name"]}']
        assert emit.await_count == 2
    else:
        assert result['phase'] == 'queued'
        assert len(requests) == 1 and requests[0]['cookies'] == {'x': 'y'}
        assert 'allow_redirects' not in requests[0]
        emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_pilot_status_never_follows_a_cross_origin_redirect(monkeypatch, openapi, connection):
    from aiohttp import web
    from open_webui.utils import tools

    calls = []

    async def target(request):
        calls.append(('destination', request.headers.get('Authorization')))
        return web.json_response({'success': True})

    destination = web.Application()
    destination.router.add_post('/', target)
    destination_runner = web.AppRunner(destination)
    await destination_runner.setup()
    source = web.Application()
    source_runner = web.AppRunner(source)
    try:
        await web.TCPSite(destination_runner, '127.0.0.1', 0).start()
        destination_port = destination_runner.addresses[0][1]

        async def redirect(request):
            calls.append(('pilot', request.headers.get('Authorization')))
            raise web.HTTPTemporaryRedirect(f'http://127.0.0.1:{destination_port}/')

        source.router.add_post(f'/micro-app-authoring/{jobs.STATUS}', redirect)
        await source_runner.setup()
        await web.TCPSite(source_runner, '127.0.0.1', 0).start()
        source_port = source_runner.addresses[0][1]
        connection['url'] = openapi['url'] = f'http://127.0.0.1:{source_port}/micro-app-authoring'
        request = SimpleNamespace(state=SimpleNamespace(token=SimpleNamespace(credentials='test-session')))
        user = SimpleNamespace(id='test-user', email='worker@example.test', name='Test', role='user')
        monkeypatch.setattr(tools, 'ENABLE_TOOL_SERVERS', True)
        monkeypatch.setattr(tools, 'AIOHTTP_CLIENT_ALLOW_REDIRECTS', True)
        await tools._execute_microapp_authoring_tool(
            request, user, {}, None, openapi, connection, jobs.STATUS, {'job_id': JOB_ID}
        )
    finally:
        await source_runner.cleanup()
        await destination_runner.cleanup()

    assert calls == [('pilot', 'Bearer test-session')]
