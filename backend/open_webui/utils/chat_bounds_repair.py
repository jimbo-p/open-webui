"""Shrink chats saved before the saved-chat bounds existed (one-off, PostgreSQL only).

The bounds heal a chat the first time it is saved again, but that first load of an oversized chat still needs
several GB. This applies the same bounds ahead of time, one chat at a time.

Dry run by default: it lists the chats whose chat JSON or chat_message rows are stored above --min-mb (compressed, as
PostgreSQL stores them) with the size before and after, by id only. It takes no lock and writes nothing.

--apply needs --backup-dir. For each chat that changes, the original chat JSON and the original columns of the rows
that change are streamed, as the exact bytes PostgreSQL holds, into a new backup: a gzip data file and a small JSON
manifest, written under temporary names, fsynced and then published without ever replacing an existing file. Reading,
planning and the backup take no lock (one REPEATABLE READ snapshot); the chat is then locked only to check that it and
the rows to change are as they were read, and to write them. A chat that changed meanwhile is skipped. updated_at is
left alone, so chat lists keep their order. --restore MANIFEST puts one backup back and refuses, unless --force, when
the chat or those rows changed after the repair, since that would drop the newer work.

Memory: one chat at a time. The chat is read message by message and tool result by tool result, and the backup is
streamed in slices, so the peak is about the parsed size of the chat (about 1.3x its stored text, measured) on top of
the interpreter. A text holding even one character outside the Basic Multilingual Plane (an emoji, for example) is
held by Python at 4 bytes per character, so a chat whose large results all contain one can need about 5x. Run it as
its own task or on a host with at least 5x the largest chat free (dry-run first: it lists the sizes), never inside a
serving Open WebUI task; --max-mb leaves larger chats for a bigger host. It exits non-zero when any chat failed or
changed while being repaired. Set DATABASE_URL to the writer:

    python -m open_webui.utils.chat_bounds_repair --min-mb 8
    python -m open_webui.utils.chat_bounds_repair --min-mb 8 --apply --backup-dir /data/chat-bounds-backup
    python -m open_webui.utils.chat_bounds_repair --restore /data/chat-bounds-backup/<chat_id>-<run>.manifest.json
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import struct
import sys
import time

# The app's import-time schema migrations must never run from this tool: they would wait on its own row locks.
os.environ.setdefault('ENABLE_DB_MIGRATIONS', 'False')

from open_webui.utils.chat_bounds import bound_chat_with_changes, bound_messages_map  # noqa: E402

FORMAT = 'open-webui-chat-bounds-backup/2'
ROW_COLUMNS = ('output', 'sources', 'files')
ARRAY_FIELDS = ('output', 'sources')
NULL_MD5 = 'null'
COPY_SIGNATURE = b'PGCOPY\n\xff\r\n\x00'


def _dumps(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _md5(data: bytes | None) -> str:
    return NULL_MD5 if data is None else hashlib.md5(data).hexdigest()


def _loads(value):
    return json.loads(value) if isinstance(value, (str, bytes, bytearray, memoryview)) else value


def rows_to_messages(chat_id: str, rows: list[tuple]) -> dict[str, dict]:
    """chat_message rows (id, parent_id, role, output, sources, files, context_summary, created_at[, done]) as a map.

    The JSON columns may be parsed values, str, or the raw UTF-8 bytes PostgreSQL returned.
    """
    prefix = f'{chat_id}-'
    messages = {}
    for row_id, parent_id, role, output, sources, files, context_summary, created_at, *rest in rows:
        message_id = row_id[len(prefix) :] if row_id.startswith(prefix) else row_id
        message = {'id': message_id, 'parentId': parent_id, 'role': role, 'timestamp': created_at or 0}
        if rest and rest[0] is False:
            message['done'] = False  # a reply still being written is never shortened for the chat budget
        for key, value in (('output', output), ('sources', sources), ('files', files)):
            if value is not None:
                message[key] = _loads(value)
        if context_summary:
            message['contextSummary'] = context_summary
        messages[message_id] = message
    return messages


def plan_chat(chat: dict) -> dict | None:
    """The bounded chat JSON, or None when it is already within the bounds."""
    bounded_chat, _ = bound_chat_with_changes(chat)
    return None if bounded_chat is chat else bounded_chat


def plan_rows(row_messages: dict[str, dict], current_id: str | None) -> dict[str, dict]:
    """Per message id, the chat_message columns that change."""
    bounded_rows, changed = bound_messages_map(row_messages, current_id)
    updates = {}
    for message_id in changed:
        before, after = row_messages[message_id], bounded_rows[message_id]
        columns = {key: after.get(key) for key in ROW_COLUMNS if after.get(key) != before.get(key)}
        if columns:
            updates[message_id] = columns
    return updates


def plan(chat: dict, row_messages: dict[str, dict], current_id: str | None) -> tuple[dict | None, dict[str, dict]]:
    history = chat.get('history') if isinstance(chat.get('history'), dict) else {}
    return plan_chat(chat), plan_rows(row_messages, current_id or history.get('currentId'))


def _connect():
    import psycopg

    url = os.environ.get('DATABASE_URL') or ''
    url = re.sub(r'^postgres(ql)?\+[a-z0-9_]+://', 'postgresql://', url)
    if not url.startswith(('postgresql://', 'postgres://')):
        sys.exit('DATABASE_URL must point at the PostgreSQL writer')
    connection = psycopg.connect(url, application_name='open-webui-chat-bounds-repair')
    connection.execute("SET statement_timeout = '900s'")
    connection.execute("SET lock_timeout = '5s'")
    connection.commit()
    return connection


def _stream(connection, query: str, params: tuple, itersize: int = 4):
    """Rows of a query through a server-side cursor, a few at a time (each row may hold one large tool result)."""
    with connection.cursor(name=f'chat_bounds_{time.monotonic_ns()}') as cursor:
        cursor.itersize = itersize
        cursor.execute(query, params)
        yield from cursor


# ---------------------------------------------------------------------------------------------------------------------
# Reading a chat and its rows piece by piece, so no whole multi-hundred-MB text is ever held or decoded at once.

CHAT_TOP = """SELECT key, json_typeof(value),
       CASE WHEN key IN ('history', 'messages') THEN NULL ELSE convert_to(value::text, 'UTF8') END
  FROM json_each((SELECT chat FROM chat WHERE id = %s))"""
HISTORY_TOP = """SELECT key, json_typeof(value),
       CASE WHEN key = 'messages' THEN NULL ELSE convert_to(value::text, 'UTF8') END
  FROM json_each((SELECT chat->'history' FROM chat WHERE id = %s))"""
LIST_ELEMENTS = """SELECT e.ordinality - 1, convert_to(e.value::text, 'UTF8')
  FROM json_array_elements((SELECT chat->'messages' FROM chat WHERE id = %s)) WITH ORDINALITY e(value, ordinality)"""
_MESSAGES = "json_each((SELECT chat->'history'->'messages' FROM chat WHERE id = %s)) m"
_FIELDS = "json_each(CASE WHEN json_typeof(m.value) = 'object' THEN m.value ELSE '{}'::json END) f"
MESSAGE_ODD = f"SELECT m.key, convert_to(m.value::text, 'UTF8') FROM {_MESSAGES} WHERE json_typeof(m.value) <> 'object'"
MESSAGE_FIELDS = f"""SELECT m.key, f.key, json_typeof(f.value),
       CASE WHEN f.key IN ('output', 'sources') AND json_typeof(f.value) = 'array' THEN NULL
            ELSE convert_to(f.value::text, 'UTF8') END
  FROM {_MESSAGES} CROSS JOIN LATERAL {_FIELDS}"""
MESSAGE_ELEMENTS = f"""SELECT m.key, f.key, e.ordinality - 1, convert_to(e.value::text, 'UTF8')
  FROM {_MESSAGES} CROSS JOIN LATERAL {_FIELDS}
  CROSS JOIN LATERAL json_array_elements(
       CASE WHEN f.key IN ('output', 'sources') AND json_typeof(f.value) = 'array' THEN f.value ELSE '[]'::json END
  ) WITH ORDINALITY e(value, ordinality)"""
ROW_SCALARS = """SELECT id, parent_id, role, context_summary, created_at, done,
       json_typeof(output), json_typeof(sources),
       CASE WHEN json_typeof(output) = 'array' THEN NULL ELSE convert_to(output::text, 'UTF8') END,
       CASE WHEN json_typeof(sources) = 'array' THEN NULL ELSE convert_to(sources::text, 'UTF8') END,
       convert_to(files::text, 'UTF8'),
       md5(convert_to(output::text, 'UTF8')), md5(convert_to(sources::text, 'UTF8')),
       md5(convert_to(files::text, 'UTF8'))
  FROM chat_message WHERE chat_id = %s"""
ROW_ELEMENTS = """SELECT id, 'output', e.ordinality - 1, convert_to(e.value::text, 'UTF8')
  FROM chat_message CROSS JOIN LATERAL json_array_elements(
       CASE WHEN json_typeof(output) = 'array' THEN output ELSE '[]'::json END) WITH ORDINALITY e(value, ordinality)
 WHERE chat_id = %s
