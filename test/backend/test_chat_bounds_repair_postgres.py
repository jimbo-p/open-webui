"""The one-off saved-chat repair against a disposable PostgreSQL database.

Opt in with CHAT_BOUNDS_TEST_DATABASE_URL pointing to a disposable PostgreSQL database; every test creates and drops
its own schema. Synthetic data only.
"""

import gzip
import json
import os
import threading
import uuid

import pytest
from open_webui import env
from open_webui.utils import chat_bounds, chat_bounds_repair

DATABASE_URL = os.getenv('CHAT_BOUNDS_TEST_DATABASE_URL')

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='Requires an isolated PostgreSQL database')


@pytest.fixture
def small_limits(monkeypatch):
    for name, value in {
        'CHAT_STORAGE_BOUNDS': True,
        'TOOL_RESULT_MAX_CHARS': 400_000,
        'CHAT_MESSAGE_TOOL_OUTPUT_MAX_CHARS': 600_000,
        'CHAT_SOURCE_DOCUMENT_MAX_CHARS': 10_000,
        'CHAT_MESSAGE_SOURCES_MAX_CHARS': 25_000,
        'CHAT_INLINE_FILE_CONTENT_MAX_CHARS': 5_000,
        'CHAT_STORED_BULK_MAX_CHARS': 1_000_000,
        'CHAT_BOUND_PROTECTED_TURNS': 1,
        'CHAT_TRIM_PREVIEW_MIN_CHARS': 10_000,
        'CHAT_TRIM_OLD_PREVIEW_CHARS': 1_000,
    }.items():
        monkeypatch.setattr(env, name, value)
    monkeypatch.setitem(chat_bounds._compaction, 'enabled', False)


