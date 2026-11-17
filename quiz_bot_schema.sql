-- SQLite, schema version 3. Times are UTC Unix seconds.
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    platform_user_id TEXT NOT NULL UNIQUE,
    nickname TEXT NOT NULL,
    nickname_normalized TEXT NOT NULL UNIQUE,
    rating INTEGER NOT NULL DEFAULT 1000,
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
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))
);
CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY,
    game_code TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK(mode IN ('duel','room')),
    matchmaking TEXT NOT NULL DEFAULT 'none' CHECK(matchmaking IN ('none','public','invite')),
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
    rating_before INTEGER,
    rating_change INTEGER,
    rating_after INTEGER,
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
CREATE VIEW IF NOT EXISTS user_mode_stats AS
WITH results AS (
    SELECT p.*,g.mode,g.question_count,
        (SELECT COUNT(*) FROM game_participants pp WHERE pp.game_id=g.game_id AND pp.rank=1) AS first_places
    FROM game_participants p JOIN games g USING(game_id) WHERE g.status='completed'
), times AS (
    SELECT a.user_id,g.mode,AVG(a.response_ms) AS average_response_ms
    FROM answers a JOIN games g USING(game_id)
    WHERE g.status='completed' AND a.answer_status='answered' GROUP BY a.user_id,g.mode
)
SELECT r.user_id,r.mode,COUNT(*) AS games_played,
    SUM(CASE WHEN rank=1 AND first_places=1 THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN rank=1 AND first_places>1 THEN 1 ELSE 0 END) AS shared_first_places,
    SUM(CASE WHEN participant_status='forfeited' THEN 1 ELSE 0 END) AS forfeits,
    SUM(total_score) AS total_points,AVG(total_score) AS average_points_per_game,
    MAX(total_score) AS best_score,AVG(rank) AS average_rank,
    SUM(correct_count) AS correct_answers,SUM(question_count) AS total_questions,
    100.0*SUM(correct_count)/SUM(question_count) AS accuracy_percent,
    t.average_response_ms
FROM results r LEFT JOIN times t ON t.user_id=r.user_id AND t.mode=r.mode
GROUP BY r.user_id,r.mode;
CREATE VIEW IF NOT EXISTS user_overall_stats AS
SELECT user_id,SUM(games_played) AS games_played,SUM(wins) AS wins,
    SUM(shared_first_places) AS shared_first_places,SUM(forfeits) AS forfeits,
    SUM(total_points) AS total_points,1.0*SUM(total_points)/SUM(games_played) AS average_points_per_game,
    MAX(best_score) AS best_score,SUM(correct_answers) AS correct_answers,
    SUM(total_questions) AS total_questions,
    100.0*SUM(correct_answers)/SUM(total_questions) AS accuracy_percent
FROM user_mode_stats GROUP BY user_id;
PRAGMA user_version = 3;
