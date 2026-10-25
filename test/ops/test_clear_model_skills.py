"""Integration checks against a disposable local PostgreSQL database only."""

import importlib.util
import json
import os
from pathlib import Path
import sys

import psycopg2
import pytest

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).parents[2] / 'scripts/clear_model_skills.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


@pytest.fixture
def conn():
    dsn = os.environ.get('OWUI_SKILLS_TEST_DSN')
    if not dsn:
        pytest.skip('Set OWUI_SKILLS_TEST_DSN to a disposable local PostgreSQL database')
    conn = psycopg2.connect(dsn)
    assert conn.info.host == '127.0.0.1' and conn.info.dbname == 'owui_skill_test'
    with conn, conn.cursor() as cur:
        cur.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        cur.execute("""CREATE TABLE "user" (id text PRIMARY KEY, email text, role text);
            CREATE TABLE config (key text PRIMARY KEY, value json);
            CREATE TABLE model (id text PRIMARY KEY, user_id text, name text, base_model_id text,
                meta text, params text, version_id text, is_active boolean, created_at bigint, updated_at bigint);
            CREATE TABLE model_history (id text PRIMARY KEY, model_id text, parent_id text,
                snapshot json, user_id text, commit_message text, created_at bigint);
            CREATE TABLE skill (id text PRIMARY KEY, content text);
            CREATE TABLE access_grant (id text PRIMARY KEY, resource_id text);
            INSERT INTO "user" VALUES ('admin','admin@example.test','admin'), ('owner','owner@example.test','user');
            INSERT INTO config VALUES ('models.default_metadata','{"builtinTools":{"skills":true}}');
            INSERT INTO skill VALUES ('skill','Do the analysis.');
            INSERT INTO access_grant VALUES ('grant','agent');
            INSERT INTO model_history VALUES ('old','agent',NULL,'{"old":true}','owner','Original',1)""")
        for mid, selections, active in [
            ('agent', ['skill'], True),
            ('inactive', ['skill'], False),
            ('empty', [], True),
        ]:
            meta = {
                'skillIds': selections,
                'hidden': True,
                'builtinTools': {'skills': True},
                'toolIds': ['tool'],
                'description': 'Keep me',
            }
            cur.execute(
                "INSERT INTO model VALUES (%s,'owner',%s,'base',%s,'{\"system\":\"Keep this too\"}','old',%s,1,2)",
                (mid, mid, json.dumps(meta), active),
            )
    yield conn
    conn.rollback()
    conn.close()


def rows(conn, table):
    with conn.cursor() as cur:
        cur.execute(f'SELECT * FROM {table} ORDER BY 1')
        return cur.fetchall()


def test_cleanup_is_selective_versioned_and_reversible(conn):
    before = rows(conn, 'model')
    skills, grants, old_history = rows(conn, 'skill'), rows(conn, 'access_grant'), rows(conn, 'model_history')
    result = cleanup.clear_selections(conn, 'admin@example.test', {'owner@example.test'})
    assert result['changed_models'] == 2
    assert rows(conn, 'skill') == skills and rows(conn, 'access_grant') == grants
    assert old_history[0] in rows(conn, 'model_history')
    with conn.cursor() as cur:
        cur.execute("SELECT meta::jsonb->'builtinTools',is_active FROM model WHERE id='inactive'")
        assert cur.fetchone() == ({'skills': True}, False)
        cur.execute("SELECT snapshot->'meta'->'skillIds' FROM model_history WHERE id != 'old'")
        assert cur.fetchall() == [(None,), (None,)]
        cur.execute(
            "SELECT snapshot->'params',snapshot->'meta'->'builtinTools',snapshot->'meta'->'hidden' FROM model_history WHERE id != 'old'"
        )
        assert cur.fetchall() == [({'system': 'Keep this too'}, {'skills': True}, None)] * 2
    conn.commit()
    assert cleanup.clear_selections(conn, 'admin@example.test')['changed_models'] == 0
    conn.rollback()
    assert cleanup.restore_selections(conn, result['run_id'])['restored_models'] == 2
    assert rows(conn, 'model') == before


def test_rehearsal_leaves_models_and_history_unchanged(conn):
    before, history = rows(conn, 'model'), rows(conn, 'model_history')
    cleanup.clear_selections(conn, 'admin@example.test')
    conn.rollback()
    assert rows(conn, 'model') == before and rows(conn, 'model_history') == history


def test_cleanup_batches_production_sized_inventory(conn):
    with conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO model SELECT 'agent-' || n, user_id, name, base_model_id,
            meta, params, version_id, is_active, created_at, updated_at
            FROM model CROSS JOIN generate_series(1,392) AS n WHERE id='agent' """)
    before = rows(conn, 'model')
    result = cleanup.clear_selections(conn, 'admin@example.test', {'owner@example.test'})
    assert result['changed_models'] == 394
    assert len(rows(conn, cleanup.BACKUP_TABLE)) == 394
    assert len(rows(conn, 'model_history')) == 395
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM model WHERE {cleanup.SELECTED}')
        assert cur.fetchone()[0] == 0
    conn.rollback()
    assert rows(conn, 'model') == before


def test_unnotified_owner_prevents_any_change(conn):
    before = rows(conn, 'model')
    with pytest.raises(ValueError, match='missing from the notified list'):
        cleanup.clear_selections(conn, 'admin@example.test', {'someone-else@example.test'})
    conn.rollback()
    assert rows(conn, 'model') == before


def test_restore_refuses_to_overwrite_later_edits(conn):
    result = cleanup.clear_selections(conn, 'admin@example.test')
    conn.commit()
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE model SET version_id='new-edit' WHERE id='agent'")
    with pytest.raises(ValueError, match='edited or deleted'):
        cleanup.restore_selections(conn, result['run_id'])


def test_unexpected_global_selections_require_review(conn):
    with conn, conn.cursor() as cur:
        cur.execute('UPDATE config SET value=\'{"skillIds":["skill"]}\'')
    with pytest.raises(ValueError, match='Global model defaults'):
        cleanup.clear_selections(conn, 'admin@example.test')


@pytest.mark.parametrize('extra', [[], ['--notification-sent']])
def test_production_apply_requires_notification_before_connecting(monkeypatch, extra):
    monkeypatch.setattr(
        sys, 'argv', ['cleanup', '--environment', 'prod', '--actor-email', 'admin@example.test', '--apply', *extra]
    )
    monkeypatch.setattr(cleanup, 'connect', lambda environment: pytest.fail('Must reject before connecting'))
    with pytest.raises(SystemExit) as error:
        cleanup.main()
    assert error.value.code == 2


def test_authorized_dev_apply_commits_without_production_notification(conn, monkeypatch):
    # Keep the disposable test connection open across the utility's durable-state check.
    class BorrowedConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

        def __enter__(self):
            return conn.__enter__()

        def __exit__(self, *args):
            return conn.__exit__(*args)

    def connect_dev(environment):
        assert environment == 'dev'
        return BorrowedConnection()

    monkeypatch.setattr(cleanup, 'connect', connect_dev)
    monkeypatch.setattr(
        sys, 'argv', ['cleanup', '--environment', 'dev', '--actor-email', 'admin@example.test', '--apply']
    )
    cleanup.main()
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM model WHERE {cleanup.SELECTED}')
        assert cur.fetchone()[0] == 0
        cur.execute(f'SELECT count(*) FROM {cleanup.BACKUP_TABLE}')
        assert cur.fetchone()[0] == 2
