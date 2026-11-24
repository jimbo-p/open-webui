"""Shrink chats saved before the saved-chat bounds existed (one-off, PostgreSQL only).

The bounds heal a chat the first time it is saved again, but that first load of an oversized chat still needs
several GB. This applies the same bounds ahead of time, one chat at a time, with the same decisions as the app:
context compaction is read from the config table, attachment text is dropped only where the file record holds it,
and the chat_message rows get the chat JSON's decisions (a row holding the same output, sources and files as the
chat's copy of its message takes exactly that message's result; other rows are bounded with the protected and
replayed messages worked out from the chat JSON).

Dry run by default: it lists the chats whose chat JSON or chat_message rows are stored above --min-mb (compressed, as
PostgreSQL stores them), and the shared-chat snapshots above it (copied whole before the bounds, and never healed by
the app), with their uncompressed sizes and the size after, by id only. It takes no row lock and writes nothing.
--sizes-only lists the sizes without reading the chats.

--apply needs --backup-dir. For each chat that changes, the original chat JSON (when it changes) and the original
columns of the rows that change are streamed, as the exact bytes PostgreSQL holds, into a new backup: a gzip data
file and a small JSON manifest, written under temporary names, fsynced and then published without ever replacing an
existing file. Reading, planning and the backup take no row lock (one REPEATABLE READ snapshot); the chat is then
locked only to check that it and
the rows to change are as they were read, and to write them, then commits as a separate step. A chat that changed
meanwhile is skipped, and a backup whose changes were not written is removed again; when the commit outcome is
unknown, the transaction's status (txid_status) decides, and the backup is kept unless it was aborted. updated_at
is left alone, so chat lists keep their order. Each chat's snapshot holds no row lock but does hold ACCESS SHARE on
chat and chat_message, which a schema migration waits for: do not run this while a deploy that runs migrations is
active or queued.
--restore MANIFEST stages the backup first, then locks the chat only to check and write: it refuses, unless --force,
when the chat or those rows changed after the repair (that would drop the newer work), and does nothing when the chat
is already as backed up.

Backups hold whole chats, so the backup directory is created 0700 and every file 0600, and a shared snapshot is
named by a short hash of its share token (in the log too; only its manifest holds the token). Keep them only as long
as the verification period after --apply, then delete them; after --apply an attachment dropped from a chat is read
from its file record, which therefore must stay.

Memory: one chat at a time. The chat is read message by message and tool result by tool result, and the backup is
streamed in slices, so the peak is about the parsed size of the chat or of its rows, whichever is larger (about 1.3x
its uncompressed text, measured), on top of the interpreter. A text holding even one character outside the Basic
Multilingual Plane (an emoji, for example) is held by Python at 4 bytes per character, so a chat whose large results
all contain one can need about 5x. Run it as its own task or on a host with at least 5x the largest chat free
(--sizes-only lists them), never inside a serving Open WebUI task; --max-mb (uncompressed) leaves larger chats for a
bigger host. It exits non-zero when any chat failed or changed while being repaired. Set DATABASE_URL to the writer:

    python -m open_webui.utils.chat_bounds_repair --min-mb 8 --sizes-only
    python -m open_webui.utils.chat_bounds_repair --min-mb 8
    python -m open_webui.utils.chat_bounds_repair --min-mb 8 --apply --backup-dir /data/chat-bounds-backup
    python -m open_webui.utils.chat_bounds_repair --restore /data/chat-bounds-backup/<name>-<run>.manifest.json
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
os.environ['ENABLE_DB_MIGRATIONS'] = 'False'

from open_webui.utils.chat_bounds import (  # noqa: E402
    bound_chat_with_changes,
    bound_message,
    bound_messages_map,
    chat_layout,
    inline_file_ids,
    note_compaction_enabled,
    saved_text_length,
)

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


def fingerprint(value) -> str:
    """A digest of a parsed JSON value that ignores key order, without serializing it whole."""
    digest = hashlib.md5()

    def walk(item):
        if isinstance(item, str):
            digest.update(b's%d:' % len(item))
            digest.update(item.encode('utf-8', 'surrogatepass'))
        elif isinstance(item, dict):
            digest.update(b'{')
            for key in sorted(item):
                walk(key)
                walk(item[key])
            digest.update(b'}')
        elif isinstance(item, list):
            digest.update(b'[%d:' % len(item))
            for element in item:
                walk(element)
            digest.update(b']')
        else:
            digest.update(repr(item).encode())

    walk(value)
    return digest.hexdigest()


class ChatPlan:
    """The bounds for one chat's JSON, and the same decisions for its chat_message rows.

    A row holding the same output, sources and files as the chat JSON's copy of its message takes exactly what the
    chat JSON plan decided for that message. A row that differs (or has no copy in the chat JSON) is bounded on its
    own, with the protected and replayed messages worked out from the chat JSON (whose childrenIds and current id the
    rows lack), so both stores protect the same newest turns.
    """

    def __init__(self, chat: dict, current_id: str | None = None, droppable_files=None):
        history = chat.get('history') if isinstance(chat.get('history'), dict) else {}
        messages = history.get('messages') if isinstance(history.get('messages'), dict) else {}
        # The same current id the app's bounds use for this chat body, then the column.
        posted = history.get('currentId') or chat.get('currentId') or chat.get('branchPointMessageId')
        self.current_id = posted if isinstance(posted, str) else current_id
        self.droppable_files = dict(droppable_files or {})
        # The layout the app's own bounds use: worked out on the per-message-bounded copies.
        bounded_view = {key: bound_message(message, self.droppable_files) for key, message in messages.items()}
        self.layout = chat_layout(bounded_view, self.current_id)
        bounded, changed = bound_chat_with_changes(chat, droppable_files=self.droppable_files)
        self.bounded_chat = None if bounded is chat else bounded
        self.fingerprints = {
            key: {column: fingerprint(message.get(column)) for column in ROW_COLUMNS}
            for key, message in messages.items()
            if isinstance(message, dict)
        }
        new_messages = (bounded.get('history') or {}).get('messages') or {}
        self.chat_fields: dict[str, dict] = {}
        for key in changed:
            after = new_messages[key]
            fields = {
                column: after.get(column)
                for column in ROW_COLUMNS
                if fingerprint(after.get(column)) != self.fingerprints[key][column]
            }
            if fields:
                self.chat_fields[key] = fields

    def row_updates(self, row_messages: dict[str, dict]) -> dict[str, dict]:
        """Per message id, the chat_message columns that change."""
        bounded_rows, changed = bound_messages_map(
            row_messages, self.current_id, layout=self.layout, droppable_files=self.droppable_files
        )
        updates = {}
        for key, row in row_messages.items():
            known = self.fingerprints.get(key)
            if known and all(fingerprint(row.get(column)) == known[column] for column in ROW_COLUMNS):
                columns = dict(self.chat_fields.get(key, {}))
            elif key in changed:
                after = bounded_rows[key]
                columns = {
                    column: after.get(column)
                    for column in ROW_COLUMNS
                    if fingerprint(after.get(column)) != fingerprint(row.get(column))
                }
            else:
                columns = {}
            if columns:
                updates[key] = columns
        return updates


def plan(chat: dict, row_messages: dict[str, dict], current_id: str | None) -> tuple[dict | None, dict[str, dict]]:
    """The bounded chat JSON (None when unchanged) and, per message id, the chat_message columns that change."""
    chat_plan = ChatPlan(chat, current_id)
    return chat_plan.bounded_chat, chat_plan.row_updates(row_messages)


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
  FROM json_each((SELECT chat FROM {table} WHERE id = %s))"""
