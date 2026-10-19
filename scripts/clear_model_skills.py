"""Clear model-builder skill selections, preserving skills and version history.

Default is a transactional rehearsal followed by ROLLBACK. Live connections use
the explicitly selected OxyGPT environment. Local tests use OWUI_SKILLS_TEST_DSN.
No credentials, prompts, skill bodies, or owner emails are printed.
"""

import argparse
import copy
import json
import os
import time
import uuid

import boto3
import psycopg2
from psycopg2.extras import Json, RealDictCursor

SELECTED = """CASE WHEN jsonb_typeof(meta::jsonb->'skillIds') = 'array'
    THEN jsonb_array_length(meta::jsonb->'skillIds') > 0 ELSE false END"""
BACKUP_TABLE = 'oxy_model_skill_selection_backup'


def connect(environment):
    if environment == 'local':
        conn = psycopg2.connect(os.environ['OWUI_SKILLS_TEST_DSN'])
    else:
        session = boto3.Session(region_name='us-east-1')
        cluster = session.client('rds').describe_db_clusters(
            DBClusterIdentifier=f'oxygpt-{environment}-cluster'
        )['DBClusters'][0]
        password = session.client('secretsmanager').get_secret_value(
            SecretId=f'oxygpt/{environment}/rds_master_password'
        )['SecretString']
        conn = psycopg2.connect(
            host=cluster['Endpoint'], dbname='openwebui', user='oxygpt_admin',
            password=password, sslmode='require', connect_timeout=10,
            application_name=f'owui_0123_skill_cleanup_{environment}',
        )
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='30s'")
        cur.execute("SET lock_timeout='5s'")
        cur.execute("SET idle_in_transaction_session_timeout='60s'")
    conn.commit()
    return conn


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def clear_selections(conn, actor_email, notified_emails=None):
    run_id = str(uuid.uuid4())
    now = int(time.time())
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Include newly created models; a short table lock closes the inventory/update race.
        cur.execute('LOCK TABLE model IN SHARE ROW EXCLUSIVE MODE')
        cur.execute('SELECT id FROM "user" WHERE lower(email)=lower(%s) AND role=\'admin\'', (actor_email,))
        actor = cur.fetchone()
        if not actor:
            raise ValueError('The migration author must be an existing administrator.')
        cur.execute("SELECT value FROM config WHERE key='models.default_metadata'")
        defaults = cur.fetchone()
        if defaults and (decode(defaults['value']) or {}).get('skillIds'):
            raise ValueError('Global model defaults now contain skill selections; refresh the inventory and review them.')
        cur.execute("SELECT id FROM model WHERE meta::jsonb ? 'skillIds' AND jsonb_typeof(meta::jsonb->'skillIds') NOT IN ('array','null')")
        if cur.fetchone():
            raise ValueError('Malformed skill selections require review before cleanup.')
        cur.execute(f'SELECT * FROM model WHERE {SELECTED} ORDER BY id')
        models = [dict(row) for row in cur.fetchall()]
        if notified_emails is not None:
            cur.execute(f'''SELECT DISTINCT lower(trim(u.email)) AS email FROM model m
                LEFT JOIN "user" u ON u.id=m.user_id WHERE {SELECTED}''')
            if any(row['email'] not in notified_emails for row in cur.fetchall()):
                raise ValueError('An affected owner is missing from the notified list; refresh and notify before applying.')
        cur.execute(f'''CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
            run_id text NOT NULL, model_id text NOT NULL, before_row jsonb NOT NULL,
            after_version_id text NOT NULL, actor_id text NOT NULL, created_at bigint NOT NULL,
            restored_at bigint, PRIMARY KEY (run_id,model_id))''')
        for model in models:
            meta = copy.deepcopy(decode(model['meta']))
            meta.pop('skillIds')
            version_id = str(uuid.uuid4())
            snapshot_meta = copy.deepcopy(meta)
            snapshot_meta.pop('hidden', None)
            snapshot_meta.pop('chat_variables_schema', None)
            snapshot = {'name': model['name'], 'base_model_id': model['base_model_id'],
                        'params': decode(model['params']) or {}, 'meta': snapshot_meta}
            cur.execute(f'INSERT INTO {BACKUP_TABLE} (run_id,model_id,before_row,after_version_id,actor_id,created_at) VALUES (%s,%s,%s,%s,%s,%s)',
                        (run_id, model['id'], Json(model), version_id, actor['id'], now))
            cur.execute('''INSERT INTO model_history (id,model_id,parent_id,snapshot,user_id,commit_message,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (version_id, model['id'], model['version_id'], Json(snapshot), actor['id'],
                 '0.12.3: clear default skill selections after owner notification', now))
            cur.execute('UPDATE model SET meta=%s,version_id=%s,updated_at=%s WHERE id=%s',
                        (json.dumps(meta), version_id, now, model['id']))
            cur.execute('SELECT * FROM model WHERE id=%s', (model['id'],))
            expected = {**model, 'meta': json.dumps(meta), 'version_id': version_id, 'updated_at': now}
            if dict(cur.fetchone()) != expected:
                raise AssertionError('A model field outside the intended change was modified.')
        cur.execute(f'SELECT count(*) AS n FROM model WHERE {SELECTED}')
        if cur.fetchone()['n']:
            raise AssertionError('Selected skills remain.')
    return {'run_id': run_id, 'changed_models': len(models)}


def restore_selections(conn, run_id):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('LOCK TABLE model IN SHARE ROW EXCLUSIVE MODE')
        cur.execute(f'SELECT * FROM {BACKUP_TABLE} WHERE run_id=%s AND restored_at IS NULL ORDER BY model_id FOR UPDATE', (run_id,))
        rows = cur.fetchall()
        if not rows:
            raise ValueError('No unrestored backup exists for that run.')
        for row in rows:
            before = row['before_row']
            cur.execute('SELECT * FROM model WHERE id=%s', (row['model_id'],))
            current = cur.fetchone()
            if not current or current['version_id'] != row['after_version_id']:
                raise ValueError('A model was edited or deleted after cleanup; restore requires review.')
            meta = decode(current['meta'])
            original_meta = decode(before['meta'])
            if meta != {key: value for key, value in original_meta.items() if key != 'skillIds'}:
                raise ValueError('Model metadata changed after cleanup; refusing to overwrite it.')
            # Restore only fields touched by cleanup. Keep the migration history as an audit trail.
            cur.execute('UPDATE model SET meta=%s,version_id=%s,updated_at=%s WHERE id=%s',
                        (before['meta'], before['version_id'], before['updated_at'], row['model_id']))
        cur.execute(f'UPDATE {BACKUP_TABLE} SET restored_at=%s WHERE run_id=%s', (int(time.time()), run_id))
    return {'run_id': run_id, 'restored_models': len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=['dev', 'prod', 'local'], required=True)
    parser.add_argument('--actor-email', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--notification-sent', action='store_true')
    parser.add_argument('--notified-emails', help='The actual notified-owner list, one email per line')
    parser.add_argument('--restore-run')
    args = parser.parse_args()
    if args.environment == 'prod' and not args.apply:
        parser.error('Production rehearsals are not allowed; validate in dev, then explicitly apply.')
    notified = None
    if args.apply and not args.restore_run:
        if not args.notification_sent or not args.notified_emails:
            parser.error('--apply requires --notification-sent and --notified-emails')
        with open(args.notified_emails, encoding='utf-8-sig') as f:
            notified = {line.strip().lower() for line in f if line.strip()}
    conn = connect(args.environment)
    try:
        result = restore_selections(conn, args.restore_run) if args.restore_run else clear_selections(conn, args.actor_email, notified)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps({'environment': args.environment, 'committed': args.apply, **result}))
    finally:
        conn.rollback()
        conn.close()
    # Verify durable state through a new writer connection, avoiding replica lag.
    if args.apply and not args.restore_run:
        with connect(args.environment) as verified, verified.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM model WHERE {SELECTED}')
            if cur.fetchone()[0]:
                raise RuntimeError('Skill selections were added after commit; re-inventory before proceeding.')


if __name__ == '__main__':
    main()
