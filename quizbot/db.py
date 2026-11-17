import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=5000')
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0, 1, 2, 3):
        raise RuntimeError('Unsupported database version: %s' % version)
    if version == 0 and db.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
        raise RuntimeError('Old draft database detected. Use a new DATABASE_PATH; keep the old file as a backup.')
    migration = ''
    if version==1:
        migration = '''ALTER TABLE game_participants ADD COLUMN question_delivered_at REAL;
                       ALTER TABLE outbox ADD COLUMN slot TEXT;'''
    if version in (1,2):
        migration += """ALTER TABLE games ADD COLUMN matchmaking TEXT NOT NULL DEFAULT 'none'
                        CHECK(matchmaking IN ('none','public','invite'));
                        UPDATE games SET matchmaking='public' WHERE mode='duel';"""
    try:
        db.executescript('BEGIN IMMEDIATE;\n'+migration+
                         (ROOT / 'quiz_bot_schema.sql').read_text(encoding='utf-8')+'\nCOMMIT;')
    except Exception:
        db.rollback()
        db.close()
        raise
    # Queue rows may disappear while an HTTP request is still running.
    # Persist a high-water mark so their IDs are never reused.
    with db:
        db.execute("""INSERT OR REPLACE INTO settings(key,value) VALUES('outbox_sequence',
            MAX(COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key='outbox_sequence'),0),
                COALESCE((SELECT MAX(outbox_id) FROM outbox),0)))""")
    return db


def seed_questions(db, filename=None):
    data = json.loads(Path(filename or ROOT / 'data' / 'questions.json').read_text(encoding='utf-8'))
    keys = set()
    for q in data:
        if q['key'] in keys or q['difficulty'] not in ('easy', 'medium', 'hard'):
            raise ValueError('Duplicate key or invalid difficulty')
        keys.add(q['key'])
        if not 2 <= len(q['options']) <= 6 or not 0 <= q['correct'] < len(q['options']):
            raise ValueError('Invalid options/answer for ' + q['key'])
        if not q['text'].strip() or not q['topic'].strip() or len(q['text']) > 1500:
            raise ValueError('Invalid question text/topic')
        if any(not isinstance(v, str) or not v.strip() or len(v) > 150 for v in q['options']):
            raise ValueError('Options must contain 1-150 characters')
    with db:
        for q in data:
            db.execute('''INSERT INTO questions
                (source_key,topic,difficulty,question_text,options_json,correct_answer,explanation)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET
                topic=excluded.topic,difficulty=excluded.difficulty,question_text=excluded.question_text,
                options_json=excluded.options_json,correct_answer=excluded.correct_answer,
                explanation=excluded.explanation''',
                (q['key'],q['topic'],q['difficulty'],q['text'],json.dumps(q['options'],ensure_ascii=False),
                 q['correct'],q.get('explanation','')))
    return len(data)