@pytest.fixture
def database(monkeypatch):
    import psycopg

    schema = f'chat_bounds_{uuid.uuid4().hex[:10]}'
    admin = psycopg.connect(DATABASE_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA {schema}')
    admin.execute(
        f'CREATE TABLE {schema}.chat (id text primary key, user_id text, title text, chat json, '
        'current_message_id text, updated_at bigint)'
    )
    admin.execute(
        f'CREATE TABLE {schema}.chat_message (id text primary key, chat_id text, role text, parent_id text, '
        'content json, output json, sources json, files json, context_summary text, created_at bigint, '
        'done boolean default true, updated_at bigint)'
    )
    separator = '&' if '?' in DATABASE_URL else '?'
    url = f'{DATABASE_URL}{separator}options=-csearch_path%3D{schema}'
    monkeypatch.setenv('DATABASE_URL', url)
    connection = psycopg.connect(url, autocommit=True)
    yield connection
    connection.close()
    admin.execute(f'DROP SCHEMA {schema} CASCADE')
    admin.close()


def tool_message(message_id, sizes, parent=None):
    output = []
    for index, size in enumerate(sizes):
        call_id = f'{message_id}-call-{index}'
        output.append({'type': 'function_call', 'call_id': call_id, 'name': 'view_file', 'arguments': '{}'})
        output.append(
            {
                'type': 'function_call_output',
                'call_id': call_id,
                # Non-ASCII on purpose: the backup must keep the exact bytes, not re-escaped text.
                'output': [{'type': 'input_text', 'text': '✓😀' + 'r' * size}],
                'status': 'completed',
            }
        )
    return {'id': message_id, 'parentId': parent, 'childrenIds': [], 'role': 'assistant', 'output': output}


def seed(connection, chat_id='big', turns=6, size=300_000):
    messages, parent = {}, None
    for index in range(turns):
        user_id, reply_id = f'u{index}', f'a{index}'
        messages[user_id] = {
            'id': user_id,
            'parentId': parent,
            'childrenIds': [reply_id],
            'role': 'user',
            'timestamp': 2 * index + 1,
        }
        if parent:
            messages[parent]['childrenIds'] = [user_id]
        messages[reply_id] = {**tool_message(reply_id, [size], parent=user_id), 'timestamp': 2 * index + 2}
        parent = reply_id
    chat = {'title': 'T', 'history': {'messages': messages, 'currentId': parent}, 'messages': list(messages.values())}
    text = json.dumps(chat, ensure_ascii=False, indent=1)  # not the compact form the repair writes
    connection.execute(
        'INSERT INTO chat VALUES (%s, %s, %s, %s::json, %s, %s)', (chat_id, 'owner', 'T', text, parent, 123)
    )
    for message_id, message in messages.items():
        output = json.dumps(message['output'], ensure_ascii=False, indent=1) if message.get('output') else None
        connection.execute(
            'INSERT INTO chat_message (id, chat_id, role, parent_id, output, created_at) VALUES (%s, %s, %s, %s, '
            '%s::json, %s)',
            (
                f'{chat_id}-{message_id}',
                chat_id,
                message['role'],
                message.get('parentId'),
                output,
                message['timestamp'],
            ),
        )
    return text


def chat_text(connection, chat_id='big'):
    return connection.execute('SELECT chat::text FROM chat WHERE id = %s', (chat_id,)).fetchone()[0]


def row_text(connection, row_id):
    return connection.execute('SELECT output::text FROM chat_message WHERE id = %s', (row_id,)).fetchone()[0]


def run(*argv):
    return chat_bounds_repair.main(list(argv))


def test_a_dry_run_takes_no_lock_and_writes_nothing(small_limits, database, capsys):
    original = seed(database)
    import psycopg

    holder = psycopg.connect(os.environ['DATABASE_URL'])
    holder.execute("SELECT 1 FROM chat WHERE id = 'big' FOR UPDATE")  # an active writer holds the chat
    try:
        assert run('--min-mb', '0') == 0
    finally:
        holder.rollback()
        holder.close()
    assert chat_text(database) == original
    assert 'would shrink' in capsys.readouterr().out


def test_apply_shrinks_both_stores_keeps_exact_backups_and_never_overwrites_them(small_limits, database, tmp_path):
    original = seed(database)
    original_row = row_text(database, 'big-a0')
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    shrunk = chat_text(database)
    assert len(shrunk) < len(original) / 2 and len(row_text(database, 'big-a0')) < len(original_row) / 2
    assert database.execute("SELECT updated_at FROM chat WHERE id = 'big'").fetchone()[0] == 123
    [manifest_path] = list(tmp_path.glob('*.manifest.json'))
    manifest = json.loads(manifest_path.read_text())
    with gzip.open(tmp_path / manifest['data_file'], 'rb') as handle:
        chat_section = manifest['sections'][0]
        assert chat_section['kind'] == 'chat'
        assert handle.read(chat_section['bytes']).decode() == original  # byte for byte, no re-escaping
    assert not list(tmp_path.glob('.*.tmp'))
    # A second run finds nothing to do and leaves the first backup alone.
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    assert list(tmp_path.glob('*.manifest.json')) == [manifest_path]


def test_restore_refuses_after_newer_work_unless_forced(small_limits, database, tmp_path, capsys):
    original = seed(database)
    original_row = row_text(database, 'big-a0')
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    [manifest_path] = list(tmp_path.glob('*.manifest.json'))
    # Unchanged since the repair: restore puts back the exact original.
    assert run('--restore', str(manifest_path)) == 0
    assert chat_text(database) == original and row_text(database, 'big-a0') == original_row
    # Repair again, then the user keeps working: restore must refuse.
    for backup in tmp_path.iterdir():
        backup.unlink()
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    [manifest_path] = list(tmp_path.glob('*.manifest.json'))
    newer = json.loads(chat_text(database))
    newer['title'] = 'Renamed after the repair'
    database.execute("UPDATE chat SET chat = %s::json WHERE id = 'big'", (json.dumps(newer),))
    capsys.readouterr()
    assert run('--restore', str(manifest_path)) == 1
    assert 'refused' in capsys.readouterr().out
    assert json.loads(chat_text(database))['title'] == 'Renamed after the repair'
    assert run('--restore', str(manifest_path), '--force') == 0
    assert chat_text(database) == original


def test_a_chat_that_changes_while_it_is_repaired_is_skipped(small_limits, database, tmp_path, monkeypatch):
    seed(database)
    original_read_rows = chat_bounds_repair.read_rows
    import psycopg

    def read_rows_and_meanwhile_edit(connection, chat_id):
        writer = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)
        writer.execute("UPDATE chat SET chat = json_build_object('title', 'edited') WHERE id = 'big'")
        writer.close()
        return original_read_rows(connection, chat_id)

    monkeypatch.setattr(chat_bounds_repair, 'read_rows', read_rows_and_meanwhile_edit)
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 1
    assert json.loads(chat_text(database)) == {'title': 'edited'}
    assert not list(tmp_path.iterdir())  # nothing was written, so no backup is left to roll it back