HISTORY_TOP = """SELECT key, json_typeof(value),
       CASE WHEN key = 'messages' THEN NULL ELSE convert_to(value::text, 'UTF8') END
  FROM json_each((SELECT chat->'history' FROM {table} WHERE id = %s))"""
LIST_ELEMENTS = """SELECT e.ordinality - 1, convert_to(e.value::text, 'UTF8')
  FROM json_array_elements((SELECT chat->'messages' FROM {table} WHERE id = %s)) WITH ORDINALITY e(value, ordinality)"""
_MESSAGES = "json_each((SELECT chat->'history'->'messages' FROM {table} WHERE id = %s)) m"
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


TABLES = ('chat', 'shared_chat')


def _q(query: str, table: str) -> str:
    if table not in TABLES:
        raise ValueError(f'unexpected table {table!r}')
    return query.replace('{table}', table)


def _value(connection, chat_id: str, path: str, table: str = 'chat'):
    """One JSON value of the chat, parsed from its UTF-8 bytes (used for small or unusual shapes only)."""
    found = connection.execute(
        _q(f"SELECT convert_to(({path})::text, 'UTF8') FROM {{table}} WHERE id = %s", table), (chat_id,)
    )
    return _loads(found.fetchone()[0])


def _object(connection, query: str, chat_id: str) -> tuple[dict, dict]:
    """A JSON object's keys with their values (None where the query leaves a large value out) and JSON types."""
    values, kinds = {}, {}
    for key, value_kind, data in connection.execute(query, (chat_id,)):
        kinds[key] = value_kind
        values[key] = _loads(data) if data is not None else None
    return values, kinds


