import asyncio

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import HTTPException
from open_webui import env
from open_webui.routers import openai as openai_router
from open_webui.utils import middleware, session_pool


class FakeProxy:
    """A local stand-in for LiteLLM behind a load balancer: scripted per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def handle(self, request):
        await request.read()
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        kind, arg = step
        if kind == 'stall':
            await asyncio.sleep(arg)
            return web.Response(text='late', content_type='application/json')
        if kind == 'gateway':
            return web.Response(status=arg, text='<html><body>502 Bad Gateway</body></html>', content_type='text/html')
        if kind == 'json_error':
            return web.json_response({'error': {'message': 'provider said no'}}, status=arg)
        if kind == 'drop':
            request.transport.close()
            await asyncio.sleep(1)
            return web.Response(text='never')
        if kind == 'sse':
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            for _ in range(arg):
                await response.write(b'data: {"x": 1}\n\n')
                await asyncio.sleep(0.05)
            await response.write(b'data: [DONE]\n\n')
            return response
        return web.json_response({'ok': True})


@pytest_asyncio.fixture
async def proxy_factory():
    runners = []

    async def start(script):
        fake = FakeProxy(script)
        app = web.Application()
        app.router.add_post('/chat/completions', fake.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return fake, f'http://127.0.0.1:{port}/chat/completions'

    yield start
    for runner in runners:
        await runner.cleanup()


async def send(url, *, stream, timeout):
    async with aiohttp.ClientSession() as session:
        response = await openai_router._request_chat_completion(
            session, stream=stream, method='POST', url=url, data=b'{}', timeout=timeout
        )
        try:
            return response.status, response.headers.get('Content-Type', ''), await response.read()
        finally:
            response.release()


STREAM_TIMEOUT = aiohttp.ClientTimeout(total=10, sock_read=0.5)


@pytest.mark.asyncio
async def test_streaming_request_without_headers_is_sent_once_more(proxy_factory, monkeypatch):
    monkeypatch.setattr(openai_router, 'OPENAI_API_NO_RESPONSE_RETRIES', 1)
    fake, url = await proxy_factory([('stall', 2.0), ('sse', 3)])
    status, content_type, body = await send(url, stream=True, timeout=STREAM_TIMEOUT)
    assert status == 200 and 'text/event-stream' in content_type
    assert body.count(b'data:') == 4
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_non_streaming_timeout_is_not_repeated(proxy_factory, monkeypatch):
    monkeypatch.setattr(openai_router, 'OPENAI_API_NO_RESPONSE_RETRIES', 1)
    fake, url = await proxy_factory([('stall', 2.0), ('ok', None)])
    with pytest.raises(asyncio.TimeoutError):
        await send(url, stream=False, timeout=aiohttp.ClientTimeout(total=0.5))
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_gateway_error_page_is_retried_but_provider_json_error_is_returned(proxy_factory, monkeypatch):
    monkeypatch.setattr(openai_router, 'OPENAI_API_NO_RESPONSE_RETRIES', 1)
    fake, url = await proxy_factory([('gateway', 502), ('ok', None)])
    status, _, body = await send(url, stream=False, timeout=STREAM_TIMEOUT)
    assert (status, fake.calls) == (200, 2)
    assert b'"ok"' in body

    fake, url = await proxy_factory([('json_error', 503), ('ok', None)])
    status, content_type, _ = await send(url, stream=False, timeout=STREAM_TIMEOUT)
    assert (status, fake.calls) == (503, 1)
    assert 'application/json' in content_type


@pytest.mark.asyncio
async def test_dropped_connection_is_retried(proxy_factory, monkeypatch):
    monkeypatch.setattr(openai_router, 'OPENAI_API_NO_RESPONSE_RETRIES', 1)
    fake, url = await proxy_factory([('drop', None), ('ok', None)])
    status, _, _ = await send(url, stream=False, timeout=STREAM_TIMEOUT)
    assert (status, fake.calls) == (200, 2)


@pytest.mark.asyncio
async def test_retries_off_keeps_the_old_single_attempt(proxy_factory, monkeypatch):
    monkeypatch.setattr(openai_router, 'OPENAI_API_NO_RESPONSE_RETRIES', 0)
    fake, url = await proxy_factory([('stall', 2.0), ('sse', 1)])
    with pytest.raises(asyncio.TimeoutError):
        await send(url, stream=True, timeout=STREAM_TIMEOUT)
    assert fake.calls == 1

    fake, url = await proxy_factory([('gateway', 502), ('ok', None)])
    status, _, _ = await send(url, stream=False, timeout=STREAM_TIMEOUT)
    assert (status, fake.calls) == (502, 1)


@pytest.mark.asyncio
async def test_last_attempt_failure_is_raised(proxy_factory, monkeypatch):
    monkeypatch.setattr(openai_router, 'OPENAI_API_NO_RESPONSE_RETRIES', 1)
    fake, url = await proxy_factory([('stall', 2.0), ('stall', 2.0)])
    with pytest.raises(asyncio.TimeoutError):
        await send(url, stream=True, timeout=STREAM_TIMEOUT)
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_a_stream_that_started_is_never_repeated(proxy_factory, monkeypatch):
    monkeypatch.setattr(openai_router, 'OPENAI_API_NO_RESPONSE_RETRIES', 1)
    fake, url = await proxy_factory([('sse', 20), ('sse', 1)])
    status, _, body = await send(url, stream=True, timeout=STREAM_TIMEOUT)
    assert (status, fake.calls) == (200, 1)
    assert body.count(b'data:') == 21


def test_no_response_reasons():
    reason = openai_router._no_response_reason
    assert reason(aiohttp.ConnectionTimeoutError(), stream=False) == 'connect timeout'
    assert reason(aiohttp.SocketTimeoutError(), stream=True) == 'no response headers in time'
    assert reason(asyncio.TimeoutError(), stream=True) == 'no response headers in time'
    assert reason(asyncio.TimeoutError(), stream=False) is None
    assert reason(aiohttp.ServerDisconnectedError(), stream=False) == 'ServerDisconnectedError'
    assert reason(ValueError('bad'), stream=True) is None


def test_stream_total_budget_and_idle_cap_feed_the_stream_timeout():
    assert session_pool.get_client_timeout(stream=True).total == env.AIOHTTP_CLIENT_STREAM_TOTAL_TIMEOUT
    assert session_pool.get_client_timeout(stream=True).sock_read == env.AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT
    assert session_pool.get_client_timeout(stream=False).total == env.AIOHTTP_CLIENT_TIMEOUT


def test_stream_total_defaults_to_the_client_timeout(monkeypatch):
    import importlib

    monkeypatch.delenv('AIOHTTP_CLIENT_STREAM_TOTAL_TIMEOUT', raising=False)
    monkeypatch.setenv('AIOHTTP_CLIENT_TIMEOUT', '600')
    reloaded = importlib.reload(env)
    try:
        assert reloaded.AIOHTTP_CLIENT_STREAM_TOTAL_TIMEOUT == 600
        assert reloaded.OPENAI_API_NO_RESPONSE_RETRIES == 0
        monkeypatch.setenv('AIOHTTP_CLIENT_STREAM_TOTAL_TIMEOUT', '1800')
        monkeypatch.setenv('OPENAI_API_NO_RESPONSE_RETRIES', '7')
        reloaded = importlib.reload(env)
        assert reloaded.AIOHTTP_CLIENT_STREAM_TOTAL_TIMEOUT == 1800
        assert reloaded.OPENAI_API_NO_RESPONSE_RETRIES == 2
    finally:
        monkeypatch.delenv('AIOHTTP_CLIENT_STREAM_TOTAL_TIMEOUT', raising=False)
        monkeypatch.delenv('OPENAI_API_NO_RESPONSE_RETRIES', raising=False)
        importlib.reload(env)


def test_failed_turn_text_is_never_empty():
    assert middleware.message_error_content(asyncio.TimeoutError()) == middleware.STREAM_TIMEOUT_MESSAGE
    assert middleware.message_error_content(
        HTTPException(status_code=500, detail='Open WebUI: Server Connection Error')
    ) == ('Open WebUI: Server Connection Error')
    assert middleware.message_error_content(RuntimeError()) == 'RuntimeError'
    assert middleware.message_error_content(RuntimeError('provider said no')) == 'provider said no'
    assert middleware.message_error_content({'detail': 'x'}) == 'x'
