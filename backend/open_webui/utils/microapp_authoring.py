"""Track an authenticated Micro-Apps authoring job in the initiating chat."""

import asyncio
import json
import logging
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

SERVER_ID = 'micro-app-authoring'
SUBMIT = 'author_microapp'
STATUS = 'microapp_job_status'
CANCEL = 'cancel_microapp_job'
REQUEST_ID_HEADER = 'X-MicroApp-Request-ID'

_POLL_INTERVAL = 3
_HEARTBEAT_INTERVAL = 45
_OBSERVE_TIMEOUT = 15 * 60
_REQUEST_TIMEOUT = 15
_EVENT_TIMEOUT = 2
_MAX_POLL_ERRORS = 3

_PHASES = {
    'queued': 'Micro-app work queued',
    'editing': 'Editing the micro-app',
    'checking': 'Checking the micro-app',
    'publishing': 'Publishing the micro-app',
    'authentication_required': 'Micro-app work is waiting for authentication',
    'succeeded': 'Micro-app work complete',
    'failed': 'Micro-app work failed',
    'cancelled': 'Micro-app work cancelled',
}
_TERMINAL = {'succeeded', 'failed', 'cancelled'}

log = logging.getLogger(__name__)


def request_id(user_id: str, metadata: dict, params: dict) -> str:
    """Deduplicate identical submissions within one authenticated chat turn."""
    message_id = metadata.get('message_id') or metadata.get('assistant_message_id')
    if not message_id:
        return str(uuid4())
    identity = json.dumps([user_id, metadata.get('chat_id'), message_id, params], sort_keys=True, separators=(',', ':'))
    return str(uuid5(NAMESPACE_URL, f'open-webui:microapp-authoring:{identity}'))


def resolve_route(server_data: dict, connection: dict, operation: str) -> dict | None:
    """Pin the configured session connection and one canonical POST operation."""
    if (
        operation not in {SUBMIT, STATUS, CANCEL}
        or (connection.get('info') or {}).get('id') != SERVER_ID
        or connection.get('auth_type') != 'session'
    ):
        return None
    url = (connection.get('url') or '').rstrip('/')
    try:
        parsed = urlsplit(url)
        if (
            url != server_data.get('url')
            or parsed.scheme not in {'http', 'https'}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith('/micro-app-authoring')
        ):
            return None
    except ValueError:
        return None
    openapi = server_data.get('openapi')
    paths = openapi.get('paths') if isinstance(openapi, dict) else None
    path = f'/{operation}'
    methods = paths.get(path) if isinstance(paths, dict) else None
    method = methods.get('post') if isinstance(methods, dict) else None
    if not isinstance(method, dict) or method.get('operationId') != operation:
        return None
    # The generic OpenAPI client resolves operation IDs by searching paths.
    # Keep the canonical path even if the spec advertises duplicate IDs.
    return {**server_data, 'openapi': {**openapi, 'paths': {path: {'post': method}}}}


def _job_id(value) -> str | None:
    if isinstance(value, str) and len(value) == 36:
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return None


async def _status(event_emitter, description: str, *, done: bool = False) -> None:
    if event_emitter is None:
        return
    try:
        async with asyncio.timeout(_EVENT_TIMEOUT):
            await event_emitter({'type': 'status', 'data': {'description': description, 'done': done}})
    except Exception:
        log.debug('Could not deliver a micro-app progress event')


async def track_job(call, params: dict, event_emitter):
    """Submit once, then return the result or a receipt for checking it later."""
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            submission, headers = await call(SUBMIT, params)
    except TimeoutError:
        return {'error': 'Micro-app submission could not be confirmed.'}, None
    if not isinstance(submission, dict):
        return {'error': 'The micro-app service returned an invalid job receipt.'}, None
    if submission.get('success') is not True:
        return submission, headers
    job_id = _job_id(submission.get('job_id'))
    if not job_id:
        return {'error': 'The micro-app service returned an invalid job receipt.'}, None
    return await _observe_job(call, job_id, event_emitter)


async def _observe_job(call, job_id: str, event_emitter):
    phase = 'queued'
    cursor = 0
    failures = 0
    await _status(event_emitter, _PHASES[phase])
    clock = asyncio.get_running_loop().time
    last_event = clock()
    try:
        async with asyncio.timeout(_OBSERVE_TIMEOUT):
            while failures < _MAX_POLL_ERRORS:
                await asyncio.sleep(_POLL_INTERVAL)
                try:
                    async with asyncio.timeout(_REQUEST_TIMEOUT):
                        status, headers = await call(STATUS, {'job_id': job_id, 'cursor': cursor})
                except TimeoutError:
                    status = None
                if not isinstance(status, dict) or status.get('success') is not True:
                    failures += 1
                    continue
                failures = 0
                next_phase, next_cursor, done = status.get('phase'), status.get('cursor'), status.get('done')
                if (
                    not isinstance(next_phase, str)
                    or next_phase not in _PHASES
                    or type(next_cursor) is not int
                    or next_cursor < cursor
                    or type(done) is not bool
                    or (next_phase != 'authentication_required' and done != (next_phase in _TERMINAL))
                ):
                    break
                cursor = next_cursor
                if done or phase != next_phase or clock() - last_event >= _HEARTBEAT_INTERVAL:
                    await _status(event_emitter, _PHASES[next_phase], done=done)
                    last_event = clock()
                phase = next_phase
                if done:
                    result = status.get('result')
                    result = result if isinstance(result, dict) else {}
                    result = {**result, 'success': phase == 'succeeded', 'job_id': job_id, 'phase': phase}
                    if phase != 'succeeded':
                        result.setdefault('error', _PHASES[phase])
                    return result, headers
    except TimeoutError:
        pass

    await _status(event_emitter, 'Check micro-app status for further progress', done=True)
    return {
        'success': True,
        'done': False,
        'job_id': job_id,
        'phase': phase,
        'message': 'Use microapp_job_status with the job ID to check progress.',
    }, None