def read_chat(connection, chat_id: str, table: str = 'chat') -> dict:
    """The chat JSON as a dict, read key by key, message by message and array element by element."""
    kind = connection.execute(_q('SELECT json_typeof(chat) FROM {table} WHERE id = %s', table), (chat_id,)).fetchone()
    if kind is None:
        raise LookupError(chat_id)
    if kind[0] != 'object':
        return _value(connection, chat_id, 'chat', table)
    chat, kinds = _object(connection, _q(CHAT_TOP, table), chat_id)
    if kinds.get('messages') == 'array':
        rows = _stream(connection, _q(LIST_ELEMENTS, table), (chat_id,))
        listed = {index: _loads(data) for index, data in rows}
        chat['messages'] = [listed[index] for index in sorted(listed)]
    elif 'messages' in kinds:
        chat['messages'] = _value(connection, chat_id, "chat->'messages'", table)
    if kinds.get('history') == 'object':
        history, history_kinds = _object(connection, _q(HISTORY_TOP, table), chat_id)
        if history_kinds.get('messages') == 'object':
            history['messages'] = _read_messages(connection, chat_id, table)
        elif 'messages' in history_kinds:
            history['messages'] = _value(connection, chat_id, "chat->'history'->'messages'", table)
        chat['history'] = history
    elif 'history' in kinds:
        chat['history'] = _value(connection, chat_id, "chat->'history'", table)
    return chat


def _read_messages(connection, chat_id: str, table: str = 'chat') -> dict:
    messages: dict = {}
    arrays: dict = {}
    for key, data in connection.execute(_q(MESSAGE_ODD, table), (chat_id,)):
        messages[key] = _loads(data)
    for message_key, field, value_kind, data in _stream(connection, _q(MESSAGE_FIELDS, table), (chat_id,), itersize=64):
        message = messages.setdefault(message_key, {})
        if data is None and field in ARRAY_FIELDS and value_kind == 'array':
            arrays[(message_key, field)] = {}
            message[field] = None
        else:
            message[field] = _loads(data)
    for message_key, field, index, data in _stream(connection, _q(MESSAGE_ELEMENTS, table), (chat_id,)):
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


def label(table: str, chat_id: str) -> str:
    """How a chat or snapshot is named in the run log and in backup file names. A shared snapshot's id is its share
    token, so it is shown only as a short hash (the manifest, readable by this account only, keeps the id)."""
    if table == 'shared_chat':
        return 'shared-' + hashlib.sha256(chat_id.encode()).hexdigest()[:12]
    return chat_id