UNION ALL
SELECT id, 'sources', e.ordinality - 1, convert_to(e.value::text, 'UTF8')
  FROM chat_message CROSS JOIN LATERAL json_array_elements(
       CASE WHEN json_typeof(sources) = 'array' THEN sources ELSE '[]'::json END) WITH ORDINALITY e(value, ordinality)
 WHERE chat_id = %s"""


def _fill(arrays: dict, owner, field, index, data) -> None:
    arrays[(owner, field)][index] = _loads(data)


def _value(connection, chat_id: str, path: str):
    """One JSON value of the chat, parsed from its UTF-8 bytes (used for small or unusual shapes only)."""
    found = connection.execute(f"SELECT convert_to(({path})::text, 'UTF8') FROM chat WHERE id = %s", (chat_id,))
    return _loads(found.fetchone()[0])


def _object(connection, query: str, chat_id: str) -> tuple[dict, dict]:
    """A JSON object's keys with their values (None where the query leaves a large value out) and JSON types."""
    values, kinds = {}, {}
    for key, value_kind, data in connection.execute(query, (chat_id,)):
        kinds[key] = value_kind
        values[key] = _loads(data) if data is not None else None
    return values, kinds


def read_chat(connection, chat_id: str) -> dict:
    """The chat JSON as a dict, read key by key, message by message and array element by element."""
    kind = connection.execute('SELECT json_typeof(chat) FROM chat WHERE id = %s', (chat_id,)).fetchone()
    if kind is None:
        raise LookupError(chat_id)
    if kind[0] != 'object':
        return _value(connection, chat_id, 'chat')
    chat, kinds = _object(connection, CHAT_TOP, chat_id)
    if kinds.get('messages') == 'array':
        listed = {index: _loads(data) for index, data in _stream(connection, LIST_ELEMENTS, (chat_id,))}
        chat['messages'] = [listed[index] for index in sorted(listed)]
    elif 'messages' in kinds:
        chat['messages'] = _value(connection, chat_id, "chat->'messages'")
    if kinds.get('history') == 'object':
        history, history_kinds = _object(connection, HISTORY_TOP, chat_id)
        if history_kinds.get('messages') == 'object':
            history['messages'] = _read_messages(connection, chat_id)
        elif 'messages' in history_kinds:
            history['messages'] = _value(connection, chat_id, "chat->'history'->'messages'")
        chat['history'] = history
    elif 'history' in kinds:
        chat['history'] = _value(connection, chat_id, "chat->'history'")
    return chat


