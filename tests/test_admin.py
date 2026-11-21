import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from quizbot.__main__ import Runner, main
from quizbot.admin import parse_admin_ids
from quizbot.app import Application
from quizbot.db import ROOT, connect, initialize_question_bank, seed_questions
from quizbot.engine import Engine, RuleError
from quizbot.questions import QuestionBank
from tests.test_bot import Clock, FakeClient


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'admin.db'
        self.db = None
        self.runner = None
        self.clock = Clock()
        self.update_id = 0
        self.open()
        self.addCleanup(self.close)

    def open(self, admin_ids=('101', '202')):
        self.db = connect(self.path)
        self.e = Engine(self.db, self.clock)
        self.runner = Runner(self.db, FakeClient(), 'quiz_bot', admin_ids=admin_ids)
        self.runner.engine = self.e
        self.runner.app = Application(self.e, 'quiz_bot', admin_ids)

    def close(self):
        if self.runner:
            self.runner.close()
        if self.db:
            self.db.close()

    def update(self, text=None, data=None, user=101, chat_type='private'):
        self.update_id += 1
        chat_id = user if chat_type == 'private' else -1
        sender = {'id': user, 'username': 'same_handle'}
        message = {'message_id': 55, 'chat': {'id': chat_id, 'type': chat_type}, 'from': sender}
        if data is None:
            update = {'message': {**message, 'text': text}}
        else:
            update = {'callback_query': {'id': str(self.update_id), 'from': sender, 'message': message, 'data': data}}
        self.runner.process({'update_id': self.update_id, **update}, self.clock())
        if data is not None:
            self.runner.collect_deliveries()
            for future in list(self.runner.acknowledgements):
                future.result(timeout=2)
            return self.runner.client.calls[-1][1]['text']

    def screen(self, user=101):
        row = self.e.one("SELECT payload_json FROM outbox WHERE chat_id=? AND slot='room'", (str(user),))
        return json.loads(row[0])

    def button(self, label, user=101):
        payload = self.screen(user)
        return next(button['callback_data'] for row in payload['reply_markup']['inline_keyboard']
                    for button in row if button['text']==label)

    def press(self, label, user=101):
        return self.update(data=self.button(label, user), user=user)

    def create_topic(self, name):
        self.update(text='/admin')
        self.press('Создать тему')
        self.update(text=name)
        self.press('Подтвердить')

    def open_questions(self, topic='Информатика', difficulty='Средняя', user=101):
        self.update(text='/admin', user=user)
        self.press(topic, user)
        self.press(difficulty, user)

    def add_draft(self):
        if not QuestionBank(self.db).topic_by_name('Информатика'):
            self.create_topic('Информатика')
        self.open_questions()
        self.press('Добавить вопрос')
        self.update(text='Что делает <команда>?')
        self.update(text='Сохраняет файл\nУдаляет файл\nЗакрывает окно')
        self.press('2')
        self.update(text='Команда удаляет файл.')
        self.assertIn('Предпросмотр', self.screen()['text'])

    def add_question(self):
        self.add_draft()
        self.press('Подтвердить сохранение')
        return self.e.one('SELECT * FROM questions')

    def put_question(self, key, topic, difficulty, text, active=1):
        cursor = self.db.execute('''INSERT INTO questions
            (source_key,topic,difficulty,question_text,options_json,correct_answer,is_active)
            VALUES(?,?,?,?,'["Да","Нет"]',0,?)''', (key, topic, difficulty, text, active))
        self.db.commit()
        return cursor.lastrowid

    def test_access_uses_telegram_id_and_checks_every_callback_and_text(self):
        self.assertEqual(parse_admin_ids(' 101, 202,101 '), {'101', '202'})
        self.assertFalse(parse_admin_ids(' '))
        with self.assertRaises(ValueError):
            parse_admin_ids('101,not-a-user-id')

        self.update(text='/admin', user=303)
        self.assertIn('нет доступа', self.screen(303)['text'])
        self.assertFalse(self.e.state(303))
        self.assertIsNone(self.e.user(303))

        self.add_draft()  # Admin access does not require a player nickname.
        save = self.button('Подтвердить сохранение')
        self.assertIn('нет доступа', self.update(data=save, user=303))
        self.assertIn('личный чат', self.update(data=save, chat_type='group'))
        self.update(text='/admin', chat_type='group')
        self.assertIsNone(self.e.one("SELECT 1 FROM outbox WHERE chat_id='-1'"))
        self.assertIsNone(self.e.one('SELECT 1 FROM questions'))

        self.runner.app = Application(self.e, 'quiz_bot')  # A restart removed the ID from the allowlist.
        self.assertIn('нет доступа', self.update(data=save))
        self.update(text='Текст из старого диалога')
        self.assertIn('нет доступа', self.screen()['text'])
        self.assertIsNone(self.e.one('SELECT 1 FROM questions'))
        self.assertIsNone(self.e.one('SELECT 1 FROM question_admin_log'))

    def test_wizard_validates_and_saves_only_after_preview_confirmation(self):
        self.create_topic('Информатика')
        self.open_questions(difficulty='Лёгкая')
        self.press('Добавить вопрос')
        self.update(text='x'*1501)
        self.assertIn('до 1500 символов', self.screen()['text'])
        self.assertEqual(self.e.state(101)['field'], 'text')
        self.update(text='Что такое стек?')
        self.assertEqual(self.e.state(101)['field'], 'options')
        self.update(text='Только один вариант')
        self.assertIn('от 2 до 6', self.screen()['text'])
        self.update(text='Структура данных\nПапка')
        self.press('1')
        self.press('Пропустить')
        self.assertIn('Структура данных ✓', self.screen()['text'])
        self.assertIsNone(self.e.one('SELECT 1 FROM questions'))
        confirm = self.button('Подтвердить сохранение')
        self.update(data=confirm)
        question = self.e.one('SELECT * FROM questions')
        self.assertRegex(question['source_key'], r'^tg-[0-9a-f]{32}$')
        self.assertEqual(question['correct_answer'], 0)
        self.assertEqual(question['explanation'], '')
        self.assertEqual(question['is_active'], 1)
        self.assertEqual((question['topic'], question['difficulty']), ('Информатика', 'easy'))
        self.assertIn('устарела', self.update(data=confirm))
        audit = self.e.one('SELECT * FROM question_admin_log')
        self.assertEqual((audit['actor_telegram_id'], audit['action'], audit['question_id'], audit['occurred_at']),
                         ('101', 'add', question['question_id'], self.clock()))
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM questions')[0], 1)

    def test_edit_keeps_key_and_requires_new_correct_option_if_options_change(self):
        initial = self.add_question()
        self.press('Редактировать')
        self.assertIn('&lt;команда&gt;', self.screen()['text'])
        self.press('Текст')
        self.update(text='Для чего нужна эта команда?')
        self.press('Варианты')
        self.update(text='Вариант A\nВариант Б')
        self.assertEqual(self.e.state(101)['field'], 'correct')
        self.press('1')
        self.assertEqual(self.e.one('SELECT question_text FROM questions')[0], initial['question_text'])
        self.press('Подтвердить сохранение')
        updated = self.e.one('SELECT * FROM questions')
        self.assertEqual((updated['source_key'], updated['question_id']), (initial['source_key'], initial['question_id']))
        self.assertEqual(QuestionBank.draft(updated)['options'], ['Вариант A', 'Вариант Б'])
        self.assertEqual(updated['correct_answer'], 0)
        self.assertEqual(updated['question_text'], 'Для чего нужна эта команда?')
        self.assertEqual(updated['explanation'], initial['explanation'])
        self.assertEqual([r[0] for r in self.e.all('SELECT action FROM question_admin_log ORDER BY log_id')], ['add','edit'])

    def test_search_filters_and_page_navigation(self):
        target = self.put_question('target', 'История', 'medium', 'Как ПЁТР открыл 100% пути?', 0)
        self.put_question('noise-enabled', 'История', 'medium', 'Другой Пётр')
        self.put_question('noise-topic', 'География', 'medium', 'ПЁТР на карте', 0)
        self.put_question('noise-difficulty', 'История', 'easy', 'Что сделал Пётр?', 0)
        for index in range(6):
            self.put_question(f'other-{index}', 'История', 'medium', f'Карта {index}')
        self.open_questions('История')
        self.press('Далее →')
        self.assertIn('страница 2', self.screen()['text'])
        self.press('Поиск')
        self.update(text='пЁтР')
        self.press('Фильтры')
        self.press('Отключённые')
        self.assertIn(f'#{target} · Как ПЁТР', self.screen()['text'])
        self.assertNotIn('Другой Пётр', self.screen()['text'])
        self.assertNotIn('ПЁТР на карте', self.screen()['text'])
        self.assertNotIn('Что сделал Пётр?', self.screen()['text'])
        self.press('Поиск')
        self.update(text='%')
        self.assertIn(f'#{target} · Как ПЁТР', self.screen()['text'])
        self.press('Поиск')
        self.update(text='_')
        self.assertIn('Вопросы не найдены', self.screen()['text'])
        self.press('Фильтры')
        self.press('Сбросить поиск и фильтры')
        self.assertEqual(self.e.state(101)['filters'], {})
        self.assertIn('Далее →', [b['text'] for row in self.screen()['reply_markup']['inline_keyboard'] for b in row])
        self.press('К сложности')
        self.press('Лёгкая')
        self.assertIn('Что сделал Пётр?', self.screen()['text'])
        self.assertNotIn('Карта', self.screen()['text'])
        self.press('К темам')
        self.assertIn('Банк вопросов · Темы', self.screen()['text'])

    def test_activity_confirmation_and_audit(self):
        question = self.add_question()
        self.press('Отключить')
        self.assertEqual(self.e.one('SELECT is_active FROM questions')[0], 1)
        self.press('Подтвердить')
        self.assertFalse(self.e.available_questions('Информатика', 'medium'))
        self.press('Включить')
        self.press('Подтвердить')
        self.assertEqual(self.e.available_questions('Информатика', 'medium')[0]['question_id'], question['question_id'])
        self.assertEqual([r[0] for r in self.e.all('SELECT action FROM question_admin_log ORDER BY log_id')],
                         ['add', 'disable', 'enable'])
        self.press('В админку')
        self.press('Журнал изменений')
        text = self.screen()['text']
        self.assertIn(f"Отключён вопрос #{question['question_id']}", text)
        self.assertIn(f"Включён вопрос #{question['question_id']}", text)
        self.assertIn('<code>101</code>', text)

    def test_cancelling_invalidates_old_save_and_keeps_existing_question(self):
        self.add_draft()
        old_save = self.button('Подтвердить сохранение')
        self.press('Отмена')
        self.assertIn('Банк вопросов', self.screen()['text'])
        self.assertIn('устарела', self.update(data=old_save))
        self.assertIsNone(self.e.one('SELECT 1 FROM questions'))
        original = self.add_question()
        self.press('Редактировать')
        self.press('Текст')
        self.update(text='Не сохранять этот текст')
        self.update(text='/cancel')
        self.assertIn('Действие отменено', self.screen()['text'])
        self.assertEqual(self.e.one('SELECT question_text FROM questions')[0], original['question_text'])
        self.press('Главное меню')
        self.assertEqual(self.e.state(101), {})

    def test_stale_edit_does_not_overwrite_a_second_admin(self):
        self.add_question()
        self.press('Редактировать')
        self.press('Текст')
        self.update(text='Правка первого')
        save = self.button('Подтвердить сохранение')
        self.open_questions(user=202)
        self.press('#1 · Что делает <команда>?', 202)
        self.press('Редактировать', 202)
        self.press('Текст', 202)
        self.update(text='Правка второго', user=202)
        self.press('Подтвердить сохранение', 202)
        self.update(data=save)
        self.assertIn('уже изменился', self.screen()['text'])
        self.assertEqual(self.e.one('SELECT question_text FROM questions')[0], 'Правка второго')
        self.assertEqual(self.e.one("SELECT actor_telegram_id FROM question_admin_log WHERE action='edit'")[0], '202')

    def test_saved_game_snapshots_and_answers_survive_bank_edits(self):
        seed_questions(self.db)
        owner = self.e.register(303, 'Участник А')['user_id']
        guest = self.e.register(404, 'Участник Б')['user_id']
        gid = self.e.create(owner, 'Информатика', 'easy', 5, 60)
        code = self.e.active(owner)['game_code']
        self.e.join(guest, code)
        self.e.start(gid, owner)
        before = [tuple(row) for row in self.e.all('SELECT * FROM game_questions WHERE game_id=? ORDER BY position', (gid,))]
        first = self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1', (gid,))
        source = self.e.one('SELECT * FROM questions WHERE question_id=?', (first['question_id'],))
        self.db.commit()
        self.open_questions(difficulty='Лёгкая')
        self.press('Поиск')
        self.update(text=source['question_text'])
        open_action = f":open:{first['question_id']}"
        callback = next(button['callback_data'] for row in self.screen()['reply_markup']['inline_keyboard']
                        for button in row if button['callback_data'].endswith(open_action))
        self.update(data=callback)
        self.press('Редактировать')
        self.press('Текст')
        self.update(text='Новый текст')
        self.press('Подтвердить сохранение')
        self.press('Отключить')
        self.press('Подтвердить')
        self.assertNotIn(first['question_id'], [q['question_id'] for q in self.e.available_questions('Информатика','easy')])
        row = self.e.one("SELECT * FROM outbox WHERE kind='question' AND user_id=?", (owner,))
        self.e.confirm_delivery(row, 123, self.clock())
        self.e.answer(gid, owner, 1, first['correct_answer_snapshot'])
        self.clock.advance(300)
        self.e.tick()
        self.assertEqual(before, [tuple(row) for row in self.e.all(
            'SELECT * FROM game_questions WHERE game_id=? ORDER BY position', (gid,))])
        answer = self.e.one('SELECT * FROM answers WHERE game_id=? AND user_id=? AND question_position=1', (gid, owner))
        self.assertEqual((answer['is_correct'], answer['points_awarded']), (1, first['base_points']+5))
        results = self.e.results(gid)
        self.press('Редактировать')
        self.press('Пояснение')
        self.update(text='Пояснение для следующих игр.')
        self.press('Подтвердить сохранение')
        self.assertEqual(before, [tuple(row) for row in self.e.all(
            'SELECT * FROM game_questions WHERE game_id=? ORDER BY position', (gid,))])
        self.assertEqual(results, self.e.results(gid))
        with self.assertRaises(RuleError):
            self.e.join(guest, code)

    def test_admin_edit_survives_cli_startup_and_explicit_import_updates_by_key(self):
        with self.db:
            self.assertEqual(initialize_question_bank(self.db), 60)
        self.open_questions(difficulty='Лёгкая')
        self.press('Поиск')
        self.update(text='битов в одном байте')
        self.press('#1 · Сколько битов в одном байте?')
        self.press('Редактировать')
        self.press('Текст')
        self.update(text='Изменённый вопрос из штатного банка')
        self.press('Подтвердить сохранение')
        self.press('Отключить')
        self.press('Подтвердить')
        self.close()
        self.runner = self.db = None
        env = {'DATABASE_PATH': str(self.path), 'BOT_TOKEN': 'test:placeholder', 'ADMIN_IDS': '101'}
        with patch.dict(os.environ, env), patch('quizbot.__main__.load_env'):
            with patch('sys.argv', ['quizbot', '--init-db']):
                self.assertEqual(main(), 0)
            with patch('sys.argv', ['quizbot']), patch('quizbot.__main__.TelegramClient') as telegram:
                telegram.return_value.call.side_effect = [{'id': 321, 'username': 'test_bot'}, {}, True]
                with patch('quizbot.__main__.Runner') as runner:
                    main()
                    self.assertEqual(runner.call_args.kwargs['admin_ids'], {'101'})
                    runner.return_value.run.assert_called_once()
            with closing(connect(self.path)) as db:
                row = db.execute("SELECT question_text,is_active FROM questions WHERE source_key='it-e01'").fetchone()
                self.assertEqual(tuple(row), ('Изменённый вопрос из штатного банка', 0))
            with patch('sys.argv', ['quizbot', '--import-questions', str(ROOT/'data'/'questions.json')]):
                self.assertEqual(main(), 0)
        self.open()
        row = self.e.one("SELECT question_text,is_active,revision FROM questions WHERE source_key='it-e01'")
        self.assertEqual(tuple(row), ('Сколько битов в одном байте?', 0, 3))
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM question_admin_log')[0], 2)

    def test_empty_topic_survives_restart_and_opens_a_scoped_question_list(self):
        self.update(text='/admin')
        self.assertIn('Банк вопросов · Темы', self.screen()['text'])
        self.press('Создать тему')
        self.update(text='Астрономия')
        self.assertIsNone(self.e.one('SELECT 1 FROM question_topics'))
        self.press('Подтвердить')
        self.assertIsNone(self.e.one('SELECT 1 FROM questions'))
        self.close()
        self.runner = self.db = None
        self.open()
        self.open_questions('Астрономия', 'Сложная')
        self.assertIn('Вопросы не найдены', self.screen()['text'])
        self.press('Добавить вопрос')
        self.update(text='Как называется наша галактика?')
        self.update(text='Млечный Путь\nАндромеда')
        self.press('1')
        self.press('Пропустить')
        self.press('Подтвердить сохранение')
        self.assertEqual(tuple(self.e.one('SELECT topic,difficulty FROM questions')), ('Астрономия','hard'))
        self.press('К списку')
        self.assertIn('Как называется наша галактика?', self.screen()['text'])

    def test_topic_pagination_duplicate_validation_and_cancel(self):
        for index in range(9):
            self.put_question(f'key-{index}', f'Тема {index}', 'easy', f'Вопрос {index}')
        self.update(text='/admin')
        self.press('Далее →')
        self.press('Тема 8')
        self.press('Лёгкая')
        self.assertIn('Вопрос 8', self.screen()['text'])
        self.press('К темам')
        self.press('Создать тему')
        self.update(text='тема 8')
        self.assertIn('уже есть', self.screen()['text'])
        self.update(text='Свободное название')
        confirmation = self.button('Подтвердить')
        self.press('Отмена')
        self.assertIn('устарела', self.update(data=confirmation))
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM question_topics')[0], 9)

    def test_rename_propagates_to_bank_and_open_lobbies_but_preserves_started_games(self):
        seed_questions(self.db)
        owner = self.e.register(303, 'Первый')['user_id']
        guest = self.e.register(404, 'Второй')['user_id']
        waiting = self.e.register(505, 'Третий')['user_id']
        started = self.e.create(owner, 'Информатика', 'easy', 5, 60)
        self.e.join(guest, self.e.active(owner)['game_code'])
        self.e.start(started, owner)
        lobby = self.e.create(waiting, 'Информатика', 'easy', 5, 60)
        self.db.commit()
        topic_id = QuestionBank(self.db).topic_by_name('Информатика')['topic_id']
        self.update(text='/admin')
        self.press('Переименовать тему')
        self.press('Информатика')
        self.update(text='Программирование')
        self.assertIsNone(QuestionBank(self.db).topic_by_name('Программирование'))
        self.press('Подтвердить')
        self.assertEqual(QuestionBank(self.db).topic_by_name('Программирование')['topic_id'], topic_id)
        self.assertIsNone(QuestionBank(self.db).topic_by_name('Информатика'))
        self.assertEqual(len(self.e.available_questions('Программирование', 'easy')), 10)
        self.assertFalse(self.e.available_questions('Информатика', 'easy'))
        self.assertEqual(self.e.one('SELECT topic FROM games WHERE game_id=?', (lobby,))[0], 'Программирование')
        self.assertEqual(self.e.one('SELECT topic FROM games WHERE game_id=?', (started,))[0], 'Информатика')
        self.assertEqual({q[0] for q in self.e.all('SELECT topic_snapshot FROM game_questions WHERE game_id=?',
                                                  (started,))}, {'Информатика'})
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM question_admin_log WHERE action='edit'")[0], 30)
        self.open_questions('Программирование', 'Лёгкая')
        self.assertIn('Программирование', self.screen()['text'])


