"""Durable one-shot timers backed by internal child chats."""

from __future__ import annotations

import asyncio
import copy
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException, Request
from open_webui.internal.db import get_async_db
from open_webui.models.chat_messages import ChatMessages
from open_webui.models.chats import Chat, ChatForm, Chats
from open_webui.models.users import UserModel, Users
from open_webui.tasks import has_active_tasks
from open_webui.utils.auth import VERIFIED_USER_ROLES
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import get_message_list
from open_webui.utils.tool_calling import strip_tool_approval_prompt
from open_webui.utils.tool_search import strip_deferred_tools_manifest
from sqlalchemy import func, select, update
from starlette.datastructures import Headers

log = logging.getLogger(__name__)

_RELATIVE_TIME = re.compile(r'^(?:\+|in\s+)?(\d+)\s*(s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?|d|days?)$')
_RFC3339_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$')
_TIME_UNITS_NS = {
    's': 1_000_000_000,
    'm': 60 * 1_000_000_000,
    'h': 60 * 60 * 1_000_000_000,
    'd': 24 * 60 * 60 * 1_000_000_000,
}
_timer_locks: dict[str, asyncio.Lock] = {}


def parse_timer_at(value: str) -> int:
    """Normalize a relative offset or timezone-aware RFC 3339 timestamp."""
    raw = value.strip()
    now = time.time_ns()
    relative = _RELATIVE_TIME.fullmatch(raw.lower())
    if relative:
        count = int(relative.group(1))
        if count <= 0:
            raise ValueError('at must be in the future.')
        return now + count * _TIME_UNITS_NS[relative.group(2)[0]]

    if not _RFC3339_TIME.fullmatch(raw):
        raise ValueError(
            'at must be a relative time such as 10s or in 10 seconds, or an RFC 3339 timestamp with a timezone.'
        )
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(
            'at must be a relative time such as 10s or in 10 seconds, or an RFC 3339 timestamp with a timezone.'
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('absolute at values must include an explicit timezone.')

    due_at = int(parsed.timestamp() * 1_000_000_000)
    if due_at <= now:
        raise ValueError('at must be in the future.')
    return due_at


async def create_timer(
    *,
    prompt: str,
    at: str,
    cancel_on: list[Literal['chat.read', 'chat.user_message']] | None,
    request: Request,
    user_data: dict,
    metadata: dict,
    parent_chat_id: str,
    parent_message_id: str | None,
) -> str:
    prompt = prompt.strip()
    if not prompt:
        return 'Error: prompt must not be empty.'
    if not parent_chat_id or not user_data.get('id'):
        return 'Error: chat and user context are required.'

    try:
        due_at = parse_timer_at(at)
    except ValueError as exc:
        return f'Error: {exc}'

    selected_events = cancel_on or []
    allowed_events = {'chat.read', 'chat.user_message'}
    if any(event not in allowed_events for event in selected_events):
        return 'Error: cancel_on accepts only chat.read and chat.user_message.'
    selected_events = list(dict.fromkeys(selected_events))

    model_id = metadata.get('model_id') or (metadata.get('model') or {}).get('id')
    if not model_id:
        return 'Error: model context is required.'
    if metadata.get('direct'):
        return 'Error: timers are unavailable for direct connections.'

    chat_id = str(uuid4())
    user_message_id = str(uuid4())
    user = UserModel(**user_data)
    run = {
        'model_id': model_id,
        'session_id': metadata.get('session_id'),
        'tool_ids': copy.deepcopy(metadata.get('tool_ids') or []),
        'skill_ids': copy.deepcopy(metadata.get('skill_ids') or []),
        'chat_context': copy.deepcopy(metadata.get('chat_context') or {}),
        'filter_ids': copy.deepcopy(metadata.get('filter_ids') or []),
        'terminal_id': metadata.get('terminal_id'),
        'features': copy.deepcopy(metadata.get('features') or {}),
        'files': copy.deepcopy(metadata.get('files') or []),
        'variables': copy.deepcopy(metadata.get('variables') or {}),
    }

    chat = await Chats.insert_new_chat(
        chat_id,
        user.id,
        ChatForm(
            chat={
                'id': chat_id,
                'title': f'Timer: {prompt[:60]}',
                'models': [model_id],
                'history': {
                    'currentId': user_message_id,
                    'messages': {
                        user_message_id: {
                            'id': user_message_id,
                            'parentId': None,
                            'childrenIds': [],
                            'role': 'user',
                            'content': prompt,
                            'timestamp': int(time.time()),
                            'models': [model_id],
                        },
                    },
                },
                'messages': [{'role': 'user', 'content': prompt}],
            }
        ),
        internal_meta={
            'internal': True,
            'type': 'timer',
            'parent_chat_id': parent_chat_id,
            'parent_message_id': parent_message_id,
            'timer_at': due_at,
            'status': 'pending',
            'timer_model_id': model_id,
            'timer_task_message_id': user_message_id,
            'cancel_on': selected_events,
            'run': run,
        },
        timer_at=due_at,
    )
    if not chat:
        return 'Error: failed to create timer.'

    return JSONCodec.dumps(
        {
            'status': 'set',
            'at': datetime.fromtimestamp(due_at / 1_000_000_000, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'cancel_on': selected_events,
        },
        ensure_ascii=False,
    )


async def claim_due_timers(now_ns: int, limit: int = 10) -> list[tuple[str, str]]:
    """Claim only available capacity, with at most one due timer per parent in this batch."""
    if limit <= 0:
        return []
    async with get_async_db() as db:
        ranked = (
            select(
                Chat.id.label('id'),
                func.row_number()
                .over(
                    partition_by=Chat.meta['parent_chat_id'].as_string(),
                    order_by=(Chat.timer_at, Chat.id),
                )
                .label('parent_position'),
            )
            .where(Chat.timer_at <= now_ns, Chat.meta['status'].as_string() == 'pending')
            .subquery()
        )
        stmt = (
            select(Chat.id, Chat.meta)
            .where(Chat.id.in_(select(ranked.c.id).where(ranked.c.parent_position == 1)))
            .order_by(Chat.timer_at, Chat.id)
            .limit(limit)
        )
        if db.bind.dialect.name == 'postgresql':
            stmt = stmt.with_for_update(skip_locked=True)

        result = await db.execute(stmt)
        claimed = []
        for row in result.all():
            claim_id = str(uuid4())
            changed = await db.execute(
                update(Chat)
                .where(
                    Chat.id == row.id,
                    Chat.timer_at <= now_ns,
                    Chat.meta['status'].as_string() == 'pending',
                )
                .values(
                    meta={
                        **(row.meta or {}),
                        'status': 'running',
                        'timer_started_at': now_ns,
                        'timer_claim_id': claim_id,
                    },
                    timer_at=None,
                    updated_at=int(time.time()),
                )
            )
            if changed.rowcount:
                claimed.append((row.id, claim_id))
        await db.commit()
        return claimed


async def rearm_due_timer(timer_id: str, claim_id: str | None) -> None:
    """Return only this still-undelivered claim to the durable queue."""
    async with get_async_db() as db:
        stmt = select(Chat.id, Chat.meta).where(Chat.id == timer_id, Chat.meta['status'].as_string() == 'running')
        if claim_id is not None:
            stmt = stmt.where(Chat.meta['timer_claim_id'].as_string() == claim_id)
        result = await db.execute(stmt)
        row = result.first()
        if row is None:
            return
        meta = row.meta or {}
        next_attempt = time.time_ns() + int(random.uniform(1, 3) * 1_000_000_000)
        condition = [Chat.id == timer_id, Chat.meta['status'].as_string() == 'running']
        if claim_id is not None:
            condition.append(Chat.meta['timer_claim_id'].as_string() == claim_id)
        await db.execute(
            update(Chat)
            .where(*condition)
            .values(
                meta={**meta, 'status': 'pending', 'timer_claim_id': None, 'timer_started_at': None},
                timer_at=max(meta.get('timer_at') or 0, next_attempt),
                updated_at=int(time.time()),
            )
        )
        await db.commit()


class _TimerParentBusy(Exception):
    """Admission was deferred before loading the parent's conversation."""


async def cancel_timers_for_chat(
    parent_chat_id: str, event: Literal['chat.read', 'chat.user_message'], user_id: str
) -> None:
    """Owner-scoped: without the user filter, reading a chat cancels every user's timers on it."""
    async with get_async_db() as db:
        result = await db.execute(
            select(Chat)
            .where(Chat.timer_at.isnot(None))
            .where(Chat.user_id == user_id)
            .where(Chat.meta['parent_chat_id'].as_string() == parent_chat_id)
            .where(Chat.meta['status'].as_string() == 'pending')
        )
        now_ns = int(time.time_ns())
        for row in result.scalars().all():
            meta = row.meta or {}
            if event not in (meta.get('cancel_on') or []):
                continue
            row.meta = {
                **meta,
                'status': 'cancelled',
                'timer_cancelled_at': now_ns,
                'timer_cancelled_by': event,
            }
            row.timer_at = None
            row.updated_at = int(time.time())
        await db.commit()


async def execute_due_timer(app, timer_id: str, claim_id: str | None = None) -> list[str] | None:
    lock = _timer_locks.setdefault(timer_id, asyncio.Lock())
    async with lock:
        from open_webui.socket.main import sio
        from open_webui.utils.subagents import _parent_locks

        timer = await Chats.get_chat_by_id(timer_id)
        if not timer:
            return
        meta = timer.meta or {}
        if meta.get('status') != 'running':
            return
        if claim_id is not None and meta.get('timer_claim_id') != claim_id:
            return

        parent_chat_id = meta.get('parent_chat_id') or ''
        prompt_message_id = meta.get('timer_task_message_id')
        prompt_message = await Chats.get_message_by_id_and_message_id(timer_id, prompt_message_id)
        if not prompt_message:
            await _set_timer_state(timer_id, 'error', claim_id=claim_id, timer_error='timer task message is missing')
            return

        user = await Users.get_user_by_id(timer.user_id)
        if not user:
            await _set_timer_state(timer_id, 'error', claim_id=claim_id, timer_error='timer user no longer exists')
            return

        # Re-gate the rehydrated owner: a demoted owner must not run.
        if user.role not in VERIFIED_USER_ROLES:
            await _set_timer_state(
                timer_id, 'error', claim_id=claim_id, timer_error='owner no longer permitted to run timers'
            )
            return

        run = meta.get('run') or {}
        model_id = run.get('model_id') or meta.get('timer_model_id')
        if not model_id:
            await _set_timer_state(timer_id, 'error', claim_id=claim_id, timer_error='model context is missing')
            return

        prompt = prompt_message.get('content') or ''
        if isinstance(prompt, list):
            prompt = ''.join(
                str(part.get('text', '')) for part in prompt if isinstance(part, dict) and part.get('type') == 'text'
            )

        user_message_id = str(uuid4())
        assistant_message_id = str(uuid4())
        user_message = None
        assistant_message = None
        message_list = []
        try:
            try:
                parent_lock = _parent_locks.setdefault(parent_chat_id, asyncio.Lock())
                if parent_lock.locked() or await has_active_tasks(app.state.redis, parent_chat_id):
                    await rearm_due_timer(timer_id, claim_id)
                    return
                async with parent_lock:
                    async with get_async_db() as db:
                        # Lock an ID-only projection; blocked workers never decode parent JSON.
                        stmt = select(Chat.id).where(Chat.id == parent_chat_id, Chat.user_id == timer.user_id)
                        if db.bind.dialect.name == 'postgresql':
                            stmt = stmt.with_for_update(skip_locked=True)
                        result = await db.execute(stmt)
                        if result.scalar_one_or_none() is None:
                            exists = await db.execute(
                                select(Chat.id).where(Chat.id == parent_chat_id, Chat.user_id == timer.user_id)
                            )
                            if exists.scalar_one_or_none() is not None:
                                raise _TimerParentBusy
                            await _set_timer_state(
                                timer_id, 'error', claim_id=claim_id, timer_error='parent chat no longer exists'
                            )
                            return
                        if await has_active_tasks(app.state.redis, parent_chat_id):
                            raise _TimerParentBusy
                        # Recheck the claim under lock before publishing anything to the parent.
                        timer_row = await db.get(Chat, timer_id, with_for_update=True)
                        current_meta = (timer_row.meta or {}) if timer_row else {}
                        if current_meta.get('status') != 'running' or (
                            claim_id is not None and current_meta.get('timer_claim_id') != claim_id
                        ):
                            return
                        parent = await db.get(Chat, parent_chat_id)

                        parent_chat = copy.deepcopy(parent.chat or {})
                        history = parent_chat.setdefault('history', {})
                        messages = history.setdefault('messages', {})
                        done_assistants = [
                            message
                            for message in messages.values()
                            if message.get('role') == 'assistant' and message.get('done') is not False
                        ]
                        parent_id = (
                            max(done_assistants, key=lambda message: message.get('timestamp', 0)).get('id')
                            if done_assistants
                            else meta.get('parent_message_id')
                        )
                        message_list = get_message_list(messages, parent_id)

                        user_message = {
                            'id': user_message_id,
                            'parentId': parent_id,
                            'childrenIds': [assistant_message_id],
                            'role': 'user',
                            'content': prompt,
                            'model': model_id,
                            'meta': {'internal': True, 'type': 'timer', 'timer_id': timer_id},
                            'timestamp': int(time.time()),
                        }
                        assistant_message = {
                            'id': assistant_message_id,
                            'parentId': user_message_id,
                            'childrenIds': [],
                            'role': 'assistant',
                            'content': '',
                            'done': False,
                            'model': model_id,
                            'timestamp': int(time.time()),
                        }
                        messages[user_message_id] = user_message
                        messages[assistant_message_id] = assistant_message
                        if parent_id and parent_id in messages:
                            children = messages[parent_id].setdefault('childrenIds', [])
                            if user_message_id not in children:
                                children.append(user_message_id)

                        parent.chat = parent_chat
                        history['currentId'] = assistant_message_id
                        parent.current_message_id = assistant_message_id
                        parent.updated_at = int(time.time())
                        timer_row = await db.get(Chat, timer_id)
                        if timer_row:
                            timer_row.meta = {
                                **(timer_row.meta or {}),
                                'status': 'completed',
                                'timer_completed_at': int(time.time_ns()),
                                'timer_response_message_id': assistant_message_id,
                            }
                            timer_row.updated_at = int(time.time())
                        await ChatMessages.upsert_message(
                            user_message_id, parent_chat_id, timer.user_id, user_message, db=db
                        )
                        await ChatMessages.upsert_message(
                            assistant_message_id, parent_chat_id, timer.user_id, assistant_message, db=db
                        )
                        await db.commit()

            except _TimerParentBusy:
                await rearm_due_timer(timer_id, claim_id)
                return

            await sio.emit(
                'events',
                {
                    'chat_id': parent_chat_id,
                    'message_id': assistant_message_id,
                    'data': {'type': 'chat:reload'},
                },
                room=f'user:{timer.user_id}',
            )
            chat_context = copy.deepcopy(run.get('chat_context') or {})
            # Timers saved before chat_context was introduced only have the assembled prompt.
            if run.get('chat_context') is None and run.get('system_prompt'):
                chat_context['messages'] = [
                    {
                        'role': 'system',
                        'content': strip_tool_approval_prompt(strip_deferred_tools_manifest(run['system_prompt'])),
                    }
                ]
            form_data = {
                'model': model_id,
                'params': chat_context.get('params') or {},
                'messages': [
                    *chat_context.get('messages', []),
                    *message_list,
                    {'role': 'user', 'content': prompt},
                ],
                'stream': True,
                'chat_id': parent_chat_id,
                'id': assistant_message_id,
                'parent_id': user_message.get('parentId'),
                'user_message': user_message,
                'session_id': run.get('session_id') or f'timer:{parent_chat_id}',
                'background_tasks': {},
                'tool_ids': run.get('tool_ids') or [],
                'skill_ids': run.get('skill_ids') or [],
                'filter_ids': run.get('filter_ids') or [],
                'features': run.get('features') or {},
                'files': run.get('files') or [],
                'variables': run.get('variables') or {},
                'chat_variables': chat_context.get('chat_variables'),
            }
            if run.get('terminal_id'):
                form_data['terminal_id'] = run['terminal_id']
            request = Request(
                {
                    'type': 'http',
                    'asgi': {'version': '3.0', 'spec_version': '2.0'},
                    'method': 'POST',
                    'path': '/api/v1/timers/internal',
                    'query_string': b'',
                    'headers': Headers({}).raw,
                    'client': ('', 0),
                    'server': ('127.0.0.1', 80),
                    'scheme': 'http',
                    'app': app,
                }
            )
            request.state.token = None
            request.state.enable_api_keys = False
            request.state.chat_context = chat_context
            response = await app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
            return response.get('task_ids', []) if isinstance(response, dict) else []
        except (asyncio.CancelledError, Exception) as exc:
            log.exception('Timer %s dispatch interrupted', timer_id)
            detail = (
                'Timer dispatch was interrupted before a response could start.'
                if isinstance(exc, asyncio.CancelledError)
                else exc.detail
                if isinstance(exc, HTTPException)
                else str(exc)
            )
            recovery = asyncio.create_task(
                _resolve_timer_dispatch_failure(
                    app, timer_id, claim_id, parent_chat_id, assistant_message_id, str(detail)[:500]
                )
            )
            while True:
                try:
                    return await asyncio.shield(recovery)
                except asyncio.CancelledError:
                    if recovery.done():
                        return recovery.result()


async def _resolve_timer_dispatch_failure(
    app,
    timer_id: str,
    claim_id: str | None,
    parent_chat_id: str,
    assistant_message_id: str,
    detail: str,
) -> list[str]:
    """Requeue unpublished work or visibly fail its saved turn; never replay it."""
    from open_webui.socket.main import sio
    from open_webui.tasks import tasks

    async with get_async_db() as db:
        result = await db.execute(select(Chat.meta).where(Chat.id == timer_id))
        meta = result.scalar_one_or_none() or {}
    if claim_id is not None and meta.get('timer_claim_id') != claim_id:
        return []
    if meta.get('status') == 'running':
        await rearm_due_timer(timer_id, claim_id)
        return []
    if meta.get('status') != 'completed' or meta.get('timer_response_message_id') != assistant_message_id:
        return []

    # main records the task ID before starting the local response. If setup raised
    # after that handoff, preserve the response and keep its scheduler capacity.
    message = await ChatMessages.get_message_by_id(f'{parent_chat_id}-{assistant_message_id}')
    task_id = (message.meta or {}).get('task_id') if message else None
    if task_id in tasks:
        return [task_id]
    if message and message.done is True:
        return []

    async with get_async_db() as db:
        parent_stmt = select(Chat.id).where(Chat.id == parent_chat_id)
        if db.bind.dialect.name == 'postgresql':
            parent_stmt = parent_stmt.with_for_update()
        parent_id = (await db.execute(parent_stmt)).scalar_one_or_none()
        if parent_id is None:
            await db.rollback()
            await _set_timer_state(
                timer_id,
                'error',
                claim_id=claim_id,
                expected_status='completed',
                response_message_id=assistant_message_id,
                timer_error=detail,
            )
            return []
        timer = await db.get(Chat, timer_id, with_for_update=True)
        current = (timer.meta or {}) if timer else {}
        if (
            current.get('status') != 'completed'
            or current.get('timer_response_message_id') != assistant_message_id
            or (claim_id is not None and current.get('timer_claim_id') != claim_id)
        ):
            return []
        message = await ChatMessages.get_message_by_id(f'{parent_chat_id}-{assistant_message_id}', db=db)
        task_id = (message.meta or {}).get('task_id') if message else None
        if task_id in tasks:
            return [task_id]
        if message and message.done is True:
            return []
        parent = await db.get(Chat, parent_chat_id)
        if parent.user_id != timer.user_id:
            return []
        parent_chat = copy.deepcopy(parent.chat or {})
        saved = (parent_chat.get('history') or {}).get('messages', {}).get(assistant_message_id)
        if saved is None:
            raise RuntimeError('Published timer response is missing from the parent history')
        saved.update({'done': True, 'error': {'content': detail}})
        saved['meta'] = {**(saved.get('meta') or {}), 'response_ended_at': time.time()}
        parent.chat = parent_chat
        parent.updated_at = int(time.time())
        timer.meta = {**current, 'status': 'error', 'timer_error': detail}
        timer.updated_at = int(time.time())
        await ChatMessages.upsert_message(assistant_message_id, parent_chat_id, timer.user_id, saved, db=db)
        await db.commit()
    try:
        await sio.emit(
            'events',
            {
                'chat_id': parent_chat_id,
                'message_id': assistant_message_id,
                'data': {'type': 'chat:message:error', 'data': {'error': {'content': detail}, 'done': True}},
            },
            room=f'user:{timer.user_id}',
        )
    except Exception:
        log.exception('Could not emit the saved timer failure for %s', timer_id)
    return []


async def _set_timer_state(
    timer_id: str,
    status: str,
    *,
    claim_id: str | None = None,
    expected_status: str = 'running',
    response_message_id: str | None = None,
    **fields,
) -> bool:
    """Only the current claim may make its expected state transition."""
    async with get_async_db() as db:
        condition = [Chat.id == timer_id, Chat.meta['status'].as_string() == expected_status]
        if claim_id is not None:
            condition.append(Chat.meta['timer_claim_id'].as_string() == claim_id)
        if response_message_id is not None:
            condition.append(Chat.meta['timer_response_message_id'].as_string() == response_message_id)
        result = await db.execute(select(Chat.meta).where(*condition))
        meta = result.scalar_one_or_none()
        if meta is None:
            return False
        changed = await db.execute(
            update(Chat)
            .where(*condition)
            .values(meta={**meta, 'status': status, **fields}, updated_at=int(time.time()))
        )
        await db.commit()
        return bool(changed.rowcount)