def test_failures_are_reported_and_the_run_continues(small_limits, database, tmp_path, monkeypatch, capsys):
    seed(database, 'big')
    seed(database, 'other')
    calls = []

    class FailOnce(chat_bounds_repair.ChatPlan):
        def __init__(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('CONTEXT: JSON data, line 1: ..."text": "what the user pasted')
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(chat_bounds_repair, 'ChatPlan', FailOnce)
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert 'FAILED (RuntimeError)' in out and '1 failed' in out and '1 shrunk' in out
    assert 'pasted' not in out  # the error's text can quote the chat: only its class is printed


def test_max_mb_leaves_larger_chats_alone(small_limits, database, tmp_path, capsys):
    original = seed(database, size=300_000)
    assert run('--min-mb', '0', '--max-mb', '0.01', '--apply', '--backup-dir', str(tmp_path)) == 0
    assert chat_text(database) == original
    out = capsys.readouterr().out
    assert 'above --max-mb' in out and 'MB stored' in out  # uncompressed and stored sizes are both listed


def test_sizes_only_lists_without_reading_the_chats(small_limits, database, tmp_path, capsys, monkeypatch):
    original = seed(database)
    monkeypatch.setattr(chat_bounds_repair, 'read_chat', lambda *args: pytest.fail('read during --sizes-only'))
    assert run('--min-mb', '0', '--sizes-only') == 0
    assert chat_text(database) == original and 'big: JSON' in capsys.readouterr().out


def test_rows_get_the_chat_decisions_with_a_stale_current_id_and_a_missing_row(small_limits, database, tmp_path):
    seed(database, turns=8, size=300_000)
    database.execute("DELETE FROM chat_message WHERE id = 'big-u3'")  # a row that was never written
    stale = json.loads(chat_text(database))
    stale['history']['currentId'] = 'a4'  # a tab that saved before the newest turns
    database.execute(
        "UPDATE chat SET chat = %s::json, current_message_id = 'a4' WHERE id = 'big'", (json.dumps(stale),)
    )
    newest_row = row_text(database, 'big-a7')
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    chat = json.loads(chat_text(database))['history']['messages']
    assert row_text(database, 'big-a7') == newest_row  # the newest turn keeps its replay row whole
    assert 'Saved copy shortened' in chat['a0']['output'][1]['output'][0]['text']
    for key in ('a0', 'a1', 'a2', 'a5', 'a6'):
        assert json.loads(row_text(database, f'big-{key}')) == chat[key]['output']  # both stores agree


def test_restoring_a_chat_already_as_backed_up_does_nothing(small_limits, database, tmp_path, capsys):
    original = seed(database)
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    [manifest_path] = list(tmp_path.glob('*.manifest.json'))
    assert run('--restore', str(manifest_path)) == 0
    capsys.readouterr()
    assert run('--restore', str(manifest_path), '--force') == 0
    assert 'already as backed up' in capsys.readouterr().out and chat_text(database) == original


def test_the_repair_reads_whether_compaction_is_on(small_limits, database, capsys):
    seed(database)
    database.execute('CREATE TABLE config (key text primary key, value json, updated_at bigint)')
    database.execute("INSERT INTO config VALUES ('chat.context_compaction.enable', 'true', 0)")
    assert run('--min-mb', '0') == 0
    assert 'context compaction is on' in capsys.readouterr().out


def test_attachment_text_is_dropped_only_where_the_owner_holds_the_same_file(small_limits, database, tmp_path):
    database.execute('CREATE TABLE file (id text primary key, user_id text, data json)')
    text = 'x' * 6_000
    for file_id, owner in (('here', 'owner'), ('theirs', 'someone-else')):
        database.execute('INSERT INTO file VALUES (%s, %s, %s::json)', (file_id, owner, json.dumps({'content': text})))
    seed(database)
    chat = json.loads(chat_text(database))
    chat['history']['messages']['u0']['files'] = [
        {'type': 'file', 'id': file_id, 'file': {'data': {'content': text}}} for file_id in ('here', 'theirs', 'gone')
    ]
    database.execute("UPDATE chat SET chat = %s::json WHERE id = 'big'", (json.dumps(chat),))
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    here, theirs, gone = json.loads(chat_text(database))['history']['messages']['u0']['files']
    assert 'content' not in here['file']['data']
    assert theirs['file']['data']['content'] == text and gone['file']['data']['content'] == text


def shared_snapshot(connection, text):
    connection.execute(
        'CREATE TABLE IF NOT EXISTS shared_chat (id text primary key, chat_id text, user_id text, title text, '
        'chat json, created_at bigint, updated_at bigint)'
    )
    connection.execute("INSERT INTO shared_chat VALUES ('share-1', 'big', 'owner', 'T', %s::json, 0, 0)", (text,))


def test_shared_snapshots_are_listed_repaired_and_restorable(small_limits, database, tmp_path, capsys):
    original = seed(database)
    shared_snapshot(database, original)
    assert run('--min-mb', '0', '--sizes-only') == 0
    assert 'shared-' in capsys.readouterr().out
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    snapshot = database.execute("SELECT chat::text FROM shared_chat WHERE id = 'share-1'").fetchone()[0]
    assert len(snapshot) < len(original) / 2
    [manifest] = [path for path in tmp_path.glob('shared-*.manifest.json')]
    out = capsys.readouterr().out
    assert 'share-1' not in out and not any('share-1' in path.name for path in tmp_path.iterdir())  # no tokens
    assert oct(tmp_path.stat().st_mode & 0o777) == '0o700'
    assert all(oct(path.stat().st_mode & 0o777) == '0o600' for path in tmp_path.iterdir())
    assert run('--restore', str(manifest)) == 0
    assert database.execute("SELECT chat::text FROM shared_chat WHERE id = 'share-1'").fetchone()[0] == original


def test_a_rows_only_repair_restores_without_touching_newer_chat_json(small_limits, database, tmp_path):
    seed(database)
    database.execute("UPDATE chat SET chat = %s::json WHERE id = 'big'", (json.dumps({'title': 'T'}),))
    original_row = row_text(database, 'big-a0')
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    assert len(row_text(database, 'big-a0')) < len(original_row)
    database.execute("UPDATE chat SET chat = %s::json WHERE id = 'big'", (json.dumps({'title': 'Renamed later'}),))
    [manifest] = list(tmp_path.glob('*.manifest.json'))
    assert run('--restore', str(manifest)) == 0  # the chat JSON was never part of this repair
    assert row_text(database, 'big-a0') == original_row
    assert json.loads(chat_text(database)) == {'title': 'Renamed later'}


class FlakyCommit:
    """A connection whose commit after an UPDATE reports an error: after committing, after the server aborted the
    transaction, or with the transaction still open (in progress)."""

    def __init__(self, inner, outcome):
        self.inner, self.outcome, self.updated = inner, outcome, False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, query, *args, **kwargs):
        if str(query).lstrip().upper().startswith('UPDATE'):
            self.updated = True
        return self.inner.execute(query, *args, **kwargs)

    def commit(self):
        if self.updated:
            self.updated = False
            if self.outcome == 'committed':
                self.inner.commit()
            elif self.outcome == 'aborted':
                self.inner.rollback()
            raise RuntimeError('connection lost while committing')
        return self.inner.commit()