class AdminMigrationTests(unittest.TestCase):
    def test_v5_upgrade_backfills_topics_without_rewriting_questions_or_audit(self):
        schema = (ROOT/'quiz_bot_schema.sql').read_text(encoding='utf-8')
        schema = (schema[:schema.index('CREATE TABLE IF NOT EXISTS question_topics')]
                  +schema[schema.index('CREATE TABLE IF NOT EXISTS games'):])
        schema = schema.replace('PRAGMA user_version = 6;', 'PRAGMA user_version = 5;')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'v5.db'
            with closing(sqlite3.connect(path)) as old:
                old.executescript(schema)
                seed_questions(old)
                QuestionBank(old).set_active(1, 0, 0, '101', Clock()())
                old.commit()
                before = old.execute('SELECT * FROM questions ORDER BY question_id').fetchall()
                audit = old.execute('SELECT * FROM question_admin_log').fetchall()
            with closing(connect(path)) as current:
                self.assertEqual(current.execute('PRAGMA user_version').fetchone()[0], 6)
                self.assertEqual([tuple(row) for row in current.execute('SELECT * FROM questions ORDER BY question_id')], before)
                self.assertEqual([tuple(row) for row in current.execute('SELECT * FROM question_admin_log')], audit)
                self.assertEqual({row[0] for row in current.execute('SELECT name FROM question_topics')},
                                 {question[2] for question in before})

    def test_v4_upgrade_keeps_existing_records_and_allows_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'v4.db'
            schema = (ROOT/'quiz_bot_schema.sql').read_text(encoding='utf-8')
            schema = schema.replace(' CHECK(is_active IN (0,1)),\n    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0)',
                                    ' CHECK(is_active IN (0,1))')
            schema = schema[:schema.index('CREATE TABLE IF NOT EXISTS question_admin_log')]+schema[schema.index('CREATE TABLE IF NOT EXISTS games'):]
            schema = schema.replace('PRAGMA user_version = 6;', 'PRAGMA user_version = 4;')
            tables = ('users','questions','games','game_participants','game_questions','answers')
            with closing(sqlite3.connect(path)) as old:
                old.executescript(schema)
                old.row_factory = sqlite3.Row
                defaults = json.loads((ROOT/'data'/'questions.json').read_text(encoding='utf-8'))[:5]
                old.executemany('''INSERT INTO questions(source_key,topic,difficulty,question_text,options_json,correct_answer)
                    VALUES(?,?,?,?,?,?)''', [(q['key'], q['topic'], q['difficulty'], q['text'],
                                             json.dumps(q['options']), q['correct']) for q in defaults])
                engine = Engine(old, Clock())
                owner = engine.register(101, 'Первый')['user_id']
                guest = engine.register(202, 'Второй')['user_id']
                old.execute("INSERT INTO settings VALUES('outbox_sequence','0')")
                gid = engine.create(owner, 'Все темы', 'mixed', 5, 60)
                engine.join(guest, engine.active(owner)['game_code'])
                engine.start(gid, owner)
                old.execute("INSERT INTO answers VALUES(?,?,1,'answered',0,1,1000,15,?)", (gid, owner, engine.clock()))
                old.commit()
                before = {table: [tuple(row) for row in old.execute('SELECT * FROM '+table)] for table in tables}
            with closing(connect(path)) as current:
                self.assertEqual(current.execute('PRAGMA user_version').fetchone()[0], 6)
                for table, rows in before.items():
                    saved_columns = 'question_id,source_key,topic,difficulty,question_text,options_json,correct_answer,explanation,is_active'
                    selection = saved_columns if table == 'questions' else '*'
                    self.assertEqual([tuple(row) for row in current.execute(f'SELECT {selection} FROM {table}')], rows)
                self.assertEqual(current.execute('SELECT MAX(revision) FROM questions').fetchone()[0], 0)
                self.assertEqual(current.execute('SELECT name FROM question_topics').fetchone()[0], 'Информатика')
                QuestionBank(current).set_active(1, 0, 0, '101', Clock()())
                current.commit()
                self.assertEqual(current.execute('SELECT action FROM question_admin_log').fetchone()[0], 'disable')
                self.assertIsNone(current.execute('PRAGMA foreign_key_check').fetchone())
