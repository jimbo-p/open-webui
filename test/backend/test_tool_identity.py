"""Tool introspection accepts machine credentials while browser sessions stay human-only."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from open_webui.routers import auths
from open_webui.utils import auth, mfa


@pytest.fixture
def identity(monkeypatch):
    user = SimpleNamespace(id='u1', email='user@example.com', name='Tool User', role='user')
    record = SimpleNamespace(active=True, session_stamp='current', mfa=None)
    config = {
        'auth.enable_api_keys': True,
        'user.permissions': {},
        'auth.api_key.endpoint_restrictions': False,
        'auth.api_key.allowed_endpoints': '',
    }
    monkeypatch.setattr(auth, 'SESSION_SECRET', 'test-only-signing-key-with-at-least-32-bytes')
    monkeypatch.setattr(auth.Users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(auth.Users, 'get_user_by_api_key', AsyncMock(return_value=user))
    monkeypatch.setattr(auth.Users, 'update_last_active_by_id', AsyncMock())
    monkeypatch.setattr(auth.Auths, 'get_auth_by_id', AsyncMock(return_value=record))
    monkeypatch.setattr(auth.Config, 'get_many', AsyncMock(side_effect=lambda *keys: config))
    monkeypatch.setattr(auth, 'has_permission', AsyncMock(return_value=True))
    monkeypatch.setattr(mfa, 'get_mfa_config', AsyncMock(return_value=SimpleNamespace(ENABLE_MFA=False)))
    app = FastAPI()
    app.state.redis = None
    app.include_router(auths.router, prefix='/api/v1/auths')

    async def no_db():
        yield None

    app.dependency_overrides[auths.get_async_session] = no_db
    return app, user, record, config


def token(kind='automation', **claims):
    return auth.create_token(
        {'id': 'u1', 'typ': kind, 'session_stamp': 'current', **claims}, expires_delta=timedelta(minutes=1)
    )


async def request(identity, credential, path='/api/v1/auths/verify'):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=identity[0]), base_url='http://test') as client:
        headers = {'Authorization': 'Bearer ' + credential} if credential else {}
        return await client.get(path, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['session', 'automation', 'subagent', 'api_key'])
async def test_introspection_resolves_each_supported_credential_without_returning_a_token_or_cookie(identity, kind):
    credential = 'sk-test-key' if kind == 'api_key' else token(kind)
    response = await request(identity, credential)
    assert response.status_code == 200, response.text
    assert response.json() == {'id': 'u1', 'email': 'user@example.com', 'name': 'Tool User', 'role': 'user'}
    assert 'set-cookie' not in response.headers
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['automation', 'subagent', 'api_key'])
async def test_introspection_does_not_open_browser_session_endpoint_to_machine_credentials(identity, kind):
    response = await request(identity, 'sk-test-key' if kind == 'api_key' else token(kind), '/api/v1/auths/')
    assert response.status_code == 403
    assert response.json()['detail'] == 'A human session is required.'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'case', ['missing', 'malformed', 'expired', 'revoked', 'unknown_type', 'inactive', 'pending', 'unknown_key']
)
async def test_introspection_rejects_invalid_credentials_and_inactive_users(identity, case, monkeypatch):
    credential = token()
    if case == 'missing':
        credential = None
    elif case == 'malformed':
        credential = 'invalid-signature'
    elif case == 'expired':
        # Exercise JWT expiry through the real decoder.
        credential = auth.create_token(
            {'id': 'u1', 'typ': 'automation', 'session_stamp': 'current'}, timedelta(seconds=-1)
        )
    elif case == 'revoked':
        identity[2].session_stamp = 'new-stamp'
    elif case == 'unknown_type':
        credential = token('mfa_challenge')
    elif case == 'inactive':
        identity[2].active = False
    elif case == 'pending':
        identity[1].role = 'pending'
    elif case == 'unknown_key':
        credential = 'sk-missing'
        monkeypatch.setattr(auth.Users, 'get_user_by_api_key', AsyncMock(return_value=None))
    response = await request(identity, credential)
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['keys_disabled', 'permission_denied', 'endpoint_denied'])
async def test_introspection_preserves_api_key_restrictions(identity, case, monkeypatch):
    if case == 'keys_disabled':
        identity[3]['auth.enable_api_keys'] = False
    elif case == 'permission_denied':
        monkeypatch.setattr(auth, 'has_permission', AsyncMock(return_value=False))
    else:
        identity[3]['auth.api_key.endpoint_restrictions'] = True
        identity[3]['auth.api_key.allowed_endpoints'] = '/api/chat/completions'
    response = await request(identity, 'sk-test-key')
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_introspection_keeps_browser_mfa_requirement(identity, monkeypatch):
    monkeypatch.setattr(mfa, 'get_mfa_config', AsyncMock(return_value=SimpleNamespace(ENABLE_MFA=True)))
    response = await request(identity, token('session', auth_method='password'))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_introspection_honors_redis_token_revocation(identity):
    identity[0].state.redis = SimpleNamespace(get=AsyncMock(return_value='1'))
    response = await request(identity, token())
    assert response.status_code == 401