def _read_messages(connection, chat_id: str) -> dict:
    messages: dict = {}
    arrays: dict = {}
    for key, data in connection.execute(MESSAGE_ODD, (chat_id,)):
        messages[key] = _loads(data)
    for message_key, field, value_kind, data in _stream(connection, MESSAGE_FIELDS, (chat_id,), itersize=64):
        message = messages.setdefault(message_key, {})
        if data is None and field in ARRAY_FIELDS and value_kind == 'array':
            arrays[(message_key, field)] = {}
            message[field] = None
        else:
            message[field] = _loads(data)
    for message_key, field, index, data in _stream(connection, MESSAGE_ELEMENTS, (chat_id,)):
        _fill(arrays, message_key, field, index, data)
    for (message_key, field), elements in arrays.items():
        messages[message_key][field] = [elements[index] for index in sorted(elements)]
    return messages


def read_rows(connection, chat_id: str) -> tuple[dict[str, dict], dict[str, tuple]]:
    """The chat's chat_message rows as a message map, and per row id the md5 of its output, sources and files."""
    rows, arrays, digests = [], {}, {}
    for (
        row_id, parent_id, role, context_summary, created_at, done, output_kind, sources_kind,
        output, sources, files, output_md5, sources_md5, files_md5,
    ) in _stream(connection, ROW_SCALARS, (chat_id,), itersize=16):  # fmt: skip
        if output_kind == 'array':
            arrays[(row_id, 'output')] = {}
            output = []
        if sources_kind == 'array':
            arrays[(row_id, 'sources')] = {}
            sources = []
        rows.append((row_id, parent_id, role, output, sources, files, context_summary, created_at, done))
        digests[row_id] = tuple(value or NULL_MD5 for value in (output_md5, sources_md5, files_md5))
    for row_id, field, index, data in _stream(connection, ROW_ELEMENTS, (chat_id, chat_id)):
        _fill(arrays, row_id, field, index, data)
    messages = rows_to_messages(chat_id, rows)
    prefix = f'{chat_id}-'
    for (row_id, field), elements in arrays.items():
        message_id = row_id[len(prefix) :] if row_id.startswith(prefix) else row_id
        messages[message_id][field] = [elements[index] for index in sorted(elements)]
    return messages, digests