@pytest.mark.parametrize('outcome', ['committed', 'aborted', 'in progress'])
def test_an_unknown_commit_outcome_is_checked_before_the_backup_is_removed(
    small_limits, database, tmp_path, monkeypatch, outcome
):
    original = seed(database)
    real_connect = chat_bounds_repair._connect
    monkeypatch.setattr(chat_bounds_repair, '_connect', lambda: FlakyCommit(real_connect(), outcome))
    code = run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path))
    manifests = list(tmp_path.glob('*.manifest.json'))
    if outcome == 'committed':
        assert code == 0 and len(manifests) == 1 and chat_text(database) != original
    elif outcome == 'aborted':
        assert code == 1 and manifests == [] and chat_text(database) == original
    else:  # still running when checked: the backup stays
        assert code == 1 and len(manifests) == 1


def test_a_landed_chat_is_reported_and_the_run_summarised_when_the_database_cannot_be_reached_again(
    small_limits, database, tmp_path, monkeypatch, capsys
):
    seed(database, 'big')
    seed(database, 'other')
    real_connect = chat_bounds_repair._connect
    monkeypatch.setattr(chat_bounds_repair, '_connect', lambda: FlakyCommit(real_connect(), 'committed'))

    def unreachable(connection):
        raise ConnectionError('the writer is failing over')

    monkeypatch.setattr(chat_bounds_repair, '_usable', unreachable)
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert 'shrunk (its commit reported an error but landed)' in out and 'FAILED' not in out
    assert 'stopped: the database could not be reached again (ConnectionError)' in out
    assert 'summary: 1 not checked, 1 shrunk' in out and 'not checked: ' in out
    assert len(list(tmp_path.glob('*.manifest.json'))) == 1


