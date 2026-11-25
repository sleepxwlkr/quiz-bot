import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quizbot.app import Application, menu
from quizbot.db import ROOT, connect
from tests import test_bot as fixtures

Clock=fixtures.Clock


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def legacy(self,version):
        path=Path(self.tmp.name)/('v%s.db'%version)
        schema=(ROOT/'tests'/'fixtures'/'schema_v3.sql').read_text(encoding='utf-8')
        if version<3:
            schema=schema.replace("    matchmaking TEXT NOT NULL DEFAULT 'none' CHECK(matchmaking IN ('none','public','invite')),\n",'')
        if version==1:
            schema=schema.replace('    question_delivered_at REAL,\n','').replace('    slot TEXT,\n','')
            schema=re.sub(r'CREATE TABLE IF NOT EXISTS ui_messages \(.*?\);\n','',schema,flags=re.S)
        schema=schema.replace('PRAGMA user_version = 3;','PRAGMA user_version = %s;'%version)
        db=sqlite3.connect(path)
        db.executescript(schema)
        now=Clock()()
        for uid in range(1,6):
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)',(uid,str(uid),f'Игрок {uid}',f'игрок {uid}',1000+uid,now,now))
        db.execute("""INSERT INTO questions(question_id,source_key,topic,difficulty,question_text,options_json,correct_answer)
            VALUES(1,'legacy','Тема','easy','Сколько будет 2+2?','["4","5"]',0)""")
        for gid,mode,status,owner in ((1,'room','completed',1),(2,'room','lobby',1),
                                      (3,'duel','completed',3),(4,'duel','lobby',5),(5,'duel','in_progress',3)):
            db.execute("""INSERT INTO games(game_id,game_code,mode,status,created_by_user_id,topic,difficulty,
                question_count,question_time_limit_seconds,created_at,started_at,finished_at,lobby_expires_at,hard_deadline_at)
                VALUES(?,?,?,?,?,'Тема','easy',5,60,?,?,?,?,?)""",
                (gid,'OLD%s'%gid,mode,status,owner,now,now if status!='lobby' else None,
                 now if status=='completed' else None,now+1800,now+300 if status=='in_progress' else None))
            players=(owner,) if gid==4 else ((1,2) if mode=='room' else (3,4))
            for index,uid in enumerate(players):
                ps='finished' if status=='completed' else ('playing' if status=='in_progress' else 'joined')
                db.execute("""INSERT INTO game_participants(game_id,user_id,nickname_at_game,participant_status,
                    total_score,correct_count,rank,joined_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (gid,uid,f'Игрок {uid}',ps,15 if status=='completed' and index==0 else 0,
                     1 if status=='completed' and index==0 else 0,index+1 if status=='completed' else None,now))
            if status=='completed':
                db.execute("""INSERT INTO game_questions VALUES(?,1,1,'Тема','easy','Сколько будет 2+2?',
                    '["4","5"]',0,'',10)""",(gid,))
                for index,uid in enumerate(players):
                    db.execute('INSERT INTO answers VALUES(?,?,1,?,?,?,?,?,?)',
                               (gid,uid,'answered',index,1-index,1000,15 if index==0 else 0,now))
        db.execute("INSERT INTO settings VALUES('update_offset:999','123')")
        db.execute("INSERT INTO settings VALUES('outbox_sequence','450')")
        db.execute("INSERT INTO outbox(outbox_id,chat_id,payload_json,created_at) VALUES(500,'1',?,?)",
                   (json.dumps({'text':'Старый экран с Elo'}),now))
        if version>=2:
            for uid in range(1,6):
                db.execute('INSERT INTO ui_messages VALUES(?,?,?)',(str(uid),'room',700+uid))
        db.commit()
        db.close()
        return path

    def test_versions_one_two_three_preserve_history_and_remove_global_metrics(self):
        for version in (1,2,3):
            with self.subTest(version=version):
                path=self.legacy(version)
                db=connect(path)
                try:
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],8)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],5)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM games').fetchone()[0],5)
                    self.assertEqual(db.execute('SELECT game_code FROM games WHERE game_id=1').fetchone()[0],'OLD1')
                    self.assertEqual(db.execute('SELECT status FROM games WHERE game_id=2').fetchone()[0],'lobby')
                    self.assertEqual(db.execute('SELECT total_score FROM game_participants WHERE game_id=3 AND user_id=3').fetchone()[0],15)
                    self.assertEqual([r[0] for r in db.execute('SELECT status FROM games WHERE game_id IN (4,5)')],['cancelled','cancelled'])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM answers').fetchone()[0],4)
                    self.assertEqual(db.execute('SELECT value FROM settings WHERE key=?',('update_offset:999',)).fetchone()[0],'123')
                    self.assertGreaterEqual(int(db.execute("SELECT value FROM settings WHERE key='outbox_sequence'").fetchone()[0]),500)
                    self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
                    self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
                    self.assertNotIn('rating',{r[1] for r in db.execute('PRAGMA table_info(users)')})
                    self.assertNotIn('mode',{r[1] for r in db.execute('PRAGMA table_info(games)')})
                    self.assertFalse({r[1] for r in db.execute('PRAGMA table_info(game_participants)')} &
                                     {'rating_before','rating_change','rating_after'})
                    self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchall())
                    self.assertFalse(db.execute('SELECT 1 FROM outbox').fetchall())
                    backups=list(path.parent.glob(path.stem+'.before-v4-*.db'))
                    self.assertEqual(len(backups),1)
                    saved=sqlite3.connect(backups[0])
                    try:
                        self.assertEqual(saved.execute('PRAGMA user_version').fetchone()[0],version)
                        self.assertEqual(saved.execute('SELECT rating FROM users WHERE user_id=1').fetchone()[0],1001)
                    finally:
                        saved.close()
                finally:
                    db.close()
                reopened=connect(path)
                reopened.close()
                self.assertEqual(len(list(path.parent.glob(path.stem+'.before-v4-*.db'))),1)

    def test_failed_upgrade_rolls_back_and_keeps_backup(self):
        path=self.legacy(3)
        with patch('quizbot.migrations.execute_schema',side_effect=RuntimeError('Interrupted migration')):
            with self.assertRaises(RuntimeError):
                connect(path)
        db=sqlite3.connect(path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT rating FROM users WHERE user_id=1').fetchone()[0],1001)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '_legacy_%'").fetchall())
        finally:
            db.close()
        self.assertEqual(len(list(path.parent.glob(path.stem+'.before-v4-*.db'))),1)


# Reuse fixtures without importing the old test class into unittest discovery.
class SimpleGameTests(unittest.TestCase):
    setUp=fixtures.BotTests.setUp
    tearDown=fixtures.BotTests.tearDown
    game=fixtures.BotTests.game
    delivered=fixtures.BotTests.delivered
    answer_all=fixtures.BotTests.answer_all

    def test_each_game_has_its_own_leaderboard(self):
        first=self.game()
        self.answer_all(first,self.a)
        self.answer_all(first,self.b,False)
        second=self.game()
        self.answer_all(second,self.a,False)
        self.answer_all(second,self.b)
        self.assertEqual(self.e.participants(first)[0]['user_id'],self.a['user_id'])
        self.assertEqual(self.e.participants(second)[0]['user_id'],self.b['user_id'])
        for gid in (first,second):
            result=self.e.results(gid)
            self.assertIn('Таблица лидеров',result)
            self.assertIn(self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0],result)
            self.assertNotIn('Elo',result)
            self.assertIn('tg://user?id=',result)
        self.assertEqual(self.e.one('SELECT total_score FROM game_participants WHERE game_id=? AND user_id=?',
                                   (second,self.a['user_id']))[0],0)

    def test_removed_commands_and_old_buttons_return_to_simple_menu(self):
        app=Application(self.e,'test_bot')
        allowed={b['callback_data'] for row in menu()['inline_keyboard'] for b in row}
        self.assertFalse(allowed & {'duel','duel_search','duel_friend','profile'})
        self.assertIn('history:0',allowed)
        events=[(command,'') for command in ('/duel','/challenge','/profile','/game OLD1')]
        events.extend(('',data) for data in ('duel','duel_search','duel_friend','profile','result:1','review:1:1'))
        for text,data in events:
            with self.subTest(text=text,data=data):
                app.route('101',text,data,self.clock())
                view=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='101' AND slot='room'")[0])
                self.assertIn('Главное меню',view['text'])
                self.assertEqual(view['reply_markup'],menu())
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM games')[0],0)
