"""Attest an authenticated Open WebUI chat's outgoing SQL tool request."""

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from uuid import UUID


SQL_CHAT_TOOL_CONTEXT_URL = os.getenv('OXY_SQL_CHAT_TOOL_CONTEXT_URL', '')
SQL_CHAT_TOOL_CONTEXT_HEADER = 'X-Oxy-Chat-Tool-Context'


@dataclass(frozen=True)
class SQLChatToolContext:
    user_id: str
    chat_id: str
    message_id: str


def _is_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def sql_chat_tool_context(user_id, metadata, message, owner_verified, session_owner):
    """Build context from the persisted assistant turn and the authenticated UI session."""
    if (
        not SQL_CHAT_TOOL_CONTEXT_URL
        or not owner_verified
        or metadata.get('automation_id')
        or metadata.get('internal')
        or metadata.get('direct')
        or not message
        or message.get('role') != 'assistant'
        or (message.get('user_id') or user_id) != user_id
    ):
        return None
    context = SQLChatToolContext(user_id, metadata.get('chat_id'), metadata.get('message_id'))
    if not all(_is_uuid(value) for value in (context.user_id, context.chat_id, context.message_id)):
        return None
    session_id = metadata.get('session_id')
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        return context if session_owner(session_id) == user_id else None
    except Exception:
        # If UI session ownership is unavailable, SQL uses the standard strict mode.
        return None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')


def _base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=')


def sign_sql_chat_tool_context(context, body, secret):
    if not secret or body.get('include_rows', False) is not False:
        return None
    try:
        body_hash = hashlib.sha256(_canonical(body)).hexdigest()
    except (TypeError, ValueError):
        return None
    issued = int(time.time())
    payload = _base64url(
        _canonical(
            {
                'v': 1,
                'purpose': 'sql-chat-ui',
                'user_id': context.user_id,
                'chat_id': context.chat_id,
                'message_id': context.message_id,
                'iat': issued,
                'exp': issued + 60,
                'args_hash': body_hash,
            }
        )
    )
    signature = hmac.digest(secret.encode('utf-8'), b'oxy-sql-chat-ui-v1.' + payload, 'sha256')
    return (payload + b'.' + _base64url(signature)).decode('ascii')
