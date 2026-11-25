"""Only a persisted authenticated chat may attest the exact dev SQL dispatch."""

import ast
import base64
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from open_webui.utils import sql_chat_tool_context as context


USER = 'a1111111-1111-4111-8111-111111111111'
CHAT = 'b2222222-2222-4222-8222-222222222222'
MESSAGE = 'c3333333-3333-4333-8333-333333333333'
DEV_URL = 'http://dev.internal/mcpo/sql-exec'
TOOL = 'tool_executeSQLQuery_post'
CHAT_HEADER = 'X-OpenWebUI-Chat-Id'
MESSAGE_HEADER = 'X-OpenWebUI-Message-Id'
SECRET = 'synthetic-test-signing-key'


def test_context_requires_the_owning_user_and_an_authenticated_chat_session(monkeypatch):
    monkeypatch.setattr(context, 'SQL_CHAT_TOOL_CONTEXT_URL', DEV_URL + '/executeSQLQuery')
    metadata = {'chat_id': CHAT, 'message_id': MESSAGE, 'session_id': 'synthetic-session'}
    message = {'role': 'assistant', 'user_id': USER}

    def owner(session_id):
        assert session_id == 'synthetic-session'
        return USER

    assert context.sql_chat_tool_context(USER, metadata, message, True, owner) == context.SQLChatToolContext(
        USER, CHAT, MESSAGE
    )
    for change in ({'internal': True}, {'automation_id': 'job'}, {'direct': True}, {'session_id': None},
                   {'chat_id': 'temporary:fixture'}, {'message_id': 'not-a-saved-id'}):
        assert context.sql_chat_tool_context(USER, {**metadata, **change}, message, True, owner) is None
    assert context.sql_chat_tool_context(USER, metadata, message, False, owner) is None
    assert context.sql_chat_tool_context(USER, metadata, message, True, lambda _: 'different-owner') is None
    assert context.sql_chat_tool_context(USER, metadata, {'role': 'user'}, True, owner) is None


@pytest.fixture
def tool_dispatch():
    """Execute the actual tool-server dispatch against a local fake HTTP transport."""
    tree = ast.parse((Path(context.__file__).parent / 'tools.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'execute_tool_server')
    outgoing = []

    @asynccontextmanager
    async def post(url, **kw):
        outgoing.append((url, kw))
        yield SimpleNamespace(status=200, headers={}, json=fixture_response)

    async def fixture_response():
        return {'result': 'fixture'}

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield SimpleNamespace(post=post)

    namespace = {
        **vars(context),
        'Any': Any,
        'ENABLE_TOOL_SERVERS': True,
        'OPENAPI_HTTP_METHODS': {'post'},
        'AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER': 1,
        'AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL': True,
        'AIOHTTP_CLIENT_ALLOW_REDIRECTS': True,
        'FORWARD_SESSION_INFO_HEADER_CHAT_ID': CHAT_HEADER,
        'FORWARD_SESSION_INFO_HEADER_MESSAGE_ID': MESSAGE_HEADER,
        'SQL_CHAT_TOOL_CONTEXT_URL': DEV_URL + '/executeSQLQuery',
        'WEBUI_SECRET_KEY': SECRET,
        'RelayInterrupted': type('RelayInterrupted', (Exception,), {}),
        'aiohttp': SimpleNamespace(ClientTimeout=lambda **kw: kw),
        'get_terminal_client': client,
        'resolve_schema': lambda schema, components: schema,
        'log': logging.getLogger(__name__),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(context.__file__), 'exec'), namespace)
    return namespace['execute_tool_server'], outgoing


@pytest.mark.asyncio
async def test_exact_post_body_is_signed_at_dispatch(tool_dispatch, monkeypatch):
    dispatch, outgoing = tool_dispatch
    monkeypatch.setattr(context.time, 'time', lambda: 1_800_000_000)
    body = {'sql_query': "SELECT 1 AS naïve", 'include_rows': False}
    server = {'openapi': {'paths': {'/executeSQLQuery': {'post': {
        'operationId': TOOL, 'requestBody': {'content': {'application/json': {'schema': {}}}},
    }}}}}
    ownership = context.SQLChatToolContext(USER, CHAT, MESSAGE)
    headers = {CHAT_HEADER: CHAT, MESSAGE_HEADER: MESSAGE, 'x-oxy-chat-tool-context': 'caller-value'}
    await dispatch(DEV_URL, headers, {}, TOOL, body, server, trusted_sql_context=ownership)
    url, request = outgoing.pop()
    assert url == DEV_URL + '/executeSQLQuery'
    assert request['json'] == body
    assert request['allow_redirects'] is False
    captured = request['headers'].pop(context.SQL_CHAT_TOOL_CONTEXT_HEADER)
    assert request['headers'] == {CHAT_HEADER: CHAT, MESSAGE_HEADER: MESSAGE}
    segment, signature = captured.encode('ascii').split(b'.')
    assert signature == base64.urlsafe_b64encode(
        hmac.digest(SECRET.encode('utf-8'), b'oxy-sql-chat-ui-v1.' + segment, 'sha256')
    ).rstrip(b'=')
    payload = json.loads(base64.urlsafe_b64decode(segment + b'=' * (-len(segment) % 4)))
    encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
    assert payload == {'v': 1, 'purpose': 'sql-chat-ui', 'user_id': USER, 'chat_id': CHAT, 'message_id': MESSAGE,
                       'iat': 1_800_000_000, 'exp': 1_800_000_060, 'args_hash': hashlib.sha256(encoded).hexdigest()}

    for context_arg, args, request_headers in (
        (None, body, headers),
        (ownership, {**body, 'include_rows': True}, headers),
        (ownership, body, {**headers, CHAT_HEADER: 'untrusted'}),
    ):
        await dispatch(DEV_URL, request_headers, {}, TOOL, args, server, trusted_sql_context=context_arg)
        _, request = outgoing.pop()
        assert request['allow_redirects'] is True
        assert not any(name.lower() == context.SQL_CHAT_TOOL_CONTEXT_HEADER.lower() for name in request['headers'])

    await dispatch('http://other.internal', {}, {}, TOOL, body, server, trusted_sql_context=ownership)
    assert context.SQL_CHAT_TOOL_CONTEXT_HEADER not in outgoing.pop()[1]['headers']
