"""Question format shared by the JSON importer and the Telegram editor."""

import json
import uuid

QUESTION_DIFFICULTIES = ('easy', 'medium', 'hard')
MAX_QUESTION_LENGTH = 1500
MAX_OPTION_LENGTH = 150


def validate_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUESTION_LENGTH:
        raise ValueError(f'Введите текст вопроса длиной до {MAX_QUESTION_LENGTH} символов.')


def validate_topic(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Введите название темы.')


def validate_options(options):
    if not isinstance(options, list) or not 2 <= len(options) <= 6:
        raise ValueError('Нужно от 2 до 6 вариантов ответа, по одному на строку.')
    if any(not isinstance(option, str) or not option.strip() or len(option) > MAX_OPTION_LENGTH
           for option in options):
        raise ValueError(f'Каждый вариант должен содержать от 1 до {MAX_OPTION_LENGTH} символов.')


def validate_question(question):
    validate_text(question.get('text'))
    validate_topic(question.get('topic'))
    if question.get('difficulty') not in QUESTION_DIFFICULTIES:
        raise ValueError('Выберите лёгкую, среднюю или сложную категорию.')
    options = question.get('options')
    validate_options(options)
    correct = question.get('correct')
    if type(correct) is not int or not 0 <= correct < len(options):
        raise ValueError('Выберите номер правильного ответа из списка вариантов.')
    if not isinstance(question.get('explanation', ''), str):
        raise ValueError('Пояснение должно быть текстом.')


class QuestionChanged(Exception):
    """The question or topic changed after the confirmation screen opened."""


class QuestionBank:
    def __init__(self, db):
        self.db = db

    def get(self, question_id):
        return self.db.execute('SELECT * FROM questions WHERE question_id=? AND deleted_at IS NULL', (question_id,)).fetchone()

    def list(self, filters, limit, offset):
        conditions, params = ['deleted_at IS NULL'], []
        for key, column in (('topic', 'topic'), ('difficulty', 'difficulty'), ('active', 'is_active')):
            if key in filters:
                conditions.append(f'{column}=?')
                params.append(filters[key])
        if filters.get('query'):
            conditions.append('instr(quiz_casefold(question_text), ?)>0')
            params.append(filters['query'].casefold())
        where = ' WHERE ' + ' AND '.join(conditions)
        return self.db.execute(
            'SELECT * FROM questions'+where+' ORDER BY question_id DESC LIMIT ? OFFSET ?',
            (*params, limit, offset),
        ).fetchall()

    def topics(self, limit, offset):
        return self.db.execute(
            'SELECT * FROM question_topics WHERE deleted_at IS NULL ORDER BY quiz_casefold(name),name LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()

    def topic(self, topic_id):
        return self.db.execute('SELECT * FROM question_topics WHERE topic_id=? AND deleted_at IS NULL', (topic_id,)).fetchone()

    def topic_by_name(self, name):
        return self.db.execute('SELECT * FROM question_topics WHERE name=? AND deleted_at IS NULL', (name,)).fetchone()

    def ensure_topic_name_available(self, name, except_id=None):
        duplicate = self.db.execute('''SELECT topic_id FROM question_topics WHERE deleted_at IS NULL AND quiz_casefold(name)=?
            AND (? IS NULL OR topic_id<>?)''', (name.casefold(), except_id, except_id)).fetchone()
        if duplicate is not None:
            raise ValueError('Тема с таким названием уже есть.')

    def create_topic(self, name):
        self.ensure_topic_name_available(name)
        self.db.execute('INSERT INTO question_topics(name) VALUES(?)', (name,))

    def rename_topic(self, topic_id, revision, name, actor_id, occurred_at):
        topic = self.topic(topic_id)
        if not topic or topic['revision'] != revision:
            raise QuestionChanged
        old_name = topic['name']
        if old_name == name:
            return False
        self.ensure_topic_name_available(name, topic_id)
        updated = self.db.execute('''UPDATE question_topics SET name=?,revision=revision+1
            WHERE topic_id=? AND revision=? AND deleted_at IS NULL''', (name, topic_id, revision))
        if updated.rowcount != 1:
            raise QuestionChanged
        self.db.execute('''INSERT INTO question_admin_log(actor_telegram_id,question_id,action,occurred_at)
            SELECT ?,question_id,'edit',? FROM questions WHERE topic=? AND deleted_at IS NULL''',
                        (actor_id, occurred_at, old_name))
        self.db.execute('UPDATE questions SET topic=?,revision=revision+1 WHERE topic=? AND deleted_at IS NULL', (name, old_name))
        self.db.execute("UPDATE games SET topic=? WHERE topic=? AND status='lobby'", (name, old_name))
        return True

    def delete_topic(self, topic_id, revision, actor_id, occurred_at):
        topic = self.topic(topic_id)
        if topic is None:
            raise QuestionChanged
        updated = self.db.execute('''UPDATE question_topics SET deleted_at=?,revision=revision+1
            WHERE topic_id=? AND revision=? AND deleted_at IS NULL''', (occurred_at, topic_id, revision))
        if updated.rowcount != 1:
            raise QuestionChanged
        self.db.execute('''UPDATE questions SET is_active=0,deleted_at=?,revision=revision+1
            WHERE topic=? AND deleted_at IS NULL''', (occurred_at, topic['name']))
        self.db.execute('''INSERT INTO question_admin_log(actor_telegram_id,action,occurred_at,topic_name)
            VALUES(?,'delete_topic',?,?)''', (actor_id, occurred_at, topic['name']))

    @staticmethod
    def draft(question):
        return dict(text=question['question_text'], topic=question['topic'], difficulty=question['difficulty'],
                    options=json.loads(question['options_json']), correct=question['correct_answer'],
                    explanation=question['explanation'])

    def save(self, draft, actor_id, occurred_at, question_id=None, revision=None):
        validate_question(draft)
        values = (draft['text'], draft['topic'], draft['difficulty'], json.dumps(draft['options'], ensure_ascii=False),
                  draft['correct'], draft.get('explanation', ''))
        if question_id is None:
            cursor = self.db.execute('''INSERT INTO questions
                (question_text,topic,difficulty,options_json,correct_answer,explanation,source_key)
                VALUES(?,?,?,?,?,?,?)''', (*values, 'tg-'+uuid.uuid4().hex))
            question_id = cursor.lastrowid
            action = 'add'
        else:
            cursor = self.db.execute('''UPDATE questions SET question_text=?,topic=?,difficulty=?,options_json=?,
                correct_answer=?,explanation=?,revision=revision+1
                WHERE question_id=? AND revision=? AND deleted_at IS NULL''',
                (*values, question_id, revision))
            if cursor.rowcount != 1:
                raise QuestionChanged
            action = 'edit'
        self.log(actor_id, question_id, action, occurred_at)
        return question_id

    def set_active(self, question_id, active, revision, actor_id, occurred_at):
        cursor = self.db.execute('''UPDATE questions SET is_active=?,revision=revision+1
            WHERE question_id=? AND revision=? AND is_active<>? AND deleted_at IS NULL''',
                                 (active, question_id, revision, active))
        if cursor.rowcount != 1:
            raise QuestionChanged
        self.log(actor_id, question_id, 'enable' if active else 'disable', occurred_at)

    def delete_question(self, question_id, revision, actor_id, occurred_at):
        cursor = self.db.execute('''UPDATE questions SET is_active=0,deleted_at=?,revision=revision+1
            WHERE question_id=? AND revision=? AND deleted_at IS NULL''', (occurred_at, question_id, revision))
        if cursor.rowcount != 1:
            raise QuestionChanged
        self.log(actor_id, question_id, 'delete', occurred_at)

    def log(self, actor_id, question_id, action, occurred_at):
        self.db.execute('''INSERT INTO question_admin_log(actor_telegram_id,question_id,action,occurred_at)
            VALUES(?,?,?,?)''', (actor_id, question_id, action, occurred_at))

    def log_page(self, limit, offset):
        return self.db.execute('''SELECT log_id,actor_telegram_id,question_id,action,occurred_at,topic_name
            FROM question_admin_log ORDER BY log_id DESC LIMIT ? OFFSET ?''', (limit, offset)).fetchall()