# ---------------------------------------------------------------------------------------------------------------------
# Backups: raw bytes streamed through COPY BINARY into gzip, never re-escaped, never overwriting an existing file.


SLICE_BYTES = 4 << 20


def _stream_value(connection, select_sql, write) -> tuple[bool, int, str]:
    """Pass the single bytea value of ``select_sql`` (a composed one-row, one-column query) to ``write`` in slices.

    The value is staged in a temporary table with uncompressed out-of-line storage, so each slice reads only its own
    part; neither the whole value nor a COPY message of its size is ever held here. Returns (is_null, bytes, md5).
    """
    from psycopg import sql

    connection.execute('CREATE TEMPORARY TABLE IF NOT EXISTS chat_bounds_value (v bytea) ON COMMIT DROP')
    connection.execute('ALTER TABLE chat_bounds_value ALTER COLUMN v SET STORAGE EXTERNAL')
    connection.execute('TRUNCATE chat_bounds_value')
    connection.execute(sql.SQL('INSERT INTO chat_bounds_value (v) {}').format(select_sql))
    found = connection.execute('SELECT octet_length(v) FROM chat_bounds_value').fetchall()
    if len(found) != 1:
        raise ValueError('expected exactly one value to back up')
    length = found[0][0]
    if length is None:
        return True, 0, NULL_MD5
    digest = hashlib.md5()
    for offset in range(0, length, SLICE_BYTES):
        part = connection.execute(
            'SELECT substring(v FROM %s FOR %s) FROM chat_bounds_value', (offset + 1, SLICE_BYTES)
        ).fetchone()[0]
        part = bytes(part)
        write(part)
        digest.update(part)
    connection.execute('TRUNCATE chat_bounds_value')
    return False, length, digest.hexdigest()


def _safe(chat_id: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]', '_', chat_id)


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(temp: str, final: str) -> None:
    """Give a finished, fsynced file its final name, never replacing an existing file."""
    os.link(temp, final)  # FileExistsError rather than an overwrite
    os.unlink(temp)


