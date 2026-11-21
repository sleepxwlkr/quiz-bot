import json
import tempfile
import unittest
from concurrent.futures import wait
from pathlib import Path
from unittest.mock import patch

from quizbot.app import Application
from quizbot.db import connect, seed_questions
from quizbot.engine import Engine
from quizbot.__main__ import Runner
from quizbot.telegram import TelegramError
from tests.test_bot import Clock, FakeClient


class VisibleClient(FakeClient):
    """Model the bot messages actually remaining in a Telegram chat."""
    def __init__(self):
        super().__init__()
        self.messages={}

    def call(self,method,**params):
        chat=str(params.get('chat_id',''))
        key=(chat,params.get('message_id'))
        if method=='editMessageText' and key not in self.messages:
            raise TelegramError(400,'Bad Request: message to edit not found')
        result=super().call(method,**params)
        if method in ('sendMessage','editMessageText'):
            self.messages[(chat,result['message_id'])]=params
        elif method=='deleteMessage':
            self.messages.pop(key,None)
            return True
        return result


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'screens.db')
        seed_questions(self.db)
        self.clock=Clock()
        self.e=Engine(self.db,self.clock)
        self.a=self.e.register(101,'Алиса')
        self.b=self.e.register(202,'Борис')
        self.client=VisibleClient()
        self.runner=Runner(self.db,self.client,'test_bot')
        self.runner.engine=self.e
        self.runner.app=Application(self.e,'test_bot')
        self.update_id=0

    def tearDown(self):
        self.runner.close()
        self.db.close()
        self.tmp.cleanup()

    def flush(self):
        for _ in range(20):
            with patch('quizbot.__main__.time.time',side_effect=self.clock):
                self.runner.deliver()
                wait(list(self.runner.in_flight)+list(self.runner.acknowledgements),timeout=3)
                self.runner.collect_deliveries()
            if not self.e.one('SELECT 1 FROM outbox') and not self.runner.in_flight:
                return
            self.clock.advance(1.1)
        self.fail('The outgoing queue did not drain')

    def request(self,text=None,data=None,pid=101,message_id=None):
        self.clock.advance(2)
        self.update_id+=1
        update={'update_id':self.update_id}
        msg={'from':{'id':pid},'chat':{'id':pid,'type':'private'}}
        if data is not None:
            if message_id is None:
                message_id=self.current_id(pid)
            msg['message_id']=message_id
            update['callback_query']={'id':str(self.update_id),'from':{'id':pid},'message':msg,'data':data}
        else:
            msg['text']=text
            update['message']=msg
        self.runner.process(update,self.clock())
        self.flush()
        return self.current(pid)

    def current_id(self,pid=101):
        return self.e.one("SELECT message_id FROM ui_messages WHERE chat_id=? AND slot='room'",(str(pid),))[0]

    def current(self,pid=101):
        self.assertEqual(sum(chat==str(pid) for chat,_ in self.client.messages),1)
        return self.client.messages[(str(pid),self.current_id(pid))]

    def complete_game(self):
        gid=self.e.create(self.a['user_id'],'room','Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        self.e.start(gid,self.a['user_id'])
        self.clock.advance(300)
        self.e.tick()
        return gid

    def test_all_main_menu_buttons_update_the_same_screen(self):
        self.request(text='/start')
        message_id=self.current_id()
        screens=[
            ('profile','Профиль'),('history:0','История игр'),('top','Рейтинг дуэлей'),
            ('help','Квиз-бот'),('nick','Введите новый ник'),('menu','Выберите действие'),
            ('join','Введите код'),('menu','Выберите действие'),('duel','Дуэли'),
            ('menu','Выберите действие'),('create','Настройки комнаты'),
        ]
        for data,expected in screens:
            with self.subTest(screen=data):
                view=self.request(data=data)
                self.assertIn(expected,view['text'])
                self.assertEqual(self.current_id(),message_id)
        token=self.e.state(101)['token']
        self.assertIn('5',self.request(data='w:%s:count:5'%token)['text'])
        self.request(data='menu')
        self.assertEqual(sum(method=='sendMessage' for method,_ in self.client.calls),1)
        self.assertTrue(all('_panel_epoch' not in params for _,params in self.client.calls))

    def test_history_pagination_results_and_reviews_share_one_screen(self):
        games=[self.complete_game() for _ in range(6)]
        self.request(text='/history')
        message_id=self.current_id()
        self.assertIn('страница 2',self.request(data='history:1')['text'])
        self.assertIn('Итоги:',self.request(data='result:%s'%games[0])['text'])
        self.assertIn('вопрос 1/5',self.request(data='review:%s:1'%games[0])['text'])
        self.assertIn('вопрос 2/5',self.request(data='review:%s:2'%games[0])['text'])
        self.assertIn('История игр',self.request(data='history:0')['text'])
        self.assertEqual(self.current_id(),message_id)

    def test_registration_and_input_errors_replace_previous_prompts(self):
        self.assertIn('Введите ник',self.request(text='/start',pid=303)['text'])
        self.assertIn('от 3 до 24',self.request(text='x',pid=303)['text'])
        self.assertIn('Регистрация завершена',self.request(text='Новый игрок',pid=303)['text'])
        self.assertEqual(self.e.user(303)['nickname'],'Новый игрок')
        self.assertIn('Профиль',self.request(data='profile',pid=303)['text'])
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM ui_messages WHERE chat_id='303'")[0],1)

    def test_rename_prompt_clears_buttons_and_accepts_text_in_one_screen(self):
        self.request(text='/start')
        view=self.request(data='nick')
        self.assertEqual(view['reply_markup']['inline_keyboard'],[])
        self.assertIn('Теперь ваш ник',self.request(text='Алиса 2')['text'])
        self.assertEqual(self.e.user(101)['nickname'],'Алиса 2')
        self.assertIn('Алиса 2',self.request(data='profile')['text'])
        self.assertEqual(len(self.client.messages),1)

    def test_text_commands_replace_and_remove_previous_bot_screen(self):
        self.request(text='/start')
        old=self.current_id()
        for command,title in [('/profile','Профиль'),('/history','История игр'),('/help','Квиз-бот'),('/top','Рейтинг дуэлей')]:
            self.assertIn(title,self.request(text=command)['text'])
            self.assertNotEqual(old,self.current_id())
            self.assertNotIn(('101',old),self.client.messages)
            old=self.current_id()

    def test_join_error_and_cancel_do_not_leave_old_menus(self):
        self.request(text='/start')
        self.request(data='join')
        self.assertIn('Комната не найдена',self.request(text='NOT-A-ROOM')['text'])
        self.assertIn('Выберите действие',self.request(text='/cancel')['text'])
        self.assertEqual(self.e.state(101),{})
        self.assertEqual(len(self.client.messages),1)

    def test_profile_and_back_to_game_keep_question_time(self):
        gid=self.e.create(self.a['user_id'],'room','Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        self.e.start(gid,self.a['user_id'])
        self.flush()
        before=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.clock.advance(20)
        self.request(data='profile')
        message_id=self.current_id()
        self.assertIn('Вопрос 1/5',self.request(data='room')['text'])
        after=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.assertEqual(self.current_id(),message_id)
        self.assertEqual(before['question_deadline_at'],after['question_deadline_at'])
        self.assertEqual(before['question_delivered_at'],after['question_delivered_at'])
        self.assertEqual(after['current_position'],1)

    def test_untracked_old_menu_becomes_the_only_active_screen(self):
        self.request(text='/start')
        original=self.current_id()
        old_menu=self.client.call('sendMessage',chat_id='101',text='An older menu')['message_id']
        self.assertIn('Профиль',self.request(data='profile',message_id=old_menu)['text'])
        self.assertEqual(self.current_id(),old_menu)
        self.assertNotIn(('101',original),self.client.messages)

    def test_deleted_screen_is_recreated_for_profile_navigation(self):
        self.request(text='/start')
        deleted=self.current_id()
        self.client.messages.pop(('101',deleted))
        self.assertIn('Профиль',self.request(data='profile',message_id=deleted)['text'])
        self.assertNotEqual(self.current_id(),deleted)

    def test_fast_navigation_queues_only_latest_screen(self):
        self.request(text='/start')
        message_id=self.current_id()
        for data in ('profile','history:0','help','top'):
            self.runner.app.route('101','',data,self.clock(),message_id)
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM outbox WHERE chat_id='101' AND slot='room'")[0],1)
        self.flush()
        self.assertIn('Рейтинг дуэлей',self.current()['text'])
        self.assertEqual(self.current_id(),message_id)
