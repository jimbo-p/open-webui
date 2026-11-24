"""Shrink chats saved before the saved-chat bounds existed (one-off, PostgreSQL only).

The bounds heal a chat the first time it is saved again, but that first load of an oversized chat still needs
several GB. This applies the same bounds ahead of time, one chat at a time.

Dry run by default: lists the chats stored above --min-mb (compressed size, as PostgreSQL stores them) with the size
before and after, by id only; nothing is written. --apply needs --backup-dir: each chat's original JSON and the
original columns of the chat_message rows it changes are written there (gzip, one file per chat, fsynced) before the
chat is rewritten under its row lock. --restore FILE puts one backup back. updated_at is left alone, so chat lists keep
their order. Run it inside the Open WebUI image that carries the bounds, with DATABASE_URL pointing at the writer:

    python -m open_webui.utils.chat_bounds_repair --min-mb 8
    python -m open_webui.utils.chat_bounds_repair --min-mb 8 --apply --backup-dir /data/chat-bounds-backup
    python -m open_webui.utils.chat_bounds_repair --restore /data/chat-bounds-backup/<chat_id>.json.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys

# The app's import-time schema migrations must never run from this tool: they would wait on its own row locks.
os.environ.setdefault('ENABLE_DB_MIGRATIONS', 'False')

from open_webui.utils.chat_bounds import bound_chat_with_changes, bound_messages_map  # noqa: E402

ROW_COLUMNS = ('output', 'sources', 'files')


def _dumps(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def rows_to_messages(chat_id: str, rows: list[tuple]) -> dict[str, dict]:
    """chat_message rows (id, parent_id, role, output, sources, files, context_summary, created_at, done) as a map."""
    prefix = f'{chat_id}-'
    messages = {}
    for row_id, parent_id, role, output, sources, files, context_summary, created_at, *rest in rows:
        message_id = row_id[len(prefix) :] if row_id.startswith(prefix) else row_id
        message = {'id': message_id, 'parentId': parent_id, 'role': role, 'timestamp': created_at or 0}
        if rest and rest[0] is False:
            message['done'] = False  # a reply still being written is never shortened for the chat budget
        for key, value in (('output', output), ('sources', sources), ('files', files)):
            if value is not None:
                message[key] = json.loads(value) if isinstance(value, str) else value
        if context_summary:
            message['contextSummary'] = context_summary
        messages[message_id] = message
    return messages


def plan(chat: dict, row_messages: dict[str, dict], current_id: str | None) -> tuple[dict | None, dict[str, dict]]:
    """The bounded chat JSON (None when unchanged) and, per message id, the chat_message columns that change."""
    bounded_chat, _ = bound_chat_with_changes(chat)
    history = chat.get('history') if isinstance(chat.get('history'), dict) else {}
    current_id = current_id or history.get('currentId')
    bounded_rows, changed = bound_messages_map(row_messages, current_id)
    updates = {}
    for message_id in changed:
        before, after = row_messages[message_id], bounded_rows[message_id]
        columns = {key: after.get(key) for key in ROW_COLUMNS if after.get(key) != before.get(key)}
        if columns:
            updates[message_id] = columns
    return (None if bounded_chat is chat else bounded_chat), updates


def _connect():
    import psycopg

    url = os.environ.get('DATABASE_URL') or ''
    url = re.sub(r'^postgres(ql)?\+[a-z0-9_]+://', 'postgresql://', url)
    if not url.startswith(('postgresql://', 'postgres://')):
        sys.exit('DATABASE_URL must point at the PostgreSQL writer')
    connection = psycopg.connect(url, application_name='open-webui-chat-bounds-repair')
    connection.execute("SET statement_timeout = '600s'")
    connection.execute("SET lock_timeout = '5s'")
    connection.commit()
    return connection


def _backup_path(directory: str, chat_id: str) -> str:
    return os.path.join(directory, f'{re.sub(r"[^A-Za-z0-9_.-]", "_", chat_id)}.json.gz')


def repair(args) -> int:
    # Load what the chat budget imports lazily now, before any row is locked.
    import open_webui.utils.context_compaction  # noqa: F401

    connection = _connect()
    min_bytes = int(args.min_mb * 1024 * 1024)
    if args.chat_id:
        candidates = connection.execute(
            'SELECT id, pg_column_size(chat) FROM chat WHERE id = ANY(%s) ORDER BY 2 DESC', (args.chat_id,)
        ).fetchall()
    else:
        candidates = connection.execute(
            'SELECT id, pg_column_size(chat) FROM chat WHERE pg_column_size(chat) > %s ORDER BY 2 DESC LIMIT %s',
            (min_bytes, args.limit),
        ).fetchall()
    connection.commit()
    print(f'{len(candidates)} chats to check (stored above {args.min_mb} MB)', flush=True)
    totals = {'before': 0, 'after': 0, 'chats': 0, 'rows': 0}
    for chat_id, stored_bytes in candidates:
        try:
            with connection.transaction():
                found = connection.execute(
                    'SELECT chat::text, current_message_id FROM chat WHERE id = %s FOR UPDATE', (chat_id,)
                ).fetchone()
                if not found:
                    continue
                chat_text, current_id = found
                rows = connection.execute(
                    'SELECT id, parent_id, role, output::text, sources::text, files::text, context_summary, '
                    'created_at, done FROM chat_message WHERE chat_id = %s',
                    (chat_id,),
                ).fetchall()
                chat = json.loads(chat_text)
                row_messages = rows_to_messages(chat_id, rows)
                bounded_chat, row_updates = plan(chat, row_messages, current_id)
                bounded_text = _dumps(bounded_chat) if bounded_chat is not None else chat_text
                row_before = sum(len(row[3] or '') + len(row[4] or '') + len(row[5] or '') for row in rows)
                row_after = row_before - sum(
                    sum(len(_dumps(row_messages[mid].get(key)) or '') for key in columns)
                    - sum(len(_dumps(value) or '') for value in columns.values())
                    for mid, columns in row_updates.items()
                )
                print(
                    f'{chat_id}: stored {stored_bytes / 1048576:.1f} MB; chat JSON {len(chat_text) / 1048576:.1f} -> '
                    f'{len(bounded_text) / 1048576:.1f} MB; chat_message {row_before / 1048576:.1f} -> '
                    f'{row_after / 1048576:.1f} MB in {len(row_updates)} rows',
                    flush=True,
                )
                totals['before'] += len(chat_text) + row_before
                totals['after'] += len(bounded_text) + row_after
                if bounded_chat is None and not row_updates:
                    continue
                totals['chats'] += 1
                totals['rows'] += len(row_updates)
                if not args.apply:
                    continue
                backup = {
                    'chat_id': chat_id,
                    'chat': chat_text,
                    'rows': {
                        f'{chat_id}-{mid}': {key: _dumps(row_messages[mid].get(key)) for key in columns}
                        for mid, columns in row_updates.items()
                    },
                }
                path = _backup_path(args.backup_dir, chat_id)
                with gzip.open(path, 'wt', encoding='utf-8') as handle:
                    json.dump(backup, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                if bounded_chat is not None:
                    connection.execute('UPDATE chat SET chat = %s::json WHERE id = %s', (bounded_text, chat_id))
                for mid, columns in row_updates.items():
                    assignments = ', '.join(f'{key} = %s::json' for key in columns)
                    connection.execute(
                        f'UPDATE chat_message SET {assignments} WHERE id = %s',
                        (*[_dumps(value) for value in columns.values()], f'{chat_id}-{mid}'),
                    )
        except Exception as error:  # a locked or unreadable chat is skipped, never half-written
            print(f'{chat_id}: skipped ({type(error).__name__}: {error})', flush=True)
            continue
    verb = 'shrunk' if args.apply else 'would shrink'
    print(
        f'{verb} {totals["chats"]} chats and {totals["rows"]} chat_message rows: '
        f'{totals["before"] / 1048576:.1f} -> {totals["after"] / 1048576:.1f} MB',
        flush=True,
    )
    return 0


def restore(path: str) -> int:
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        backup = json.load(handle)
    connection = _connect()
    with connection.transaction():
        connection.execute('SELECT 1 FROM chat WHERE id = %s FOR UPDATE', (backup['chat_id'],))
        connection.execute('UPDATE chat SET chat = %s::json WHERE id = %s', (backup['chat'], backup['chat_id']))
        for row_id, columns in backup['rows'].items():
            assignments = ', '.join(f'{key} = %s::json' for key in columns)
            connection.execute(f'UPDATE chat_message SET {assignments} WHERE id = %s', (*columns.values(), row_id))
    print(f'restored {backup["chat_id"]} and {len(backup["rows"])} chat_message rows')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--min-mb', type=float, default=8.0, help='stored (compressed) chat size to check above')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--chat-id', action='append', help='check only these chats (repeatable)')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir')
    parser.add_argument('--restore', metavar='FILE')
    args = parser.parse_args(argv)
    if args.restore:
        return restore(args.restore)
    if args.apply:
        if not args.backup_dir:
            parser.error('--apply needs --backup-dir')
        os.makedirs(args.backup_dir, exist_ok=True)
    return repair(args)


if __name__ == '__main__':
    sys.exit(main())