class Backup:
    """A data file (gzip of raw sections) and its manifest, written to temporary names and published together."""

    def __init__(self, directory: str, chat_id: str, run_id: str):
        self.directory = directory
        base = f'{_safe(chat_id)}-{run_id}'
        self.data_name = f'{base}.data.gz'
        self.manifest_name = f'{base}.manifest.json'
        self.temp_data = os.path.join(directory, f'.{self.data_name}.tmp')
        self.sections: list[dict] = []
        self.sha256 = hashlib.sha256()
        self._raw = open(self.temp_data, 'xb')  # noqa: SIM115 - closed in finish() or discard()
        self._gzip = gzip.GzipFile(fileobj=self._raw, mode='wb', compresslevel=1)

    def add(self, connection, section: dict, select_sql) -> str:
        """Stream one value into the data file; return its md5."""
        is_null, length, digest = _stream_value(connection, select_sql, self._gzip.write)
        self.sections.append({**section, 'null': is_null, 'bytes': length, 'md5': digest})
        return digest

    def finish(self, manifest: dict) -> str:
        self._gzip.close()  # writes the final block and the gzip trailer before the file is synced
        self._raw.flush()
        os.fsync(self._raw.fileno())
        self._raw.close()
        with open(self.temp_data, 'rb') as handle:
            for block in iter(lambda: handle.read(1 << 20), b''):
                self.sha256.update(block)
        _publish(self.temp_data, os.path.join(self.directory, self.data_name))
        body = {
            **manifest,
            'format': FORMAT,
            'data_file': self.data_name,
            'data_sha256': self.sha256.hexdigest(),
            'sections': self.sections,
        }
        temp_manifest = os.path.join(self.directory, f'.{self.manifest_name}.tmp')
        with open(temp_manifest, 'x', encoding='utf-8') as handle:
            json.dump(body, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        final = os.path.join(self.directory, self.manifest_name)
        _publish(temp_manifest, final)
        _fsync_dir(self.directory)
        return final

    def discard(self) -> None:
        try:
            self._gzip.close()
            self._raw.close()
        finally:
            if os.path.exists(self.temp_data):
                os.unlink(self.temp_data)


# ---------------------------------------------------------------------------------------------------------------------


def _candidates(connection, args) -> list[tuple[str, int, int]]:
    """(chat id, stored chat bytes, stored row bytes), largest first."""
    if args.chat_id:
        ids = list(args.chat_id)
    else:
        min_bytes = int(args.min_mb * 1024 * 1024)
        ids = [
            row[0]
            for row in connection.execute(
                'SELECT id FROM chat WHERE pg_column_size(chat) > %s '
                'UNION SELECT chat_id FROM chat_message GROUP BY chat_id HAVING sum('
                'coalesce(pg_column_size(output), 0) + coalesce(pg_column_size(sources), 0) + '
                'coalesce(pg_column_size(files), 0)) > %s',
                (min_bytes, min_bytes),
            ).fetchall()
        ]
    if not ids:
        connection.commit()
        return []
    found = connection.execute(
        'SELECT c.id, pg_column_size(c.chat), coalesce((SELECT sum(coalesce(pg_column_size(m.output), 0) + '
        'coalesce(pg_column_size(m.sources), 0) + coalesce(pg_column_size(m.files), 0)) FROM chat_message m '
        'WHERE m.chat_id = c.id), 0) FROM chat c WHERE c.id = ANY(%s)',
        (ids,),
    ).fetchall()
    connection.commit()
    return sorted(((i, int(c or 0), int(r or 0)) for i, c, r in found), key=lambda item: -(item[1] + item[2]))


class _Changed(Exception):
    pass


def _check_unchanged(connection, chat_id: str, chat_md5: str, row_digests: dict[str, tuple]) -> None:
    """Under the chat's row lock, raise _Changed unless the chat and the rows to change are as they were read."""
    now = connection.execute(
        "SELECT md5(convert_to(chat::text, 'UTF8')) FROM chat WHERE id = %s FOR UPDATE", (chat_id,)
    ).fetchone()
    if not now or now[0] != chat_md5:
        raise _Changed()
    if not row_digests:
        return
    current = connection.execute(
        "SELECT id, md5(convert_to(output::text, 'UTF8')), md5(convert_to(sources::text, 'UTF8')), "
        "md5(convert_to(files::text, 'UTF8')) FROM chat_message WHERE id = ANY(%s) FOR UPDATE",
        (list(row_digests),),
    ).fetchall()
    if len(current) != len(row_digests):
        raise _Changed()
    for row_id, *digests in current:
        if tuple(value or NULL_MD5 for value in digests) != row_digests[row_id]:
            raise _Changed()


class _Plan:
    def __init__(self, chat_id: str, chat_md5: str, chat_bytes: int, current_id: str | None):
        self.chat_id, self.chat_md5, self.chat_bytes, self.current_id = chat_id, chat_md5, chat_bytes, current_id
        self.bounded_text: bytes | None = None
        self.written_rows: dict[str, dict] = {}
        self.row_digests: dict[str, tuple] = {}
        self.backup: Backup | None = None

    def manifest(self, run_id: str) -> dict:
        return {
            'chat_id': self.chat_id,
            'run_id': run_id,
            'created_at': int(time.time()),
            'current_message_id': self.current_id,
            'read': {'chat_md5': self.chat_md5},
            'written': {
                'chat_md5': _md5(self.bounded_text) if self.bounded_text is not None else self.chat_md5,
                'rows': {
                    row_id: {key: _md5(value) for key, value in columns.items()}
                    for row_id, columns in self.written_rows.items()
                },
            },
        }


def _plan_rows_of(connection, plan: _Plan) -> int:
    """Read the rows, plan them and keep only the encoded new columns; return the characters removed."""
    row_messages, digests = read_rows(connection, plan.chat_id)
    removed = 0
    for message_id, columns in plan_rows(row_messages, plan.current_id).items():
        row_id = f'{plan.chat_id}-{message_id}'
        encoded = {key: (None if value is None else _dumps(value).encode()) for key, value in columns.items()}
        removed += sum(
            len(_dumps(row_messages[message_id].get(key)) or '') - len(value or b'') for key, value in encoded.items()
        )
        plan.written_rows[row_id] = encoded
        plan.row_digests[row_id] = digests[row_id]
    return removed


def _back_up(connection, args, run_id: str, plan: _Plan) -> Backup:
    """Stream the originals into a new backup from the same snapshot the plan was made from."""
    from psycopg import sql

    backup = Backup(args.backup_dir, plan.chat_id, run_id)
    try:
        chat_sql = sql.SQL("SELECT convert_to(chat::text, 'UTF8') FROM chat WHERE id = {}")
        if backup.add(connection, {'kind': 'chat'}, chat_sql.format(sql.Literal(plan.chat_id))) != plan.chat_md5:
            raise _Changed()
        for row_id, columns in plan.written_rows.items():
            for key in columns:
                row_sql = sql.SQL("SELECT convert_to({}::text, 'UTF8') FROM chat_message WHERE id = {}")
                section = {'kind': 'row', 'id': row_id, 'column': key}
                backup.add(connection, section, row_sql.format(sql.Identifier(key), sql.Literal(row_id)))
        return backup
    except BaseException:
        backup.discard()
        raise


def _read_and_plan(connection, args, chat_id: str, run_id: str) -> tuple[str, _Plan | None]:
    """Read and plan in one REPEATABLE READ snapshot without a lock; with --apply, back up from that snapshot."""
    with connection.transaction():
        # A backup stages values in a temporary table, which a READ ONLY transaction would refuse.
        mode = 'REPEATABLE READ' if args.apply else 'REPEATABLE READ, READ ONLY'
        connection.execute(f'SET TRANSACTION ISOLATION LEVEL {mode}')
        found = connection.execute(
            "SELECT md5(convert_to(chat::text, 'UTF8')), octet_length(convert_to(chat::text, 'UTF8')), "
            'current_message_id FROM chat WHERE id = %s',
            (chat_id,),
        ).fetchone()
        if not found:
            return 'unchanged', None
        plan = _Plan(chat_id, *found)
        chat = read_chat(connection, chat_id)
        history = chat.get('history') if isinstance(chat.get('history'), dict) else {}
        plan.current_id = plan.current_id or history.get('currentId')
        bounded_chat = plan_chat(chat)
        del chat, history
        plan.bounded_text = _dumps(bounded_chat).encode() if bounded_chat is not None else None
        del bounded_chat
        removed = _plan_rows_of(connection, plan)
        after_chat = len(plan.bounded_text) if plan.bounded_text is not None else plan.chat_bytes
        print(
            f'{chat_id}: chat JSON {plan.chat_bytes / 1048576:.1f} -> {after_chat / 1048576:.1f} MB; '
            f'chat_message -{removed / 1048576:.1f} MB in {len(plan.written_rows)} rows',
            flush=True,
        )
        if plan.bounded_text is None and not plan.written_rows:
            return 'unchanged', None
        if not args.apply:
            return 'would shrink', None
        plan.backup = _back_up(connection, args, run_id, plan)
    return 'planned', plan


def _write(connection, plan: _Plan) -> None:
    """Lock the chat briefly, check nothing changed since the snapshot, and write the bounded chat and rows."""
    with connection.transaction():
        _check_unchanged(connection, plan.chat_id, plan.chat_md5, plan.row_digests)
        if plan.bounded_text is not None:
            connection.execute(
                "UPDATE chat SET chat = convert_from(%b, 'UTF8')::json WHERE id = %s", (plan.bounded_text, plan.chat_id)
            )
        for row_id, columns in plan.written_rows.items():
            assignments = ', '.join(
                f"{key} = convert_from(%b, 'UTF8')::json" if value is not None else f'{key} = NULL'
                for key, value in columns.items()
            )
            values = [value for value in columns.values() if value is not None]
            connection.execute(f'UPDATE chat_message SET {assignments} WHERE id = %s', (*values, row_id))


def _repair_one(connection, args, chat_id: str, run_id: str) -> str:
    """'shrunk', 'unchanged', 'would shrink' or 'changed' (skipped because it changed while being repaired)."""
    try:
        outcome, plan = _read_and_plan(connection, args, chat_id, run_id)
        if plan is None:
            return outcome
        try:
            manifest_path = plan.backup.finish(plan.manifest(run_id))
        except BaseException:
            plan.backup.discard()
            raise
        _write(connection, plan)
        print(f'{chat_id}: shrunk; backup {manifest_path}', flush=True)
        return 'shrunk'
    except _Changed:
        print(f'{chat_id}: skipped, it changed while being repaired (run again)', flush=True)
        return 'changed'


def repair(args) -> int:
    # Load what the chat budget imports lazily now, before any row is locked.
    import open_webui.utils.context_compaction  # noqa: F401

    connection = _connect()
    run_id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-{os.getpid()}'
    candidates = _candidates(connection, args)
    print(f'{len(candidates)} chats to check (stored above {args.min_mb} MB)', flush=True)
    outcomes: dict[str, list[str]] = {}
    for chat_id, chat_bytes, row_bytes in candidates:
        stored = chat_bytes + row_bytes
        if args.max_mb and stored > args.max_mb * 1024 * 1024:
            print(f'{chat_id}: left for a bigger host, stored {stored / 1048576:.1f} MB is above --max-mb', flush=True)
            outcomes.setdefault('above --max-mb', []).append(chat_id)
            continue
        try:
            outcome = _repair_one(connection, args, chat_id, run_id)
        except Exception as error:  # reported and counted; the next chat is still tried
            try:
                connection.rollback()
            except Exception:
                connection = _connect()
            print(f'{chat_id}: FAILED ({type(error).__name__}: {error})', flush=True)
            outcome = 'failed'
        outcomes.setdefault(outcome, []).append(chat_id)
    summary = ', '.join(f'{len(ids)} {name}' for name, ids in sorted(outcomes.items())) or 'nothing to do'
    print(f'summary: {summary}', flush=True)
    for name in ('failed', 'changed', 'above --max-mb'):
        if outcomes.get(name):
            print(f'{name}: {" ".join(outcomes[name])}', flush=True)
    return 1 if outcomes.get('failed') or outcomes.get('changed') else 0


def _sections(manifest: dict, directory: str):
    """Yield (section, reader) per section of a backup's data file, after checking its checksum."""
    path = os.path.join(directory, manifest['data_file'])
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    if digest.hexdigest() != manifest['data_sha256']:
        raise SystemExit(f'{path} does not match its manifest checksum')
    with gzip.open(path, 'rb') as handle:
        for section in manifest['sections']:
            yield section, handle


def _copy_into_restore_table(connection, handle, length: int) -> str:
    """Stream ``length`` bytes from ``handle`` into the one-row temporary restore table; return their md5."""
    digest = hashlib.md5()
    connection.execute('TRUNCATE chat_bounds_restore')
    with (
        connection.cursor() as cursor,
        cursor.copy('COPY chat_bounds_restore (value) FROM STDIN (FORMAT BINARY)') as copy,
    ):
        copy.write(COPY_SIGNATURE + struct.pack('!II', 0, 0) + struct.pack('!hi', 1, length))
        remaining = length
        while remaining:
            block = handle.read(min(remaining, 1 << 20))
            if not block:
                raise SystemExit('the backup data file ended early')
            digest.update(block)
            copy.write(block)
            remaining -= len(block)
        copy.write(struct.pack('!h', -1))
    return digest.hexdigest()


def _restore_drift(connection, chat_id: str, written: dict) -> list[str] | None:
    """Under the chat's lock, what changed since the repair wrote it; None when the chat no longer exists."""
    now = connection.execute(
        "SELECT md5(convert_to(chat::text, 'UTF8')) FROM chat WHERE id = %s FOR UPDATE", (chat_id,)
    ).fetchone()
    if not now:
        return None
    drift = [] if now[0] == written['chat_md5'] else ['the chat JSON']
    for row_id, columns in written['rows'].items():
        if any(key not in ROW_COLUMNS for key in columns):
            raise SystemExit(f'unexpected columns in the manifest for {row_id}')
        current = connection.execute(
            'SELECT '
            + ', '.join(f"md5(convert_to({key}::text, 'UTF8'))" for key in columns)
            + ' FROM chat_message WHERE id = %s FOR UPDATE',
            (row_id,),
        ).fetchone()
        if not current or [value or NULL_MD5 for value in current] != list(columns.values()):
            drift.append(f'row {row_id}')
    return drift


def _restore_sections(connection, manifest: dict, path: str) -> list[str]:
    """Write every backed-up section back; return the ids that were not found."""
    connection.execute('CREATE TEMPORARY TABLE IF NOT EXISTS chat_bounds_restore (value bytea) ON COMMIT DROP')
    missing = []
    for section, handle in _sections(manifest, os.path.dirname(os.path.abspath(path))):
        if section['kind'] == 'row' and section['column'] not in ROW_COLUMNS:
            raise SystemExit(f'unexpected column {section["column"]!r} in {path}')
        value = 'NULL'
        if not section['null']:
            if _copy_into_restore_table(connection, handle, section['bytes']) != section['md5']:
                raise SystemExit(f'{path}: a section does not match its manifest')
            value = "convert_from((SELECT value FROM chat_bounds_restore), 'UTF8')::json"
        if section['kind'] == 'chat':
            result = connection.execute(f'UPDATE chat SET chat = {value} WHERE id = %s', (manifest['chat_id'],))
        else:
            result = connection.execute(
                f'UPDATE chat_message SET {section["column"]} = {value} WHERE id = %s', (section['id'],)
            )
        if result.rowcount != 1:
            missing.append(section.get('id') or manifest['chat_id'])
    return missing


def restore(path: str, force: bool) -> int:
    with open(path, encoding='utf-8') as handle:
        manifest = json.load(handle)
    if manifest.get('format') != FORMAT:
        raise SystemExit(f'{path} is not a {FORMAT} manifest')
    chat_id = manifest['chat_id']
    connection = _connect()
    with connection.transaction():
        drift = _restore_drift(connection, chat_id, manifest['written'])
        if drift is None:
            print(f'{chat_id}: the chat no longer exists; nothing restored')
            return 1
        if drift and not force:
            print(
                f'{chat_id}: refused, changed since the repair ({", ".join(drift[:5])}); restoring would drop that '
                'newer work. Use --force to overwrite anyway.'
            )
            return 1
        missing = _restore_sections(connection, manifest, path)
    rows = len(manifest['written']['rows'])
    print(f'restored {chat_id} and {rows} chat_message rows' + (' (forced)' if drift else ''))
    if missing:
        print(f'not found while restoring: {" ".join(sorted(set(missing)))}')
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--min-mb', type=float, default=8.0, help='stored (compressed) size to check above')
    parser.add_argument('--max-mb', type=float, default=0, help='leave chats stored above this for a bigger host')
    parser.add_argument('--chat-id', action='append', help='check only these chats (repeatable)')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir')
    parser.add_argument('--restore', metavar='MANIFEST')
    parser.add_argument('--force', action='store_true', help='restore even when the chat changed since the repair')
    args = parser.parse_args(argv)
    if args.restore:
        return restore(args.restore, args.force)
    if args.apply:
        if not args.backup_dir:
            parser.error('--apply needs --backup-dir')
        os.makedirs(args.backup_dir, exist_ok=True)
    return repair(args)


if __name__ == '__main__':
    sys.exit(main())
