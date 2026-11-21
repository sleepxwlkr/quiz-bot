import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import wait
from pathlib import Path
from unittest.mock import patch

from quizbot.app import Application
from quizbot.db import ROOT, connect, seed_questions
from quizbot.engine import CODE_ALPHABET, Engine, RuleError, mention, points_for, new_game_code
from quizbot.__main__ import Runner
from quizbot.telegram import TelegramError


class Clock:
    def __init__(self):
        self.now=1700000000.0

    def __call__(self):
        return self.now

    def advance(self,seconds):
        self.now+=seconds


class FakeClient:
    def __init__(self):
        self.calls=[]
        self.error=None

    def call(self,method,**kwargs):
        if self.error:
            raise self.error
        self.calls.append((method,kwargs))
        return {'message_id':kwargs.get('message_id',len(self.calls))}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.db'
        self.db=connect(self.path)
        seed_questions(self.db)
        self.clock=Clock()
        self.e=Engine(self.db,self.clock)
        self.runners=[]
        self.a=self.e.register(101,'Алиса')
        self.b=self.e.register(202,'Борис')

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.db.close()
        self.tmp.cleanup()

    def runner(self,client):
        runner=Runner(self.db,client,'test_bot')
        self.runners.append(runner)
        return runner

    def collect(self,runner):
        wait(list(runner.in_flight)+list(runner.acknowledgements),timeout=3)
        runner.collect_deliveries()

    def delivered(self,gid,user=None):
        for row in self.e.all("SELECT * FROM outbox WHERE kind='question' AND game_id=?",(gid,)):
            if user is None or row['user_id']==user['user_id']:
                self.e.confirm_delivery(row,100+row['user_id'],self.clock())

    def game(self,count=5,seconds=60):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',count,seconds)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        self.e.start(gid,self.a['user_id'])
        self.delivered(gid)
        return gid

    def answer_all(self,gid,u,correct=True,seconds=1):
        for q in self.e.all('SELECT * FROM game_questions WHERE game_id=? ORDER BY position',(gid,)):
            self.delivered(gid,u)
            self.clock.advance(seconds)
            selected=q['correct_answer_snapshot'] if correct else (q['correct_answer_snapshot']+1)%4
            self.e.answer(gid,u['user_id'],q['position'],selected)

    def test_nickname_uniqueness_and_stable_identity(self):
        with self.assertRaises(RuleError):
            self.e.register(303,'аЛиСа')
        updated=self.e.register(101,'Алиса 2')
        self.assertEqual(updated['user_id'],self.a['user_id'])
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM users')[0],2)
        with self.assertRaises(RuleError):
            self.e.register(303,'<a>')

    def test_bank_and_seed_idempotence(self):
        seed_questions(self.db)
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM questions')[0],60)
        self.assertEqual(len(self.e.available_questions('Информатика','hard')),10)
        self.assertEqual(len(self.e.available_questions('География','easy')),10)

    def test_owner_and_lobby_capacity(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',10,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        with self.assertRaises(RuleError):
            self.e.start(gid,self.b['user_id'])
        with self.assertRaises(RuleError):
            self.e.start(gid,self.a['user_id'])
        for i in range(9):
            user=self.e.register(1000+i,'Player%s'%i)
            self.e.join(user['user_id'],code)
        with self.assertRaises(RuleError):
            self.e.join(self.b['user_id'],code)
        self.assertEqual(len(self.e.participants(gid)),10)
        self.e.start(gid,self.a['user_id'])
        with self.assertRaises(RuleError):
            self.e.join(self.b['user_id'],code)

    def test_question_snapshots_and_same_order(self):
        gid=self.game()
        q=self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1',(gid,))
        self.db.execute("UPDATE questions SET question_text='Changed' WHERE question_id=?",(q['question_id'],))
        self.assertEqual(self.e.one('SELECT question_snapshot FROM game_questions WHERE game_id=? AND position=1',(gid,))[0],q['question_snapshot'])
        payloads=[]
        for p in self.e.participants(gid):
            payloads.append(self.e.question_payload({'game_id':gid,'user_id':p['user_id'],'position':1}))
        self.assertEqual(payloads[0],payloads[1])
        self.assertEqual(len(set(q['question_id'] for q in self.e.all('SELECT * FROM game_questions'))),5)

    def test_score_formula(self):
        self.assertEqual(points_for(10,0,60000,True),15)
        self.assertEqual(points_for(20,30000,60000,True),20)
        self.assertEqual(points_for(15,59999,60000,True),10)
        self.assertEqual(points_for(20,0,60000,False),0)

    def test_duplicate_and_foreign_answers_rejected(self):
        gid=self.game()
        q=self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1',(gid,))
        with self.assertRaises(RuleError):
            self.e.answer(gid,9999,1,0)
        self.e.answer(gid,self.a['user_id'],1,q['correct_answer_snapshot'])
        with self.assertRaises(RuleError):
            self.e.answer(gid,self.a['user_id'],1,q['correct_answer_snapshot'])
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM answers')[0],1)

    def test_timeout_automatically_advances(self):
        gid=self.game(count=10)
        self.clock.advance(60)
        self.e.tick()
        a=self.e.one('SELECT * FROM answers WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.assertEqual((a['answer_status'],a['is_correct'],a['points_awarded']),('timed_out',0,0))
        p=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.assertEqual(p['current_position'],2)
        self.assertEqual(p['question_deadline_at'],1700000120)
        self.assertIsNone(self.e.question_payload({'game_id':gid,'user_id':self.a['user_id'],'position':1}))

    def test_exact_deadline_rejects_answer(self):
        gid=self.game()
        self.clock.advance(60)
        with self.assertRaises(RuleError):
            self.e.answer(gid,self.a['user_id'],1,0)
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM answers')[0],2)

    def test_delayed_timer_check_finishes_at_the_original_deadline(self):
        gid=self.game(count=10)
        self.clock.advance(600)
        self.e.tick()
        self.e.tick()
        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'completed')
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM answers')[0],20)
        self.assertEqual([p['rank'] for p in self.e.participants(gid)],[1,1])
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM games WHERE status='completed'")[0],1)

    def test_fast_players_finish_before_global_deadline(self):
        gid=self.game()
        self.answer_all(gid,self.a)
        self.assertEqual(self.e.one('SELECT status FROM games')[0],'in_progress')
        self.answer_all(gid,self.b,False)
        self.assertEqual(self.e.one('SELECT status FROM games')[0],'completed')
        self.assertEqual(self.e.participants(gid)[0]['user_id'],self.a['user_id'])


    def test_forfeit_fills_remaining_and_loses(self):
        gid=self.game()
        self.e.leave(self.a['user_id'])
        with self.assertRaises(RuleError):
            self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        self.clock.advance(300)
        self.e.tick()
        results=self.e.participants(gid)
        self.assertEqual(results[0]['user_id'],self.b['user_id'])
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM answers WHERE answer_status='forfeited'")[0],5)
        self.assertFalse(self.e.active(self.a['user_id']))

    def test_lobby_cancel_does_not_complete_game(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        self.clock.advance(1800)
        self.e.tick()
        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'cancelled')

    def test_rename_keeps_historical_nickname(self):
        gid=self.game()
        self.e.register(101,'Новое имя')
        self.clock.advance(300)
        self.e.tick()
        result=self.e.results(gid)
        self.assertIn('tg://user?id=101',result)
        self.assertIn('Алиса',result)
        self.assertNotIn('Новое имя',result)


    def test_history_only_shows_games_the_player_joined(self):
        gid=self.game()
        app=Application(self.e,'test_bot')
        self.clock.advance(300)
        self.e.tick()
        self.e.register(303,'Третий')
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        for player,expected in (('101',f'<code>{code}</code>'),('303','Игр пока нет.')):
            app.route(player,'/history','',self.clock())
            self.assertIn(expected,json.loads(app.pending_response(player)['payload_json'])['text'])

    def test_new_player_invite_survives_registration(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        app=Application(self.e,'test_bot')
        app.handle({'message':{'from':{'id':303},'chat':{'id':303,'type':'private'},'text':'/start join_'+code}})
        app.handle({'message':{'from':{'id':303},'chat':{'id':303,'type':'private'},'text':'Новичок'}})
        self.assertEqual(len(self.e.participants(gid)),2)
        self.assertEqual(self.e.state(303),{})

    def test_duplicate_updates_and_malformed_callback(self):
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        update={'update_id':1,'message':{'from':{'id':303},'chat':{'id':303,'type':'private'},'text':'Игрок'}}
        runner.process(update,self.clock())
        before=self.e.one('SELECT COUNT(*) FROM outbox')[0]
        runner.process(update,self.clock())
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM outbox')[0],before)
        bad={'callback_query':{'id':'c','from':{'id':101},'message':{'chat':{'id':101,'type':'private'}},'data':'a:bad'}}
        self.assertIn('устарела',runner.app.handle(bad))

    def test_update_offsets_are_separate_for_each_telegram_bot(self):
        old_bot=self.runner(FakeClient())
        old_bot.offset_key='update_offset:100'
        old_bot.process({'update_id':9000,'message':{
            'from':{'id':101},'chat':{'id':101,'type':'private'},'text':'/start'}},self.clock())
        new_bot=self.runner(FakeClient())
        new_bot.offset_key='update_offset:200'
        before=self.e.one("SELECT MAX(outbox_id) FROM outbox WHERE chat_id='101'")[0]
        new_bot.process({'update_id':1,'message':{
            'from':{'id':101},'chat':{'id':101,'type':'private'},'text':'/start'}},self.clock())
        self.assertGreater(self.e.one("SELECT MAX(outbox_id) FROM outbox WHERE chat_id='101'")[0],before)
        self.assertEqual(self.e.one("SELECT value FROM settings WHERE key='update_offset:100'")[0],'9001')
        self.assertEqual(self.e.one("SELECT value FROM settings WHERE key='update_offset:200'")[0],'2')

    def test_outbox_retry_does_not_lose_result(self):
        self.e.send(101,'Persistent result')
        self.db.commit()
        client=FakeClient()
        runner=self.runner(client)
        client.error=TelegramError(429,'Too many requests',2)
        with patch('quizbot.__main__.time.time',return_value=self.clock()):
            runner.deliver()
            self.collect(runner)
        row=self.e.one('SELECT * FROM outbox')
        self.assertEqual(row['attempts'],1)
        self.clock.advance(3)
        client.error=None
        with patch('quizbot.__main__.time.time',return_value=self.clock()):
            runner.deliver()
            self.collect(runner)
        self.assertIsNone(self.e.one('SELECT * FROM outbox'))
        self.assertEqual(client.calls[0][1]['text'],'Persistent result')

    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO game_participants(game_id,user_id,nickname_at_game,joined_at) VALUES(?,?,?,?)',(999,999,'Ghost',0))

    def test_html_nickname_is_escaped(self):
        self.assertIn('&lt;',mention(101,'<x>'))

    def test_full_menu_flow_to_leaderboard_and_compact_history(self):
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        update_id=0
        payloads=[]
        def event(pid,text=None,data=None):
            nonlocal update_id
            update_id+=1
            update={'update_id':update_id}
            message={'from':{'id':pid},'chat':{'id':pid,'type':'private'}}
            if data is None:
                message['text']=text
                update['message']=message
            else:
                update['callback_query']={'id':str(update_id),'from':{'id':pid},'message':message,'data':data}
            runner.process(update,self.clock())
            response=self.e.one("SELECT payload_json FROM outbox WHERE chat_id=? AND slot='room'",(str(pid),))
            if response:
                payloads.append(json.loads(response[0]))
        event(101,data='create')
        token=self.e.state(101)['token']
        event(101,data='w:%s:count:5'%token)
        event(101,data='w:%s:seconds:15'%token)
        event(101,data='w:%s:save:yes'%token)
        game=self.e.active(self.a['user_id'])
        self.assertEqual(game['question_time_limit_seconds'],15)
        event(202,text='/join '+game['game_code'])
        event(101,data='start:%s'%game['game_id'])
        # An old start button must not duplicate the question snapshots.
        event(101,data='start:%s'%game['game_id'])
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM game_questions')[0],5)
        answer_ids=set()
        for q in self.e.all('SELECT * FROM game_questions ORDER BY position'):
            for pid in (101,202):
                self.delivered(game['game_id'],self.e.user(pid))
                self.clock.advance(1)
                selected=q['correct_answer_snapshot'] if pid==101 else (q['correct_answer_snapshot']+1)%4
                event(pid,data='a:%s:%s:%s'%(game['game_id'],q['position'],selected))
                answer_ids.add(str(update_id))
        self.collect(runner)
        replies={params['callback_query_id']:params['text'] for method,params in client.calls
                 if method=='answerCallbackQuery' and params['callback_query_id'] in answer_ids}
        self.assertEqual(replies,dict.fromkeys(answer_ids,''))
        self.assertEqual(self.e.one('SELECT status FROM games')[0],'completed')
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM outbox WHERE kind='question'")[0],0)
        result=payloads[-1]
        self.assertIn('Таблица лидеров',result['text'])
        self.assertEqual(result['reply_markup']['inline_keyboard'], [[
            {'text':'История игр','callback_data':'history:0'}, {'text':'Меню','callback_data':'menu'}]])
        event(101,data='history:0')
        history=payloads[-1]['text']
        self.assertIn('История игр',history)
        self.assertIn(game['game_code'],history)
        self.assertIn('Завершена · 1-е место,',history)
        self.assertTrue(all(len(p['text'])<=4096 for p in payloads))
        self.assertTrue(all(len(b['callback_data'].encode())<=64 for p in payloads
            for row in p.get('reply_markup',{}).get('inline_keyboard',[]) for b in row))

    def test_slow_send_does_not_block_other_commands_or_chats(self):
        started=threading.Event()
        release=threading.Event()
        class SlowClient(FakeClient):
            def call(self,method,**kwargs):
                if method=='sendMessage' and str(kwargs.get('chat_id'))=='101':
                    started.set()
                    release.wait(3)
                return super().call(method,**kwargs)
        client=SlowClient()
        runner=self.runner(client)
        self.e.send(101,'Slow recipient')
        self.db.commit()
        runner.deliver()
        self.assertTrue(started.wait(1))
        try:
            update={'update_id':10,'message':{'from':{'id':202},'chat':{'id':202,'type':'private'},'text':'/start'}}
            runner.process(update,self.clock())
            runner.deliver()
            other=[f for f,(row,_) in runner.in_flight.items() if row['chat_id']=='202']
            self.assertEqual(len(other),1)
            other[0].result(timeout=1)
            runner.collect_deliveries()
            self.assertTrue(any(kwargs.get('chat_id')=='202' for _,kwargs in client.calls))
            self.assertEqual(len(runner.in_flight),1)
        finally:
            release.set()
            self.collect(runner)

    def test_poll_offset_is_confirmed_only_after_batch_commit(self):
        stop=threading.Event()
        import queue
        events=queue.Queue()
        class PollClient(FakeClient):
            def call(self,method,**kwargs):
                super().call(method,**kwargs)
                if len(self.calls)==1:
                    return [{'update_id':7}]
                stop.set()
                return []
        client=PollClient()
        runner=self.runner(client)
        thread=threading.Thread(target=runner._poll,args=(events,stop,0))
        thread.start()
        try:
            event=events.get(timeout=1)
            self.assertEqual(len(client.calls),1)
            event[3].set()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(client.calls[1][1]['offset'],8)
        finally:
            stop.set()
            thread.join(timeout=1)

    def test_outbox_ids_never_reused_after_removing_inflight_question(self):
        self.e.send(101,'first')
        first=self.e.one('SELECT outbox_id FROM outbox')[0]
        self.db.execute('DELETE FROM outbox')
        self.e.send(101,'second')
        second=self.e.one('SELECT outbox_id FROM outbox')[0]
        self.assertGreater(second,first)
        self.db.execute('DELETE FROM outbox WHERE outbox_id=?',(first,))
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM outbox')[0],1)

    def test_ten_second_grace_and_fifty_percent_floor(self):
        for base in (10,15,20):
            maximum=base+5
            for limit in (15000,30000,60000,120000):
                self.assertEqual(points_for(base,9999,limit,True),maximum)
                self.assertEqual(points_for(base,10000,limit,True),maximum)
                self.assertEqual(points_for(base,limit,limit,True),(maximum+1)//2)
                previous=maximum
                for elapsed in range(0,limit+1,100):
                    score=points_for(base,elapsed,limit,True)
                    self.assertLessEqual(score,previous)
                    self.assertGreaterEqual(score,maximum/2)
                    self.assertLessEqual(score,maximum)
                    self.assertEqual(points_for(base,elapsed,limit,False),0)
                    previous=score
        self.assertEqual(points_for(15,35000,60000,True),15)

    def test_penalty_starts_at_delivery_not_queue_and_resend_cannot_reset_it(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        self.e.start(gid,self.a['user_id'])
        self.clock.advance(20)  # Slow API delivery must not consume the scoring grace.
        self.delivered(gid,self.a)
        self.clock.advance(10)
        self.delivered(gid,self.a)  # Re-displaying the same question is not a new grace.
        p=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.assertEqual(p['question_delivered_at'],1700000020)
        q=self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1',(gid,))
        self.e.answer(gid,self.a['user_id'],1,q['correct_answer_snapshot'])
        a=self.e.one('SELECT * FROM answers WHERE user_id=?',(self.a['user_id'],))
        self.assertEqual(a['response_ms'],10000)
        self.assertEqual(a['points_awarded'],q['base_points']+5)
        self.assertEqual(p['question_deadline_at'],1700000060)

    def test_pending_delivery_cannot_be_answered_or_old_delivery_apply_to_new_question(self):
        gid=self.game()
        row=self.e.one("SELECT * FROM outbox WHERE kind='question' AND user_id=?",(self.a['user_id'],))
        q=self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1',(gid,))
        self.e.answer(gid,self.a['user_id'],1,q['correct_answer_snapshot'])
        self.e.confirm_delivery(row,12345,self.clock())
        p=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.assertIsNone(p['question_delivered_at'])
        with self.assertRaises(RuleError):
            self.e.answer(gid,self.a['user_id'],2,0)

    def test_bot_start_cancels_open_games_without_resuming_or_replaying_them(self):
        completed=self.game()
        self.answer_all(completed,self.a)
        self.answer_all(completed,self.b,False)
        saved_result=self.e.results(completed)
        started=self.game()
        q=self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1',(started,))
        self.e.answer(started,self.a['user_id'],1,q['correct_answer_snapshot'])
        owner=self.e.register(303,'Вера')['user_id']
        lobby=self.e.create(owner,'Все темы','mixed',5,60)
        self.e.set_state(303,{'step':'join'})
        self.db.execute("INSERT INTO settings VALUES('update_offset:default','25')")
        self.db.commit()
        self.db.close()
        self.clock.advance(600)
        self.db=connect(self.path)
        self.e=Engine(self.db,self.clock)
        runner=self.runner(FakeClient())
        runner.engine=self.e
        with patch.object(runner,'_poll'), patch.object(runner,'deliver',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.run()

        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(completed,))[0],'completed')
        self.assertEqual(self.e.results(completed),saved_result)
        for game_id in (started,lobby):
            self.assertEqual(tuple(self.e.one('SELECT status,finished_at FROM games WHERE game_id=?',(game_id,))),
                             ('cancelled',self.clock()))
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM answers WHERE game_id=?',(started,))[0],1)
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM game_questions WHERE game_id=?',(started,))[0],5)
        self.assertIsNone(self.e.one('SELECT 1 FROM game_participants WHERE game_id=? AND question_deadline_at IS NOT NULL',
                                    (started,)))
        self.assertIsNone(self.e.one('SELECT 1 FROM outbox'))
        self.assertEqual(self.e.state(303),{})
        self.assertEqual(self.e.one("SELECT value FROM settings WHERE key='update_offset:default'")[0],'25')
        self.assertGreater(self.e.create(self.a['user_id'],'Все темы','mixed',5,60),lobby)

    def test_wizard_and_questions_edit_one_persistent_message(self):
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        def flush():
            with patch('quizbot.__main__.time.time',side_effect=self.clock):
                runner.deliver()
                self.collect(runner)
        runner.app.route('101','/create','',self.clock())
        flush()
        message=self.e.one("SELECT message_id FROM ui_messages WHERE chat_id='101' AND slot='room'")[0]
        token=self.e.state(101)['token']
        self.clock.advance(2)
        runner.app.route('101','','w:%s:count:5'%token,self.clock())
        flush()
        self.assertEqual(client.calls[-1][0],'editMessageText')
        self.assertEqual(client.calls[-1][1]['message_id'],message)
        self.clock.advance(2)
        runner.app.route('101','','w:%s:save:yes'%token,self.clock())
        flush()
        game=self.e.active(self.a['user_id'])
        self.e.join(self.b['user_id'],game['game_code'])
        self.clock.advance(2)
        self.e.start(game['game_id'],self.a['user_id'])
        flush()
        self.clock.advance(2)
        q=self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1',(game['game_id'],))
        self.e.answer(game['game_id'],self.a['user_id'],1,q['correct_answer_snapshot'])
        flush()
        calls=[(method,p) for method,p in client.calls if p.get('chat_id')=='101']
        self.assertEqual(sum(method=='sendMessage' for method,_ in calls),1)
        self.assertTrue(all(p.get('message_id')==message for method,p in calls if method=='editMessageText'))
        self.assertIn('Вопрос 2 из 5',calls[-1][1]['text'])
        self.clock.advance(300)
        self.e.tick()
        flush()
        self.assertIn('Итоги игры',[p for _,p in client.calls if p.get('chat_id')=='101'][-1]['text'])


    def test_five_character_codes_and_collision_retry(self):
        for _ in range(50):
            code=new_game_code()
            self.assertEqual(len(code),5)
            self.assertTrue(set(code)<=set(CODE_ALPHABET))
        with patch('quizbot.engine.new_game_code',return_value='S23J8'):
            first=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        self.e.cancel(first,'Cancelled')
        with patch('quizbot.engine.new_game_code',side_effect=['S23J8','AB234']) as generate:
            second=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
            self.assertEqual(generate.call_count,2)
        self.assertEqual(self.e.one('SELECT game_code FROM games WHERE game_id=?',(second,))[0],'AB234')
        self.assertEqual(self.e.join(self.b['user_id'],'  ab234  '),second)
        self.assertEqual(self.e.one('SELECT game_code FROM games WHERE game_id=?',(first,))[0],'S23J8')








    def test_unchanged_menu_edit_is_successful_without_duplicate_message(self):
        class UnchangedClient(FakeClient):
            def call(self,method,**kwargs):
                self.calls.append((method,kwargs))
                raise TelegramError(400,'Bad Request: message is not modified')
        client=UnchangedClient()
        runner=self.runner(client)
        runner.engine=self.e
        self.db.execute("INSERT INTO ui_messages VALUES('101','room',700)")
        self.e.send(101,'Same settings',slot='room')
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        self.assertEqual([method for method,_ in client.calls],['editMessageText'])
        self.assertIsNone(self.e.one('SELECT * FROM outbox'))
        self.assertEqual(self.e.one('SELECT message_id FROM ui_messages')[0],700)

    def test_cancel_then_create_uses_the_menu_that_was_clicked(self):
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        def click(data,message_id):
            self.clock.advance(2)
            runner.app.handle({'callback_query':{'id':'c','from':{'id':101},'data':data,
                'message':{'message_id':message_id,'chat':{'id':101,'type':'private'}}}},self.clock())
            with patch('quizbot.__main__.time.time',side_effect=self.clock):
                runner.deliver()
                self.collect(runner)
        click('create',500)
        old=self.e.one("SELECT message_id FROM ui_messages WHERE chat_id='101' AND slot='room'")[0]
        click('menu',old)
        self.assertEqual(self.e.state(101),{})
        # A newer main menu is now at the bottom of the chat.
        click('create',900)
        self.assertEqual(self.e.state(101)['step'],'wizard')
        edit=[params for method,params in client.calls if method=='editMessageText'][-1]
        self.assertEqual(edit['message_id'],900)

    def test_room_command_shows_lobby_at_bottom_of_chat(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.db.execute("INSERT INTO ui_messages VALUES('101','room',700)")
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        runner.process({'update_id':1,'message':{'from':{'id':101},
            'chat':{'id':101,'type':'private'},'text':'/room'}},self.clock())
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        self.assertEqual(client.calls[0][0],'sendMessage')
        self.assertIn(code,client.calls[0][1]['text'])

    def test_room_refresh_keeps_question_deadline_and_scoring_start(self):
        gid=self.game()
        before=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.clock.advance(20)
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        runner.process({'update_id':1,'message':{'from':{'id':101},
            'chat':{'id':101,'type':'private'},'text':'/room'}},self.clock())
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        p=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.assertEqual(p['question_deadline_at'],before['question_deadline_at'])
        self.assertEqual(p['question_delivered_at'],before['question_delivered_at'])
        self.assertEqual(p['current_position'],1)
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM answers')[0],0)
        question=[params for method,params in client.calls if method=='sendMessage' and params.get('chat_id')=='101'][0]
        self.assertIn('Вопрос 1 из 5',question['text'])
        q=self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=1',(gid,))
        self.e.answer(gid,self.a['user_id'],1,q['correct_answer_snapshot'])
        score=self.e.one('SELECT points_awarded FROM answers WHERE user_id=?',(self.a['user_id'],))[0]
        self.assertEqual(score,points_for(q['base_points'],20000,60000,True))
        self.assertLess(score,q['base_points']+5)

    def test_room_callback_opens_the_clicked_menu(self):
        self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        self.db.execute("INSERT INTO ui_messages VALUES('101','room',700)")
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        runner.process({'update_id':1,'callback_query':{'id':'room','from':{'id':101},'data':'room',
            'message':{'message_id':900,'chat':{'id':101,'type':'private'}}}},self.clock())
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        edits=[params for method,params in client.calls if method=='editMessageText']
        self.assertEqual(len(edits),1)
        self.assertEqual(edits[0]['message_id'],900)

    def test_room_when_no_game_and_after_completion_does_not_reopen_it(self):
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        def request(number):
            runner.process({'update_id':number,'message':{'from':{'id':101},
                'chat':{'id':101,'type':'private'},'text':'/room'}},self.clock())
            last=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='101' AND slot='room'")[0])
            self.assertIn('нет активной игры',last['text'])
            self.assertIn('/history',last['text'])
        request(1)
        gid=self.game()
        self.clock.advance(300)
        self.e.tick()
        request(2)
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM games')[0],1)
        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'completed')
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM outbox WHERE kind='question'")[0],0)

    def test_room_advances_elapsed_question_before_showing_it(self):
        gid=self.game()
        self.clock.advance(60)
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        runner.process({'update_id':1,'message':{'from':{'id':101},
            'chat':{'id':101,'type':'private'},'text':'/room'}},self.clock())
        row=self.e.one("SELECT * FROM outbox WHERE kind='question' AND user_id=?",(self.a['user_id'],))
        self.assertEqual(row['position'],2)
        self.assertIn('Вопрос 2 из 5',self.e.question_payload(row)['text'])
        self.assertEqual(self.e.one("SELECT COUNT(*) FROM answers WHERE answer_status='timed_out'")[0],2)
        runner.process({'update_id':2,'message':{'from':{'id':101},
            'chat':{'id':101,'type':'private'},'text':'/room'}},self.clock())
        self.assertEqual(self.e.one('SELECT COUNT(*) FROM answers')[0],2)

    def test_room_shows_waiting_after_finishing_or_forfeiting_without_leave_button(self):
        app=Application(self.e,'test_bot')
        for status in ('finished','forfeited'):
            gid=self.game()
            if status=='finished':
                self.answer_all(gid,self.a)
            else:
                self.e.leave(self.a['user_id'])
            app.route('101','/room','',self.clock())
            row=self.e.one("SELECT * FROM outbox WHERE chat_id='101' AND slot='room'")
            payload=json.loads(row['payload_json'])
            self.assertIn('когда закончат остальные',payload['text'])
            data=[button['callback_data'] for buttons in payload['reply_markup']['inline_keyboard'] for button in buttons]
            self.assertNotIn('leave',data)
            self.assertIn('room',data)
            self.assertEqual(row['kind'],'message')
            self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'in_progress')
            self.clock.advance(300)
            self.e.tick()


    def test_room_does_not_delay_scoring_when_old_question_delivery_finishes_late(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        self.e.start(gid,self.a['user_id'])
        original=self.e.one("SELECT * FROM outbox WHERE kind='question' AND user_id=?",(self.a['user_id'],))
        app=Application(self.e,'test_bot')
        app.route('101','/room','',self.clock())
        latest=self.e.one("SELECT * FROM outbox WHERE kind='question' AND user_id=?",(self.a['user_id'],))
        self.clock.advance(2)
        self.e.confirm_delivery(original,700,self.clock())
        self.clock.advance(15)
        self.e.confirm_delivery(latest,900,self.clock())
        p=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.a['user_id']))
        self.assertEqual(p['question_delivered_at'],1700000002)
        self.assertEqual(p['question_deadline_at'],1700000060)
        self.assertEqual(self.e.one("SELECT message_id FROM ui_messages WHERE chat_id='101'")[0],900)

    def test_create_command_after_cancel_sends_settings_at_bottom(self):
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        app=Application(self.e,'test_bot')
        self.db.execute("INSERT INTO ui_messages VALUES('101','room',700)")
        app.route('101','/create','',self.clock())
        self.assertIsNone(self.e.one("SELECT message_id FROM ui_messages WHERE chat_id='101'"))
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        self.assertEqual(client.calls[0][0],'sendMessage')
        self.assertIn('Настройки комнаты',client.calls[0][1]['text'])
        self.clock.advance(2)
        app.route('101','/cancel','',self.clock())
        self.assertEqual(self.e.state(101),{})
        self.clock.advance(2)
        app.route('101','/create','',self.clock())
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        self.assertEqual(client.calls[-1][0],'sendMessage')
        self.assertEqual(self.e.state(101)['step'],'wizard')
        self.assertIsNone(self.e.active(self.a['user_id']))

    def test_late_delivery_cannot_rebind_panel_after_cancel_and_reopen(self):
        app=Application(self.e,'test_bot')
        app.route('101','','create',self.clock(),500)
        old=self.e.one("SELECT * FROM outbox WHERE slot='room'")
        app.route('101','','menu',self.clock(),500)
        app.route('101','','create',self.clock(),900)
        self.e.confirm_delivery(old,500,self.clock())
        self.assertEqual(self.e.one("SELECT message_id FROM ui_messages WHERE chat_id='101'")[0],900)
        cleanup=[json.loads(row[0]) for row in self.e.all("SELECT payload_json FROM outbox WHERE kind='api'")]
        self.assertTrue(any(p['params']['message_id']==500 for p in cleanup))

    def test_stale_wizard_buttons_cannot_save_after_cancel(self):
        app=Application(self.e,'test_bot')
        app.route('101','','create',self.clock(),500)
        old_token=self.e.state(101)['token']
        app.route('101','','menu',self.clock(),500)
        app.route('101','','create',self.clock(),500)
        new_token=self.e.state(101)['token']
        self.assertNotEqual(old_token,new_token)
        reply=app.handle({'callback_query':{'id':'old','from':{'id':101},'data':'w:%s:save:yes'%old_token,
            'message':{'message_id':500,'chat':{'id':101,'type':'private'}}}},self.clock())
        self.assertIn('устарели',reply)
        self.assertEqual(self.e.state(101)['token'],new_token)
        self.assertIsNone(self.e.active(self.a['user_id']))

    def test_deleted_panel_is_recreated_and_next_update_edits_new_message(self):
        class DeletedClient(FakeClient):
            def call(self,method,**kwargs):
                if method=='editMessageText' and kwargs.get('message_id')==700:
                    self.calls.append((method,kwargs))
                    raise TelegramError(400,'Bad Request: message to edit not found')
                return super().call(method,**kwargs)
        client=DeletedClient()
        runner=self.runner(client)
        runner.engine=self.e
        self.db.execute("INSERT INTO ui_messages VALUES('101','room',700)")
        self.e.send(101,'Settings',slot='room')
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        new_id=self.e.one('SELECT message_id FROM ui_messages')[0]
        self.assertNotEqual(new_id,700)
        self.clock.advance(2)
        self.e.send(101,'Changed settings',slot='room')
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        self.assertEqual(client.calls[-1][0],'editMessageText')
        self.assertEqual(client.calls[-1][1]['message_id'],new_id)

    def test_lobby_owner_has_delete_button_and_guest_has_leave(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        app=Application(self.e,'test_bot')
        for user,expected in ((self.a,'delete:%s'%gid),(self.b,'leave')):
            app.lobby(user,gid)
            payload=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id=? AND slot='room'",
                                         (user['platform_user_id'],))[0])
            buttons=[button for row in payload['reply_markup']['inline_keyboard'] for button in row]
            self.assertIn(expected,[button['callback_data'] for button in buttons])
            self.assertEqual(any(button['text']=='Удалить' for button in buttons),user==self.a)

    def test_delete_lobby_requires_confirmation_and_opens_main_menu(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        app=Application(self.e,'test_bot')
        app.route('101','','delete:%s'%gid,self.clock(),500)
        confirmation=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='101' AND slot='room'")[0])
        self.assertIn('Удалить комнату',confirmation['text'])
        buttons=[b['callback_data'] for row in confirmation['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn('delete_yes:%s'%gid,buttons)
        self.assertIn('room',buttons)
        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'lobby')
        app.route('101','','delete_yes:%s'%gid,self.clock(),500)
        with patch('quizbot.__main__.time.time',side_effect=self.clock):
            runner.deliver()
            self.collect(runner)
        owner=[params for method,params in client.calls if params.get('chat_id')=='101']
        self.assertEqual(len(owner),1)
        self.assertEqual(owner[0]['message_id'],500)
        self.assertIn('Главное меню',owner[0]['text'])
        actions=[b['callback_data'] for row in owner[0]['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn('create',actions)
        self.assertIn('join',actions)
        self.assertIsNone(self.e.active(self.a['user_id']))
        self.assertIsNone(self.e.active(self.b['user_id']))
        self.assertEqual(self.e.state(101),{})
        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'cancelled')

    def test_delete_lobby_cannot_be_used_by_guest_or_after_start(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        app=Application(self.e,'test_bot')
        def press(pid,data):
            return app.handle({'callback_query':{'id':'delete','from':{'id':pid},'data':data,
                'message':{'message_id':500,'chat':{'id':pid,'type':'private'}}}},self.clock())
        self.assertIn('создатель',press(202,'delete:%s'%gid))
        self.assertIn('создатель',press(202,'delete_yes:%s'%gid))
        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'lobby')
        app.route('101','','delete:%s'%gid,self.clock(),500)
        self.e.start(gid,self.a['user_id'])
        self.assertIn('игра началась',press(101,'delete_yes:%s'%gid))
        self.assertEqual(self.e.one('SELECT status FROM games WHERE game_id=?',(gid,))[0],'in_progress')
        self.assertTrue(all(p['participant_status']=='playing' for p in self.e.participants(gid)))

    def test_delete_cancel_returns_to_lobby_and_owner_leave_uses_delete_confirmation(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        app=Application(self.e,'test_bot')
        app.route('101','/leave','',self.clock())
        payload=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='101' AND slot='room'")[0])
        self.assertIn('Удалить комнату',payload['text'])
        app.route('101','','room',self.clock(),500)
        payload=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='101' AND slot='room'")[0])
        self.assertIn('Как пригласить',payload['text'])
        self.assertEqual(self.e.active(self.a['user_id'])['game_id'],gid)
        buttons=[b['callback_data'] for row in payload['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn('delete:%s'%gid,buttons)

    def test_guest_leave_replaces_room_then_opens_menu_and_allows_reentry(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        self.db.execute("INSERT INTO ui_messages VALUES('202','room',600)")
        client=FakeClient()
        runner=self.runner(client)
        runner.engine=self.e
        runner.app=Application(self.e,'test_bot')
        def click(number,data):
            runner.process({'update_id':number,'callback_query':{'id':str(number),'from':{'id':202},
                'data':data,'message':{'message_id':600,'chat':{'id':202,'type':'private'}}}},self.clock())
            with patch('quizbot.__main__.time.time',side_effect=self.clock):
                runner.deliver()
                self.collect(runner)
        click(1,'leave')
        guest=[p for method,p in client.calls if method=='editMessageText' and p.get('chat_id')=='202']
        self.assertEqual(len(guest),1)
        self.assertEqual(guest[0]['message_id'],600)
        self.assertEqual(guest[0]['text'],'Точно хотите покинуть комнату?')
        self.assertNotIn('очк',guest[0]['text'])
        self.assertEqual(len(self.e.participants(gid)),2)
        confirmation=next(b['callback_data'] for row in guest[0]['reply_markup']['inline_keyboard']
                          for b in row if b['text']=='Да, выйти')
        self.clock.advance(2)
        click(2,confirmation)
        guest=[p for method,p in client.calls if method=='editMessageText' and p.get('chat_id')=='202']
        self.assertEqual(guest[-1]['message_id'],600)
        self.assertIn('Главное меню',guest[-1]['text'])
        actions=[b['callback_data'] for row in guest[-1]['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn('create',actions)
        self.assertIn('join',actions)
        self.assertEqual(self.e.state(202),{})
        self.assertIsNone(self.e.active(self.b['user_id']))
        self.assertEqual(len(self.e.participants(gid)),1)
        self.assertEqual(self.e.active(self.a['user_id'])['game_id'],gid)
        for text in ('/start join_'+code,'/join '+code.lower()):
            runner.app.handle({'message':{'from':{'id':202},'chat':{'id':202,'type':'private'},'text':text}},self.clock())
            self.assertEqual(self.e.active(self.b['user_id'])['game_id'],gid)
            self.assertEqual(len(self.e.participants(gid)),2)
            p=self.e.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.b['user_id']))
            self.assertEqual(p['participant_status'],'joined')
            self.assertEqual(p['total_score'],0)
            reply=runner.app.handle({'callback_query':{'id':'old','from':{'id':202},'data':confirmation,
                'message':{'message_id':600,'chat':{'id':202,'type':'private'}}}},self.clock())
            self.assertIn('устарело',reply)
            self.assertEqual(len(self.e.participants(gid)),2)
            self.e.leave(self.b['user_id'])

    def test_guest_can_cancel_leave_and_old_confirmation_is_invalid(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        app=Application(self.e,'test_bot')
        app.route('202','','leave',self.clock(),600)
        token=self.e.state(202)['token']
        app.route('202','','room',self.clock(),600)
        payload=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='202' AND slot='room'")[0])
        self.assertIn(code,payload['text'])
        self.assertEqual(self.e.state(202),{})
        reply=app.handle({'callback_query':{'id':'old','from':{'id':202},'data':'leave_yes:%s:%s'%(gid,token),
            'message':{'message_id':600,'chat':{'id':202,'type':'private'}}}},self.clock())
        self.assertIn('устарело',reply)
        self.assertEqual(self.e.active(self.b['user_id'])['game_id'],gid)
        self.assertEqual(len(self.e.participants(gid)),2)

    def test_game_start_during_lobby_exit_requires_a_new_warning_and_confirmation(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        app=Application(self.e,'test_bot')
        app.route('202','','leave',self.clock(),600)
        original=self.e.state(202)['token']
        self.e.start(gid,self.a['user_id'])
        app.route('202','','leave_yes:%s:%s'%(gid,original),self.clock(),600)
        state=self.e.state(202)
        self.assertEqual(state['phase'],'in_progress')
        self.assertNotEqual(state['token'],original)
        payload=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='202' AND slot='room'")[0])
        self.assertIn('0 очков',payload['text'])
        self.assertNotIn('сдача',payload['text'])
        self.assertTrue(all(p['participant_status']=='playing' for p in self.e.participants(gid)))
        app.route('202','','leave_yes:%s:%s'%(gid,state['token']),self.clock(),600)
        p=self.e.one('SELECT participant_status FROM game_participants WHERE game_id=? AND user_id=?',(gid,self.b['user_id']))
        self.assertEqual(p[0],'forfeited')
        self.assertEqual(self.e.state(202),{})
        menu=json.loads(self.e.one("SELECT payload_json FROM outbox WHERE chat_id='202' AND slot='room'")[0])
        self.assertIn('Главное меню',menu['text'])
        with self.assertRaises(RuleError):
            self.e.join(self.b['user_id'],code)


    def test_reentry_requires_a_free_slot_in_the_same_lobby(self):
        gid=self.e.create(self.a['user_id'],'Все темы','mixed',5,60)
        code=self.e.one('SELECT game_code FROM games WHERE game_id=?',(gid,))[0]
        self.e.join(self.b['user_id'],code)
        for number in range(8):
            user=self.e.register(1000+number,'Player%s'%number)
            self.e.join(user['user_id'],code)
        self.assertEqual(len(self.e.participants(gid)),10)
        self.e.leave(self.b['user_id'])
        newcomer=self.e.register(2000,'Новичок')
        self.e.join(newcomer['user_id'],code)
        with self.assertRaises(RuleError):
            self.e.join(self.b['user_id'],code)
        self.e.leave(newcomer['user_id'])
        self.assertEqual(self.e.join(self.b['user_id'],code.lower()),gid)
        self.assertEqual(len(self.e.participants(gid)),10)

    def test_ranking_uses_score_then_accuracy_then_time_and_exact_ties(self):
        cases=[
            ((50,2,9000),(49,5,1000),(1,2)),
            ((50,4,9000),(50,3,1000),(1,2)),
            ((50,3,1000),(50,3,2000),(1,2)),
            ((50,3,2000),(50,3,1000),(2,1)),
            ((50,3,1000),(50,3,1000),(1,1)),
        ]
        for first,second,expected in cases:
            gid=self.game()
            for user,data in ((self.a,first),(self.b,second)):
                self.db.execute("""UPDATE game_participants SET total_score=?,correct_count=?,total_response_ms=?,
                    participant_status='finished' WHERE game_id=? AND user_id=?""",(*data,gid,user['user_id']))
            self.e.finalize(gid)
            ranks=tuple(self.e.one('SELECT rank FROM game_participants WHERE game_id=? AND user_id=?',
                                  (gid,user['user_id']))[0] for user in (self.a,self.b))
            self.assertEqual(ranks,expected)


if __name__=='__main__':
    unittest.main()
