"""Database upgrades for game history and question administration."""
import logging
import sqlite3
import time
import uuid
from pathlib import Path

TABLES=('users','questions','games','game_participants','game_questions','answers',
        'conversations','settings','outbox','ui_messages')


def quoted(name):
    return '"'+name.replace('"','""')+'"'


def execute_schema(db,schema):
    # Unlike executescript(), this preserves our current migration transaction.
    statement=''
    for line in schema.splitlines(keepends=True):
        statement+=line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement=''
    if statement.strip():
        raise RuntimeError('Incomplete database schema')


def upgrade_to_v4(db,path,schema):
    # Rebuild onto the current schema, including additive features since v4.
    path=Path(path)
    backup=path.with_name(path.stem+'.before-v4-'+uuid.uuid4().hex[:8]+'.db')
    saved=sqlite3.connect(str(backup))
    try:
        db.backup(saved)
    finally:
        saved.close()
    logging.getLogger('quizbot').info('Database backup before upgrade: %s',backup)
    existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables=[t for t in TABLES if t in existing]
    previous_queue_max=db.execute('SELECT COALESCE(MAX(outbox_id),0) FROM outbox').fetchone()[0]
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DROP VIEW IF EXISTS user_overall_stats')
        db.execute('DROP VIEW IF EXISTS user_mode_stats')
        indexes=db.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall()
        for name,table in indexes:
            if table in tables:
                db.execute('DROP INDEX '+quoted(name))
        for table in tables:
            db.execute('ALTER TABLE '+quoted(table)+' RENAME TO '+quoted('_legacy_'+table))
        execute_schema(db,schema)
        for table in tables:
            if table=='outbox':
                continue  # Old messages can contain screens from removed modes.
            source='_legacy_'+table
            old_columns={r[1] for r in db.execute('PRAGMA table_info('+quoted(source)+')')}
            columns=[r[1] for r in db.execute('PRAGMA table_info('+quoted(table)+')') if r[1] in old_columns]
            names=','.join(quoted(c) for c in columns)
            db.execute('INSERT INTO '+quoted(table)+' ('+names+') SELECT '+names+' FROM '+quoted(source))
        # Completed games keep their scores and places. Removed active duels close
        # without being included in a room game or blocking their participants.
        db.execute("""UPDATE games SET status='cancelled',finished_at=? WHERE game_id IN
            (SELECT game_id FROM _legacy_games WHERE mode='duel' AND status IN ('lobby','in_progress'))""",(time.time(),))
        for table in reversed(tables):
            db.execute('DROP TABLE '+quoted('_legacy_'+table))
        db.execute("""INSERT OR REPLACE INTO settings VALUES('outbox_sequence',
            MAX(?,COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key='outbox_sequence'),0)))""",
                   (previous_queue_max,))
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Database migration failed the foreign key check')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')


def upgrade_schema(db, schema, version):
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute("ALTER TABLE games ADD COLUMN play_mode TEXT NOT NULL DEFAULT 'room' CHECK(play_mode IN ('room','solo'))")
        if version == 4:
            db.execute('ALTER TABLE questions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0)')
        if version < 7:
            db.execute('ALTER TABLE questions ADD COLUMN deleted_at REAL CHECK(deleted_at IS NULL OR is_active=0)')
        if version == 6:
            db.execute('DROP TRIGGER question_topic_after_insert')
            db.execute('DROP TRIGGER question_topic_after_update')
            db.execute('ALTER TABLE question_topics RENAME TO _old_question_topics')
        if version in (5,6):
            db.execute('ALTER TABLE question_admin_log RENAME TO _old_question_admin_log')
        execute_schema(db, schema)
        if version == 6:
            db.execute('''INSERT INTO question_topics(topic_id,name,revision)
                SELECT topic_id,name,revision FROM _old_question_topics''')
            db.execute('DROP TABLE _old_question_topics')
        if version < 7:
            db.execute('INSERT OR IGNORE INTO question_topics(name) SELECT DISTINCT topic FROM questions')
        if version in (5,6):
            db.execute('''INSERT INTO question_admin_log(log_id,actor_telegram_id,question_id,action,occurred_at)
                SELECT log_id,actor_telegram_id,question_id,action,occurred_at FROM _old_question_admin_log''')
            db.execute('DROP TABLE _old_question_admin_log')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Database migration failed the foreign key check')
        db.commit()
    except Exception:
        db.rollback()
        raise
