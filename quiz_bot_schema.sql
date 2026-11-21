-- SQLite, schema version 7. Times are UTC Unix seconds.
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    platform_user_id TEXT NOT NULL UNIQUE,
    nickname TEXT NOT NULL,
    nickname_normalized TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    question_id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','medium','hard')),
    question_text TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_answer INTEGER NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
    deleted_at REAL CHECK(deleted_at IS NULL OR is_active=0)
);
CREATE TABLE IF NOT EXISTS question_topics (
    topic_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
    deleted_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_question_topics_name ON question_topics(name) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS question_admin_log (
    log_id INTEGER PRIMARY KEY,
    actor_telegram_id TEXT NOT NULL,
    question_id INTEGER REFERENCES questions(question_id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK(action IN ('add','edit','enable','disable','delete','delete_topic')),
    occurred_at REAL NOT NULL,
    topic_name TEXT,
    CHECK((action='delete_topic' AND question_id IS NULL AND topic_name IS NOT NULL)
          OR (action<>'delete_topic' AND question_id IS NOT NULL AND topic_name IS NULL))
);
CREATE TRIGGER IF NOT EXISTS question_topic_after_insert
AFTER INSERT ON questions WHEN NEW.deleted_at IS NULL BEGIN
    INSERT INTO question_topics(name) VALUES(NEW.topic) ON CONFLICT(name) WHERE deleted_at IS NULL DO NOTHING;
END;
CREATE TRIGGER IF NOT EXISTS question_topic_after_update
AFTER UPDATE OF topic ON questions WHEN NEW.topic<>OLD.topic AND NEW.deleted_at IS NULL BEGIN
    INSERT INTO question_topics(name) VALUES(NEW.topic) ON CONFLICT(name) WHERE deleted_at IS NULL DO NOTHING;
END;
CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY,
    game_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'lobby'
        CHECK(status IN ('lobby','in_progress','completed','cancelled')),
    created_by_user_id INTEGER NOT NULL REFERENCES users(user_id),
    topic TEXT NOT NULL,
    difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','medium','hard','mixed')),
    question_count INTEGER NOT NULL CHECK(question_count BETWEEN 1 AND 30),
    question_time_limit_seconds INTEGER NOT NULL CHECK(question_time_limit_seconds BETWEEN 1 AND 120),
    scoring_version INTEGER NOT NULL DEFAULT 2,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    lobby_expires_at REAL NOT NULL,
    hard_deadline_at REAL,
    CHECK(status <> 'completed' OR finished_at IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS game_participants (
    game_id INTEGER NOT NULL REFERENCES games(game_id),
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    nickname_at_game TEXT NOT NULL,
    participant_status TEXT NOT NULL DEFAULT 'joined'
        CHECK(participant_status IN ('joined','playing','finished','forfeited')),
    total_score INTEGER NOT NULL DEFAULT 0 CHECK(total_score >= 0),
    correct_count INTEGER NOT NULL DEFAULT 0 CHECK(correct_count >= 0),
    rank INTEGER CHECK(rank IS NULL OR rank > 0),
    total_response_ms INTEGER NOT NULL DEFAULT 0 CHECK(total_response_ms >= 0),
    current_position INTEGER NOT NULL DEFAULT 0,
    question_opened_at REAL,
    question_delivered_at REAL,
    question_deadline_at REAL,
    joined_at REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY(game_id,user_id)
);
CREATE TABLE IF NOT EXISTS game_questions (
    game_id INTEGER NOT NULL REFERENCES games(game_id),
    position INTEGER NOT NULL CHECK(position > 0),
    question_id INTEGER REFERENCES questions(question_id) ON DELETE SET NULL,
    topic_snapshot TEXT NOT NULL,
    difficulty_snapshot TEXT NOT NULL,
    question_snapshot TEXT NOT NULL,
    options_snapshot TEXT NOT NULL,
    correct_answer_snapshot INTEGER NOT NULL,
    explanation_snapshot TEXT NOT NULL DEFAULT '',
    base_points INTEGER NOT NULL,
    PRIMARY KEY(game_id,position)
);
CREATE TABLE IF NOT EXISTS answers (
    game_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    question_position INTEGER NOT NULL,
    answer_status TEXT NOT NULL CHECK(answer_status IN ('answered','timed_out','forfeited')),
    submitted_answer INTEGER,
    is_correct INTEGER NOT NULL CHECK(is_correct IN (0,1)),
    response_ms INTEGER CHECK(response_ms IS NULL OR response_ms >= 0),
    points_awarded INTEGER NOT NULL DEFAULT 0 CHECK(points_awarded >= 0),
    submitted_at REAL NOT NULL,
    PRIMARY KEY(game_id,user_id,question_position),
    FOREIGN KEY(game_id,user_id) REFERENCES game_participants(game_id,user_id),
    FOREIGN KEY(game_id,question_position) REFERENCES game_questions(game_id,position),
    CHECK((answer_status='answered' AND submitted_answer IS NOT NULL AND response_ms IS NOT NULL)
        OR (answer_status IN ('timed_out','forfeited') AND submitted_answer IS NULL
            AND is_correct=0 AND points_awarded=0 AND response_ms IS NULL))
);
CREATE TABLE IF NOT EXISTS conversations (
    platform_user_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message',
    payload_json TEXT NOT NULL,
    game_id INTEGER,
    user_id INTEGER,
    position INTEGER,
    slot TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status,lobby_expires_at);
CREATE INDEX IF NOT EXISTS idx_completed_games_finished ON games(finished_at) WHERE status='completed';
CREATE INDEX IF NOT EXISTS idx_participants_user ON game_participants(user_id,game_id);
CREATE INDEX IF NOT EXISTS idx_deadlines ON game_participants(participant_status,question_deadline_at);
CREATE INDEX IF NOT EXISTS idx_questions_filter ON questions(topic,difficulty,is_active);
CREATE INDEX IF NOT EXISTS idx_answers_user ON answers(user_id,game_id);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(available_at,outbox_id);
CREATE TABLE IF NOT EXISTS ui_messages (
    chat_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY(chat_id,slot)
);
PRAGMA user_version = 7;