def test_an_unreadable_file_record_does_not_stop_the_repair(small_limits, database, tmp_path):
    seed(database)  # no file table at all: the attachment check fails, the chat is still repaired
    chat = json.loads(chat_text(database))
    chat['history']['messages']['u0']['files'] = [
        {'type': 'file', 'id': 'f', 'file': {'data': {'content': 'x' * 6_000}}}
    ]
    database.execute("UPDATE chat SET chat = %s::json WHERE id = 'big'", (json.dumps(chat),))
    assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) == 0
    [item] = json.loads(chat_text(database))['history']['messages']['u0']['files']
    assert item['file']['data']['content'] == 'x' * 6_000


def test_the_repair_never_runs_migrations():
    import subprocess
    import sys

    code = 'import os, open_webui.utils.chat_bounds_repair; print(os.environ["ENABLE_DB_MIGRATIONS"])'
    result = subprocess.run(
        [sys.executable, '-c', code], capture_output=True, text=True, env={**os.environ, 'ENABLE_DB_MIGRATIONS': 'True'}
    )
    assert result.stdout.strip().splitlines()[-1] == 'False'


def test_a_concurrent_writer_waits_only_briefly(small_limits, database, tmp_path):
    seed(database)
    import psycopg

    done = threading.Event()
    result = {}

    def writer():
        connection = psycopg.connect(os.environ['DATABASE_URL'])
        connection.execute("SET lock_timeout = '10s'")
        while not done.is_set():
            connection.execute("SELECT 1 FROM chat WHERE id = 'big' FOR UPDATE")
            connection.rollback()
        result['ok'] = True
        connection.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert run('--min-mb', '0', '--apply', '--backup-dir', str(tmp_path)) in (0, 1)
    finally:
        done.set()
        thread.join()
    assert result.get('ok')
