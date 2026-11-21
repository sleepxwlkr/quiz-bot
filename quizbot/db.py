import json
import sqlite3
from pathlib import Path

from .migrations import upgrade_question_schema, upgrade_to_v4
from .questions import validate_question

ROOT = Path(__file__).resolve().parent.parent


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.create_function('quiz_casefold', 1, str.casefold)
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0, 1, 2, 3, 4, 5, 6, 7):
        raise RuntimeError('Unsupported database version: %s' % version)
    if version == 0 and db.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
        raise RuntimeError('Old draft database detected. Use a new DATABASE_PATH; keep the old file as a backup.')
    schema=(ROOT / 'quiz_bot_schema.sql').read_text(encoding='utf-8')
    try:
        if version in (1,2,3):
            upgrade_to_v4(db,path,schema)
        elif version in (4,5,6):
            upgrade_question_schema(db,schema,version)
        else:
            db.executescript(schema)
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
    if not isinstance(data, list):
        raise ValueError('The question file must contain a JSON array.')
    keys = set()
    for index, q in enumerate(data, 1):
        if not isinstance(q, dict):
            raise ValueError(f'Question #{index} must be a JSON object.')
        key = q.get('key')
        if not isinstance(key, str) or not key.strip() or key in keys:
            raise ValueError(f'Question #{index} needs a unique, non-empty key.')
        keys.add(key)
        try:
            validate_question(q)
        except ValueError as exc:
            raise ValueError(f'Question #{index}: {exc}') from None
    imported = 0
    with db:
        deleted_topics = {row[0] for row in db.execute('''SELECT name FROM question_topics WHERE deleted_at IS NOT NULL
            EXCEPT SELECT name FROM question_topics WHERE deleted_at IS NULL''')}
        deleted_keys = {row[0] for row in db.execute('SELECT source_key FROM questions WHERE deleted_at IS NOT NULL')}
        for q in data:
            if q['key'] in deleted_keys or q['topic'] in deleted_topics:
                continue
            db.execute('''INSERT INTO questions
                (source_key,topic,difficulty,question_text,options_json,correct_answer,explanation)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET
                topic=excluded.topic,difficulty=excluded.difficulty,question_text=excluded.question_text,
                options_json=excluded.options_json,correct_answer=excluded.correct_answer,
                explanation=excluded.explanation,revision=questions.revision+1
                WHERE questions.topic<>excluded.topic OR questions.difficulty<>excluded.difficulty
                   OR questions.question_text<>excluded.question_text OR questions.options_json<>excluded.options_json
                   OR questions.correct_answer<>excluded.correct_answer OR questions.explanation<>excluded.explanation''',
                (q['key'],q['topic'],q['difficulty'],q['text'],json.dumps(q['options'],ensure_ascii=False),
                 q['correct'],q.get('explanation','')))
            imported += 1
    return imported


def initialize_question_bank(db):
    """Seed a new bank and report how many questions are visible in it."""
    stored, deleted = db.execute('SELECT COUNT(*),COUNT(deleted_at) FROM questions').fetchone()
    return stored-deleted if stored else seed_questions(db)