def _private_file(path: str, mode: str):
    """Open a new file readable and writable by this account only; never replace an existing one."""
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), mode)


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
        self.published: list[str] = []
        self.sections: list[dict] = []
        self.sha256 = hashlib.sha256()
        self._raw = _private_file(self.temp_data, 'wb')  # closed in finish() or discard()
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
        data_path = os.path.join(self.directory, self.data_name)
        _publish(self.temp_data, data_path)
        self.published.append(data_path)
        body = {
            **manifest,
            'format': FORMAT,
            'data_file': self.data_name,
            'data_sha256': self.sha256.hexdigest(),
            'sections': self.sections,
        }
        temp_manifest = os.path.join(self.directory, f'.{self.manifest_name}.tmp')
        with _private_file(temp_manifest, 'w') as handle:
            json.dump(body, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        final = os.path.join(self.directory, self.manifest_name)
        _publish(temp_manifest, final)
        self.published.append(final)
        _fsync_dir(self.directory)
        return final

    def discard(self) -> None:
        try:
            self._gzip.close()
            self._raw.close()
        finally:
            if os.path.exists(self.temp_data):
                os.unlink(self.temp_data)

    def remove(self) -> None:
        """Remove a published backup whose changes were never written: restoring it would only roll the chat back."""
        for path in reversed(self.published):
            if os.path.exists(path):
                os.unlink(path)
        if self.published:
            _fsync_dir(self.directory)


# ---------------------------------------------------------------------------------------------------------------------

CANDIDATES = """SELECT id FROM chat WHERE pg_column_size(chat) > %s
UNION SELECT chat_id FROM chat_message GROUP BY chat_id HAVING sum(
    coalesce(pg_column_size(output), 0) + coalesce(pg_column_size(sources), 0) + coalesce(pg_column_size(files), 0)
) > %s"""
STORED_SIZES = """SELECT c.id, pg_column_size(c.chat), coalesce((
    SELECT sum(coalesce(pg_column_size(m.output), 0) + coalesce(pg_column_size(m.sources), 0)
               + coalesce(pg_column_size(m.files), 0))
      FROM chat_message m WHERE m.chat_id = c.id), 0)
  FROM chat c WHERE c.id = ANY(%s)"""
RAW_SIZES = """SELECT octet_length(convert_to(chat::text, 'UTF8')), coalesce((
    SELECT sum(coalesce(octet_length(convert_to(output::text, 'UTF8')), 0)
               + coalesce(octet_length(convert_to(sources::text, 'UTF8')), 0)
               + coalesce(octet_length(convert_to(files::text, 'UTF8')), 0))
      FROM chat_message WHERE chat_id = %s), 0)
  FROM chat WHERE id = %s"""
SHARED_CANDIDATES = 'SELECT id, pg_column_size(chat) FROM shared_chat WHERE pg_column_size(chat) > %s'


def _candidates(connection, args) -> list[tuple[str, str, int, int]]:
    """(table, id, stored chat bytes, stored row bytes), largest first. Stored sizes are compressed.

    Shared-chat snapshots copied an oversized chat whole before the bounds existed and never heal on their own.
    """
    if args.chat_id:
        ids = list(dict.fromkeys(args.chat_id))
    else:
        min_bytes = int(args.min_mb * 1024 * 1024)
        ids = [row[0] for row in connection.execute(CANDIDATES, (min_bytes, min_bytes)).fetchall()]
    found = [('chat', *row) for row in connection.execute(STORED_SIZES, (ids,)).fetchall()] if ids else []
    connection.commit()
    if not args.chat_id:
        try:
            shared = connection.execute(SHARED_CANDIDATES, (int(args.min_mb * 1024 * 1024),)).fetchall()
            found += [('shared_chat', shared_id, size, 0) for shared_id, size in shared]
            connection.commit()
        except Exception:  # an instance without shared snapshots
            connection.rollback()
    return sorted(
        ((table, i, int(c or 0), int(r or 0)) for table, i, c, r in found), key=lambda item: -(item[2] + item[3])
    )


def _raw_sizes(connection, table: str, chat_id: str) -> tuple[int, int]:
    """Uncompressed UTF-8 bytes of the chat JSON and of its rows' output, sources and files."""
    if table == 'shared_chat':
        query, params = (
            "SELECT octet_length(convert_to(chat::text, 'UTF8')), 0 FROM shared_chat WHERE id = %s",
            (chat_id,),
        )
    else:
        query, params = RAW_SIZES, (chat_id, chat_id)
    found = connection.execute(query, params).fetchone()
    connection.commit()
    return (int(found[0] or 0), int(found[1] or 0)) if found else (0, 0)


def _compaction_from_config(connection) -> bool:
    """Whether context compaction is on, as the app reads it, so the plans match what the model replays."""
    try:
        found = connection.execute(
            "SELECT value::text FROM config WHERE key = 'chat.context_compaction.enable'"
        ).fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        return False
    return bool(_loads(found[0])) if found else False


def _droppable(connection, chat: dict, owner_id: str | None) -> dict:
    """Attachments over the inline limit whose text a file record of the chat's owner holds: id -> text length."""
    ids = sorted(inline_file_ids(chat))
    if not ids or not owner_id:
        return {}
    try:
        with connection.transaction():  # a savepoint: an unreadable record only keeps the text
            found = connection.execute(
                "SELECT id, length(data->>'content') FROM file WHERE id = ANY(%s) AND user_id = %s", (ids, owner_id)
            ).fetchall()
    except Exception as error:
        print(f'  could not check attachment records, keeping their text: {type(error).__name__}', flush=True)
        return {}
    return {file_id: size for file_id, size in found if size}


class _Changed(Exception):
    pass


class _CommitUnknown(Exception):
    pass


def _chat_md5(connection, table: str, chat_id: str, lock: bool) -> str | None:
    query = "SELECT md5(convert_to(chat::text, 'UTF8')) FROM {table} WHERE id = %s" + (' FOR UPDATE' if lock else '')
    found = connection.execute(_q(query, table), (chat_id,)).fetchone()
    return found[0] if found else None


def _row_md5s(connection, row_ids: list[str], lock: bool) -> dict[str, tuple]:
    if not row_ids:
        return {}
    found = connection.execute(
        "SELECT id, md5(convert_to(output::text, 'UTF8')), md5(convert_to(sources::text, 'UTF8')), "
        "md5(convert_to(files::text, 'UTF8')) FROM chat_message WHERE id = ANY(%s)" + (' FOR UPDATE' if lock else ''),
        (row_ids,),
    ).fetchall()
    return {row_id: tuple(value or NULL_MD5 for value in md5s) for row_id, *md5s in found}


class _Plan:
    def __init__(self, table: str, chat_id: str, chat_md5: str, chat_bytes: int, current_id, owner_id):
        self.table, self.chat_id, self.chat_md5, self.chat_bytes = table, chat_id, chat_md5, chat_bytes
        self.current_id, self.owner_id = current_id, owner_id
        self.bounded_text: bytes | None = None
        self.written_rows: dict[str, dict] = {}
        self.row_digests: dict[str, tuple] = {}
        self.backup: Backup | None = None
        self.txid: int | None = None

    def manifest(self, run_id: str) -> dict:
        return {
            'table': self.table,
            'chat_id': self.chat_id,
            'run_id': run_id,
            'created_at': int(time.time()),
            'current_message_id': self.current_id,
            'read': {'chat_md5': self.chat_md5},
            'written': {
                # None: the repair left the chat JSON alone (rows only), so a restore leaves it alone too.
                'chat_md5': _md5(self.bounded_text) if self.bounded_text is not None else None,
                'rows': {
                    row_id: {key: _md5(value) for key, value in columns.items()}
                    for row_id, columns in self.written_rows.items()
                },
            },
        }

    def written_state(self) -> tuple[str | None, dict[str, tuple]]:
        rows = {}
        for row_id, columns in self.written_rows.items():
            digests = list(self.row_digests[row_id])
            for key, value in columns.items():
                digests[ROW_COLUMNS.index(key)] = _md5(value)
            rows[row_id] = tuple(digests)
        return (_md5(self.bounded_text) if self.bounded_text is not None else self.chat_md5), rows


def _plan_rows_of(connection, plan: _Plan, chat_plan: ChatPlan) -> int:
    """Read the rows, plan them from the chat's plan and keep only the encoded new columns; return chars removed."""
    row_messages, digests = read_rows(connection, plan.chat_id)
    removed = 0
    for message_id, columns in chat_plan.row_updates(row_messages).items():
        row_id = f'{plan.chat_id}-{message_id}'
        before = row_messages[message_id]
        removed += saved_text_length(before) - saved_text_length({**before, **columns})
        plan.written_rows[row_id] = {
            key: (None if value is None else _dumps(value).encode()) for key, value in columns.items()
        }
        plan.row_digests[row_id] = digests[row_id]
    return removed


def _back_up(connection, args, run_id: str, plan: _Plan) -> Backup:
    """Stream the originals into a new backup from the same snapshot the plan was made from."""
    from psycopg import sql

    backup = Backup(args.backup_dir, label(plan.table, plan.chat_id), run_id)
    try:
        if plan.bounded_text is not None:
            chat_sql = sql.SQL("SELECT convert_to(chat::text, 'UTF8') FROM {} WHERE id = {}").format(
                sql.Identifier(plan.table), sql.Literal(plan.chat_id)
            )
            if backup.add(connection, {'kind': 'chat'}, chat_sql) != plan.chat_md5:
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


def _read_and_plan(connection, args, table: str, chat_id: str, run_id: str) -> tuple[str, _Plan | None]:
    """Read and plan in one REPEATABLE READ snapshot without a row lock; with --apply, back up from that snapshot."""
    with connection.transaction():
        # A backup stages values in a temporary table, which a READ ONLY transaction would refuse.
        mode = 'REPEATABLE READ' if args.apply else 'REPEATABLE READ, READ ONLY'
        connection.execute(f'SET TRANSACTION ISOLATION LEVEL {mode}')
        found = connection.execute(
            _q(
                "SELECT md5(convert_to(chat::text, 'UTF8')), octet_length(convert_to(chat::text, 'UTF8')), "
                + ('current_message_id' if table == 'chat' else 'NULL')
                + ', user_id FROM {table} WHERE id = %s',
                table,
            ),
            (chat_id,),
        ).fetchone()
        if not found:
            return 'unchanged', None
        plan = _Plan(table, chat_id, *found)
        chat = read_chat(connection, chat_id, table)
        droppable = _droppable(connection, chat, plan.owner_id) if table == 'chat' else {}
        chat_plan = ChatPlan(chat, plan.current_id, droppable)
        plan.current_id = chat_plan.current_id
        del chat
        bounded_chat, chat_plan.bounded_chat = chat_plan.bounded_chat, None
        plan.bounded_text = _dumps(bounded_chat).encode() if bounded_chat is not None else None
        del bounded_chat
        removed = _plan_rows_of(connection, plan, chat_plan) if table == 'chat' else 0
        after_chat = len(plan.bounded_text) if plan.bounded_text is not None else plan.chat_bytes
        print(
            f'{label(table, chat_id)}: JSON {plan.chat_bytes / 1048576:.1f} -> {after_chat / 1048576:.1f} MB; '
            f'chat_message saved texts -{removed / 1048576:.1f} M characters in {len(plan.written_rows)} rows',
            flush=True,
        )
        if plan.bounded_text is None and not plan.written_rows:
            return 'unchanged', None
        if not args.apply:
            return 'would shrink', None
        plan.backup = _back_up(connection, args, run_id, plan)
    return 'planned', plan


def _write(connection, plan: _Plan) -> None:
    """Lock the chat briefly, check nothing changed since the snapshot, write, then commit as a separate step.

    Raises _Changed or another error when nothing was written, and _CommitUnknown when the commit itself failed
    (it may or may not have landed).
    """
    try:
        if _chat_md5(connection, plan.table, plan.chat_id, lock=True) != plan.chat_md5:
            raise _Changed()
        current = _row_md5s(connection, list(plan.row_digests), lock=True)
        if current != plan.row_digests:
            raise _Changed()
        if plan.bounded_text is not None:
            connection.execute(
                _q("UPDATE {table} SET chat = convert_from(%b, 'UTF8')::json WHERE id = %s", plan.table),
                (plan.bounded_text, plan.chat_id),
            )
        for row_id, columns in plan.written_rows.items():
            assignments = ', '.join(
                f"{key} = convert_from(%b, 'UTF8')::json" if value is not None else f'{key} = NULL'
                for key, value in columns.items()
            )
            values = [value for value in columns.values() if value is not None]
            connection.execute(f'UPDATE chat_message SET {assignments} WHERE id = %s', (*values, row_id))
        plan.txid = connection.execute('SELECT txid_current()').fetchone()[0]
    except BaseException:
        connection.rollback()
        raise
    try:
        connection.commit()
    except BaseException as error:
        raise _CommitUnknown(error) from error


def _landed(plan: _Plan) -> str:
    """After an unknown commit outcome, on a fresh connection: 'written', 'original' or 'unknown'.

    The transaction's own status decides when PostgreSQL still knows it (committed or aborted); while it is in
    progress, or cannot be read, the outcome stays unknown and the backup is kept.
    """
    try:
        connection = _connect()
        try:
            status = connection.execute('SELECT txid_status(%s)', (plan.txid,)).fetchone()[0] if plan.txid else None
        finally:
            connection.close()
    except Exception:
        return 'unknown'
    return {'committed': 'written', 'aborted': 'original'}.get(status, 'unknown')


def _repair_one(connection, args, table: str, chat_id: str, run_id: str) -> str:
    """'shrunk', 'unchanged', 'would shrink' or 'changed' (skipped because it changed while being repaired)."""
    try:
        outcome, plan = _read_and_plan(connection, args, table, chat_id, run_id)
        if plan is None:
            return outcome
        try:
            manifest_path = plan.backup.finish(plan.manifest(run_id))
        except BaseException:
            plan.backup.discard()
            plan.backup.remove()
            raise
        try:
            _write(connection, plan)
        except _CommitUnknown:
            state = _landed(plan)
            name = label(table, chat_id)
            if state == 'written':
                print(f'{name}: shrunk (its commit reported an error but landed); backup {manifest_path}', flush=True)
                return 'shrunk'
            if state == 'original':
                plan.backup.remove()
            else:
                print(f'{name}: the commit outcome is unknown; its backup is kept: {manifest_path}', flush=True)
            raise
        except BaseException:
            plan.backup.remove()  # nothing was written, so the backup would only roll the chat back
            raise
        print(f'{label(table, chat_id)}: shrunk; backup {manifest_path}', flush=True)
        return 'shrunk'
    except _Changed:
        print(f'{label(table, chat_id)}: skipped, it changed while being repaired (run again)', flush=True)
        return 'changed'


def _usable(connection):
    """The same connection, or a new one when the last one broke (for example after a commit error)."""
    if connection.closed or connection.broken:
        return _connect()
    return connection


def _attempt(connection, args, table: str, chat_id: str, run_id: str):
    """Size, check and repair one chat; any error is reported and counted, and the next chat is still tried."""
    name = label(table, chat_id)
    try:
        chat_stored, rows_stored = args.stored[(table, chat_id)]
        chat_raw, rows_raw = _raw_sizes(connection, table, chat_id)
        print(
            f'{name}: JSON {chat_raw / 1048576:.1f} MB and rows {rows_raw / 1048576:.1f} MB '
            f'({(chat_stored + rows_stored) / 1048576:.1f} MB stored)',
            flush=True,
        )
        if args.max_mb and max(chat_raw, rows_raw) > args.max_mb * 1024 * 1024:
            print(f'{name}: left for a bigger host, above --max-mb', flush=True)
            return connection, 'above --max-mb'
        if args.sizes_only:
            return connection, 'listed'
        outcome = _repair_one(connection, args, table, chat_id, run_id)
        return _usable(connection), outcome
    except Exception as error:
        try:
            connection.rollback()
        except Exception:
            pass
        print(f'{name}: FAILED ({type(error).__name__}: {error})', flush=True)
        return _usable(connection), 'failed'


def repair(args) -> int:
    # Load what the chat budget imports lazily now, before any row is locked.
    import open_webui.utils.context_compaction  # noqa: F401

    connection = _connect()
    compaction = _compaction_from_config(connection)
    note_compaction_enabled(compaction)
    run_id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-{os.getpid()}'
    candidates = _candidates(connection, args)
    print(
        f'{len(candidates)} chats and shared snapshots to check (stored above {args.min_mb} MB compressed); '
        f'context compaction is {"on" if compaction else "off"}',
        flush=True,
    )
    outcomes: dict[str, list[str]] = {}
    args.stored = {
        (table, chat_id): (chat_stored, rows_stored) for table, chat_id, chat_stored, rows_stored in candidates
    }
    for table, chat_id, _, _ in candidates:
        connection, outcome = _attempt(connection, args, table, chat_id, run_id)
        outcomes.setdefault(outcome, []).append(label(table, chat_id))
    summary = ', '.join(f'{len(ids)} {name}' for name, ids in sorted(outcomes.items())) or 'nothing to do'
    print(f'summary: {summary}', flush=True)
    for name in ('failed', 'changed', 'above --max-mb'):
        if outcomes.get(name):
            print(f'{name}: {" ".join(outcomes[name])}', flush=True)
    return 1 if outcomes.get('failed') or outcomes.get('changed') else 0


# ---------------------------------------------------------------------------------------------------------------------
# Restore: stage every section first, then lock only to check and write.


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


def _stage(connection, manifest: dict, path: str) -> None:
    """Copy every non-null section into a temporary table (numbered as in the manifest), checking each md5."""
    connection.execute('CREATE TEMPORARY TABLE IF NOT EXISTS chat_bounds_restore (n integer, v bytea) ON COMMIT DROP')
    connection.execute('ALTER TABLE chat_bounds_restore ALTER COLUMN v SET STORAGE EXTERNAL')
    with (
        connection.cursor() as cursor,
        cursor.copy('COPY chat_bounds_restore (n, v) FROM STDIN (FORMAT BINARY)') as copy,
    ):
        copy.write(COPY_SIGNATURE + struct.pack('!II', 0, 0))
        for number, (section, handle) in enumerate(_sections(manifest, os.path.dirname(os.path.abspath(path)))):
            if section['kind'] == 'row' and section['column'] not in ROW_COLUMNS:
                raise SystemExit(f'unexpected column {section["column"]!r} in {path}')
            if section['null']:
                continue
            copy.write(struct.pack('!hiii', 2, 4, number, section['bytes']))
            digest, remaining = hashlib.md5(), section['bytes']
            while remaining:
                block = handle.read(min(remaining, 1 << 20))
                if not block:
                    raise SystemExit('the backup data file ended early')
                digest.update(block)
                copy.write(block)
                remaining -= len(block)
            if digest.hexdigest() != section['md5']:
                raise SystemExit(f'{path}: a section does not match its manifest')
        copy.write(struct.pack('!h', -1))


def _restore_state(manifest: dict, chat_md5: str, rows: dict[str, tuple]) -> tuple[str, list[str]]:
    """'written' (as the repair left it), 'original' (already as backed up) or 'drift' (changed since), with details.

    A rows-only repair never touched the chat JSON, so later activity in the chat JSON does not count as drift.
    """
    written = manifest['written']
    originals = {(s['id'], s['column']): s['md5'] for s in manifest['sections'] if s['kind'] == 'row'}
    drift, original = [], True
    if written['chat_md5'] is not None:
        if chat_md5 != written['chat_md5']:
            drift.append('the chat JSON')
        original = chat_md5 == manifest['read']['chat_md5']
    for row_id, columns in written['rows'].items():
        now = rows.get(row_id)
        current = [now[ROW_COLUMNS.index(key)] for key in columns] if now else None
        if current != list(columns.values()):
            drift.append(f'row {row_id}')
        if current != [originals.get((row_id, key)) for key in columns]:
            original = False
    if not drift:
        return 'written', []
    return ('original', drift) if original else ('drift', drift)


def _write_staged(connection, manifest: dict, table: str) -> list[str]:
    """Write every staged section back; return the ids that were not found."""
    missing = []
    for number, section in enumerate(manifest['sections']):
        if section['null']:
            value, params = 'NULL', ()
        else:
            value, params = "convert_from((SELECT v FROM chat_bounds_restore WHERE n = %s), 'UTF8')::json", (number,)
        if section['kind'] == 'chat':
            query = _q(f'UPDATE {{table}} SET chat = {value} WHERE id = %s', table)
            result = connection.execute(query, (*params, manifest['chat_id']))
        else:
            result = connection.execute(
                f'UPDATE chat_message SET {section["column"]} = {value} WHERE id = %s', (*params, section['id'])
            )
        if result.rowcount != 1:
            missing.append(section.get('id') or manifest['chat_id'])
    return missing


def restore(path: str, force: bool) -> int:
    with open(path, encoding='utf-8') as handle:
        manifest = json.load(handle)
    if manifest.get('format') != FORMAT:
        raise SystemExit(f'{path} is not a {FORMAT} manifest')
    chat_id, table = manifest['chat_id'], manifest.get('table', 'chat')
    connection = _connect()
    with connection.transaction():
        _stage(connection, manifest, path)  # before any lock: the chat is locked only to check and write
        chat_md5 = _chat_md5(connection, table, chat_id, lock=True)
        if chat_md5 is None:
            print(f'{label(table, chat_id)}: the chat no longer exists; nothing restored')
            return 1
        rows = _row_md5s(connection, list(manifest['written']['rows']), lock=True)
        state, drift = _restore_state(manifest, chat_md5, rows)
        if state == 'original':
            print(f'{label(table, chat_id)}: already as backed up (not applied, or restored before); nothing to do')
            return 0
        if state == 'drift' and not force:
            print(
                f'{label(table, chat_id)}: refused, changed since the repair ({", ".join(drift[:5])}); restoring would '
                'drop that '
                'newer work. Use --force to overwrite anyway.'
            )
            return 1
        missing = _write_staged(connection, manifest, table)
    rows_count = len(manifest['written']['rows'])
    print(f'restored {label(table, chat_id)} and {rows_count} chat_message rows' + (' (forced)' if drift else ''))
    if missing:
        print(f'not found while restoring: {" ".join(sorted(set(missing)))}')
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--min-mb', type=float, default=8.0, help='stored (compressed) size to check above')
    parser.add_argument(
        '--max-mb',
        type=float,
        default=0,
        help='leave chats whose JSON or rows are larger (uncompressed) for a bigger host',
    )
    parser.add_argument('--chat-id', action='append', help='check only these chats (repeatable)')
    parser.add_argument('--sizes-only', action='store_true', help='list the candidates and their sizes, nothing else')
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
        os.makedirs(args.backup_dir, mode=0o700, exist_ok=True)
        os.chmod(args.backup_dir, 0o700)
    return repair(args)


if __name__ == '__main__':
    sys.exit(main())
