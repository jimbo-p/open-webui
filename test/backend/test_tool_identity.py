"""Tool introspection accepts machine credentials while browser sessions stay human-only."""

import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

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
        {'id': 'u1', 'typ': kind, 'session_stamp': 'current', **claims}, expires_delta=dt.timedelta(minutes=1)
    )


async def request(identity, credential, path='/api/v1/auths/verify', *, include_context=False):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=identity[0]), base_url='http://test') as client:
        headers = {'Authorization': 'Bearer ' + credential} if credential else {}
        params = {'include_verification_context': 'true'} if include_context else None
        return await client.get(path, headers=headers, params=params)


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
@pytest.mark.parametrize('include_context', [False, True])
@pytest.mark.parametrize(
    'case', ['missing', 'malformed', 'expired', 'revoked', 'unknown_type', 'inactive', 'pending', 'unknown_key']
)
async def test_introspection_rejects_invalid_credentials_and_inactive_users(
    identity, case, include_context, monkeypatch
):
    credential = token()
    if case == 'missing':
        credential = None
    elif case == 'malformed':
        credential = 'invalid-signature'
    elif case == 'expired':
        # Exercise JWT expiry through the real decoder.
        credential = auth.create_token(
            {'id': 'u1', 'typ': 'automation', 'session_stamp': 'current'}, dt.timedelta(seconds=-1)
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
    response = await request(identity, credential, include_context=include_context)
    assert response.status_code == 401, response.text
    assert 'verification_context' not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize('include_context', [False, True])
@pytest.mark.parametrize('case', ['keys_disabled', 'permission_denied', 'endpoint_denied'])
async def test_introspection_preserves_api_key_restrictions(identity, case, include_context, monkeypatch):
    if case == 'keys_disabled':
        identity[3]['auth.enable_api_keys'] = False
    elif case == 'permission_denied':
        monkeypatch.setattr(auth, 'has_permission', AsyncMock(return_value=False))
    else:
        identity[3]['auth.api_key.endpoint_restrictions'] = True
        identity[3]['auth.api_key.allowed_endpoints'] = '/api/chat/completions'
    response = await request(identity, 'sk-test-key', include_context=include_context)
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('include_context', [False, True])
async def test_introspection_keeps_browser_mfa_requirement(identity, include_context, monkeypatch):
    monkeypatch.setattr(mfa, 'get_mfa_config', AsyncMock(return_value=SimpleNamespace(ENABLE_MFA=True)))
    response = await request(identity, token('session', auth_method='password'), include_context=include_context)
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('include_context', [False, True])
async def test_introspection_honors_redis_token_revocation(identity, include_context):
    identity[0].state.redis = SimpleNamespace(get=AsyncMock(return_value='1'))
    response = await request(identity, token(), include_context=include_context)
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['session', 'automation', 'subagent'])
@pytest.mark.parametrize('stamp', ['current', None])
async def test_verification_context_preserves_validated_jwt_lineage_without_credentials(identity, kind, stamp):
    identity[2].session_stamp = stamp
    credential = token(
        kind, session_stamp=stamp, auth_method='oauth', mfa_verified=True, private_extension='must-not-be-returned'
    )
    source = auth.decode_token(credential)

    response = await request(identity, credential, include_context=True)

    assert response.status_code == 200, response.text
    context = response.json()['verification_context']
    assert context == {
        'version': 1,
        'user_id': 'u1',
        'claims': {
            key: source[key] for key in ('session_stamp', 'typ', 'auth_method', 'mfa_verified', 'exp', 'iat', 'jti')
        },
    }
    assert credential not in response.text
    assert 'must-not-be-returned' not in response.text
    assert 'token' not in response.json()
    assert 'set-cookie' not in response.headers
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_legacy_verification_context_preserves_null_stamp_and_absent_lineage(identity):
    identity[2].session_stamp = None
    credential = auth.jwt.encode({'id': 'u1'}, auth.SESSION_SECRET, algorithm=auth.ALGORITHM)

    response = await request(identity, credential, include_context=True)

    assert response.status_code == 200, response.text
    assert response.json()['verification_context']['claims'] == {'session_stamp': None, 'typ': 'session'}


@pytest.mark.asyncio
async def test_verification_context_does_not_refresh_a_stamp_revoked_after_authentication(identity, monkeypatch):
    credential = token()

    async def resolve_user(user_id):
        # Account-wide revocation races with the remainder of this request.
        identity[2].session_stamp = 'revoked-after-validation'
        return identity[1]

    monkeypatch.setattr(auth.Users, 'get_user_by_id', resolve_user)
    response = await request(identity, credential, include_context=True)

    assert response.status_code == 200, response.text
    assert response.json()['verification_context']['claims']['session_stamp'] == 'current'
    assert (await request(identity, credential, include_context=True)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp', ['current', None])
async def test_api_key_verification_context_uses_active_account_and_short_lifetime(identity, stamp):
    identity[2].session_stamp = stamp
    before = int(time.time())

    response = await request(identity, 'sk-test-key', include_context=True)

    assert response.status_code == 200, response.text
    context = response.json()['verification_context']
    assert context['version'] == 1 and context['user_id'] == 'u1'
    claims = context['claims']
    assert set(claims) == {'session_stamp', 'typ', 'iat', 'exp', 'jti'}
    assert claims['session_stamp'] == stamp
    assert claims['typ'] == 'automation'
    assert before <= claims['iat'] <= int(time.time())
    assert claims['exp'] - claims['iat'] == 300
    assert str(UUID(claims['jti'])) == claims['jti']
    assert 'sk-test-key' not in response.text
    assert 'token' not in response.json()
    assert 'set-cookie' not in response.headers
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_api_key_verification_context_refuses_inactive_account(identity):
    identity[2].active = False

    response = await request(identity, 'sk-test-key', include_context=True)

    assert response.status_code == 401
    assert 'verification_context' not in response.json()


@pytest.mark.asyncio
async def test_verification_context_explicit_false_keeps_identity_contract(identity):
    response = await request(identity, token(), '/api/v1/auths/verify?include_verification_context=false')

    assert response.status_code == 200
    assert set(response.json()) == {'id', 'email', 'name', 'role'}


@pytest.mark.asyncio
async def test_verification_context_never_coerces_a_source_mfa_assertion(identity):
    credential = token('session', mfa_verified='true')
    # MFA is disabled in this fixture, so normal authentication can accept the
    # credential. Delegation must never turn this string into a verified factor.
    assert (await request(identity, credential)).status_code == 200

    response = await request(identity, credential, include_context=True)

    assert response.status_code == 401
    assert 'verification_context' not in response.json()


@pytest.mark.asyncio
async def test_verification_context_preserves_fractional_numeric_dates(identity):
    source = {
        'id': 'u1',
        'typ': 'automation',
        'session_stamp': 'current',
        'iat': time.time() - 10.5,
        'exp': time.time() + 60.5,
        'jti': 'parent-token',
    }
    credential = auth.jwt.encode(source, auth.SESSION_SECRET, algorithm=auth.ALGORITHM)

    response = await request(identity, credential, include_context=True)

    assert response.status_code == 200, response.text
    assert response.json()['verification_context']['claims'] == {
        key: value for key, value in source.items() if key != 'id'
    }
