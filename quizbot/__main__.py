import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .admin import parse_admin_ids
from .app import Application
from .db import ROOT, connect, initialize_question_bank, seed_questions
from .engine import Engine
from .telegram import TelegramClient, TelegramError

log = logging.getLogger('quizbot')


def load_env():
    path = ROOT / '.env'
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key,value = line.split('=',1)
        os.environ.setdefault(key.strip(),value.strip().strip('\"\''))


class InstanceLock:
    """OS lock automatically released on process exit, including crashes."""
    def __init__(self,path):
        self.path = Path(str(path)+'.lock')

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another bot process is already using this database.') from None
        return self

    def __exit__(self,*args):
        self.file.close()


class Runner:
    def __init__(self,db,client,username,bot_id='default',admin_ids=()):
        self.db=db
        self.client=client
        self.engine=Engine(db)
        self.app=Application(self.engine,username,admin_ids)
        self.offset_key='update_offset:%s'%bot_id
        self.last_send={}
        self.api_available_at=0
        self.executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix='telegram-send')
        self.in_flight={}
        self.acknowledgements=set()

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)
        self.collect_deliveries()

    def tick(self):
        with self.db:
            self.engine.tick()

    def process(self,update,received_at):
        self.collect_deliveries()
        previous=self.engine.one('SELECT value FROM settings WHERE key=?',(self.offset_key,))
        if previous and update['update_id']<int(previous[0]):
            return
        try:
            with self.db:
                # Recheck deadlines before accepting any answer.
                self.engine.tick()
                reply=self.app.handle(update,received_at)
                self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                                (self.offset_key,str(update['update_id']+1)))
        except Exception:
            # Do not confirm an update if its database transaction failed.
            log.exception('Update transaction failed (update_id=%s)',update['update_id'])
            raise
        callback=update.get('callback_query')
        if callback:
            self.acknowledgements.add(self.executor.submit(self.client.call,
                'answerCallbackQuery',callback_query_id=callback['id'],text=reply[:200]))

    def collect_deliveries(self):
        """Only this main thread touches SQLite; workers perform HTTP requests."""
        for future in list(self.acknowledgements):
            if not future.done():
                continue
            self.acknowledgements.remove(future)
            if future.cancelled():
                continue
            try:
                future.result()
            except TelegramError as exc:
                if exc.code==401:
                    raise
                log.warning('Callback acknowledgement failed: code=%s',exc.code)
        for future,(row,started) in list(self.in_flight.items()):
            if not future.done():
                continue
            del self.in_flight[future]
            if future.cancelled():
                continue
            try:
                result,delivered_at=future.result()
            except TelegramError as exc:
                if exc.code==401:
                    raise
                if exc.code in (400,403):
                    log.warning('Undeliverable message: outbox=%s, code=%s',row['outbox_id'],exc.code)
                    with self.db:
                        self.db.execute('DELETE FROM outbox WHERE outbox_id=?',(row['outbox_id'],))
                else:
                    wait=max(exc.retry_after,min(60,2**min(row['attempts']+1,6)))
                    with self.db:
                        self.db.execute('UPDATE outbox SET attempts=attempts+1,available_at=? WHERE outbox_id=?',
                                        (time.time()+wait,row['outbox_id']))
                    self.api_available_at=time.time()+max(exc.retry_after,1)
                continue
            log.debug('Message delivered: network=%.0f ms, total_queue=%.0f ms',
                      (time.perf_counter()-started)*1000,(time.time()-row['created_at'])*1000)
            self.last_send[row['chat_id']]=time.time()
            with self.db:
                if row['slot'] or row['kind']=='question':
                    self.engine.confirm_delivery(row,result['message_id'],delivered_at)
                self.db.execute('DELETE FROM outbox WHERE outbox_id=?',(row['outbox_id'],))

    def _send_message(self,method,params):
        try:
            result=self.client.call(method,**params)
        except TelegramError as exc:
            description=exc.description.lower()
            if method=='editMessageText' and exc.code==400 and 'message is not modified' in description:
                result={'message_id':params['message_id']}
            elif method=='editMessageText' and exc.code==400 and (
                    'message to edit not found' in description or "message can't be edited" in description):
                params=dict(params)
                params.pop('message_id')
                result=self.client.call('sendMessage',**params)
            else:
                raise
        return result,self.engine.clock()

    def deliver(self,limit=10):
        self.collect_deliveries()
        self.tick()
        now=time.time()
        if now<self.api_available_at:
            return
        busy={row['chat_id'] for row,_ in self.in_flight.values()}
        rows=self.engine.all("SELECT * FROM outbox WHERE available_at<=? ORDER BY (kind='api'),outbox_id LIMIT 100",(now,))
        dispatched=0
        for row in rows:
            if dispatched>=limit or len(self.in_flight)>=4:
                break
            if row['chat_id'] in busy or now-self.last_send.get(row['chat_id'],0)<1.05:
                continue
            if row['slot'] and json.loads(row['payload_json']).get('_panel_epoch',0)!=self.engine.panel_epoch(row['chat_id']):
                with self.db:
                    self.db.execute('DELETE FROM outbox WHERE outbox_id=?',(row['outbox_id'],))
                continue
            payload=self.engine.question_payload(row) if row['kind']=='question' else json.loads(row['payload_json'])
            if payload is None:
                with self.db:
                    self.db.execute('DELETE FROM outbox WHERE outbox_id=?',(row['outbox_id'],))
                continue
            payload.pop('_panel_epoch',None)
            params={'chat_id':row['chat_id'],**payload}
            target=self.engine.one('SELECT message_id FROM ui_messages WHERE chat_id=? AND slot=?',
                                   (row['chat_id'],row['slot'])) if row['slot'] else None
            method='editMessageText' if target else 'sendMessage'
            if row['kind']=='api':
                method=payload['method']
                params={'chat_id':row['chat_id'],**payload['params']}
                current=self.engine.one("SELECT message_id FROM ui_messages WHERE chat_id=? AND slot='room'",
                                        (row['chat_id'],))
                if method=='deleteMessage' and current and current[0]==params['message_id']:
                    # Rapid navigation can select a panel whose old cleanup is still queued.
                    with self.db:
                        self.db.execute('DELETE FROM outbox WHERE outbox_id=?',(row['outbox_id'],))
                    continue
            if row['slot']:
                params.setdefault('reply_markup',{'inline_keyboard':[]})
            if target:
                params['message_id']=target['message_id']
            future=self.executor.submit(self._send_message,method,params)
            self.in_flight[future]=(row,time.perf_counter())
            busy.add(row['chat_id'])
            dispatched+=1

    def _poll(self,events,stop,offset):
        """Fetch immediately on arrival; confirm a batch only after DB commits."""
        backoff=1
        while not stop.is_set():
            try:
                updates=self.client.call('getUpdates',offset=offset,
                    timeout=20,limit=30,allowed_updates=['message','callback_query'])
                if updates:
                    handled=threading.Event()
                    events.put(('updates',updates,time.time(),handled))
                    while not handled.wait(0.1):
                        if stop.is_set():
                            return
                    if stop.is_set():
                        return
                    offset=updates[-1]['update_id']+1
                backoff=1
            except TelegramError as exc:
                events.put(('error',exc))
                if exc.code in (401,409):
                    return
                stop.wait(max(backoff,int(exc.retry_after)))
                backoff=min(30,backoff*2)
            except Exception:
                events.put(('error',RuntimeError('Polling worker failed. Restart the bot.')))
                return

    def reset_session(self):
        """Close unfinished games and discard pending screens from the previous run."""
        with self.db:
            self.db.execute("""UPDATE games SET status='cancelled',finished_at=?
                WHERE status IN ('lobby','in_progress')""", (self.engine.clock(),))
            self.db.execute("""UPDATE game_participants SET question_deadline_at=NULL
                WHERE question_deadline_at IS NOT NULL AND game_id IN (
                    SELECT game_id FROM games WHERE status='cancelled')""")
            self.db.execute('DELETE FROM conversations')
            self.db.execute('DELETE FROM outbox')

    def run(self):
        self.reset_session()
        stop=threading.Event()
        events=queue.Queue()
        row=self.engine.one('SELECT value FROM settings WHERE key=?',(self.offset_key,))
        poller=threading.Thread(target=self._poll,args=(events,stop,int(row[0]) if row else 0),
                                name='telegram-poll',daemon=True)
        poller.start()
        try:
            while True:
                self.deliver()
                try:
                    event=events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if event[0]=='error':
                    exc=event[1]
                    if not isinstance(exc,TelegramError):
                        raise exc
                    if exc.code in (401,409):
                        raise RuntimeError('Telegram rejected the token (401), or another bot instance/webhook is running (409).') from None
                    log.warning('Telegram polling unavailable: code=%s. Retrying.',exc.code)
                    continue
                _,updates,received,handled=event
                for update in updates:
                    self.process(update,received)
                    self.deliver()  # Send a command reply without waiting for the next poll.
                handled.set()
        finally:
            stop.set()
            self.close()


def main():
    parser=argparse.ArgumentParser(description='Telegram quiz bot')
    bank_commands=parser.add_mutually_exclusive_group()
    bank_commands.add_argument('--init-db',action='store_true',help='Create database and load the default bank only if empty')
    bank_commands.add_argument('--import-questions',metavar='JSON',help='Update questions by key and exit without starting the bot')
    parser.add_argument('--network-check',action='store_true',help='Measure five read-only API requests without polling or sending messages')
    args=parser.parse_args()
    load_env()
    logging.basicConfig(level=getattr(logging,os.environ.get('LOG_LEVEL','INFO').upper(),logging.INFO),
                        format='%(asctime)s %(levelname)s %(message)s')
    path=Path(os.environ.get('DATABASE_PATH','data/quiz_bot.db'))
    if not path.is_absolute():
        path=ROOT/path
    token=os.environ.get('BOT_TOKEN','').strip()
    if not args.init_db and not args.import_questions and (not token or ':' not in token):
        print('BOT_TOKEN is missing. Copy .env.example to .env, set the token from @BotFather, then run py -3 -m quizbot.')
        return 1
    if args.network_check:
        client=TelegramClient(token)
        try:
            for number in range(1,6):
                started=time.perf_counter()
                client.call('getMe')
                print('getMe #%s: %.0f ms'%(number,(time.perf_counter()-started)*1000))
            print('First request includes connection setup; later requests reuse it when supported by the proxy.')
        finally:
            client.close()
        return 0
    admin_ids=parse_admin_ids(os.environ.get('ADMIN_IDS','')) if not (args.init_db or args.import_questions) else ()
    with InstanceLock(path):
        db=connect(path)
        try:
            if args.import_questions:
                count=seed_questions(db,args.import_questions)
                log.info('Question records imported: %s. Database: %s',count,path)
                return 0
            count=initialize_question_bank(db)
            log.info('Questions in SQLite: %s. Database: %s',count,path)
            if args.init_db:
                return 0
            client=TelegramClient(token)
            me=client.call('getMe')
            hook=client.call('getWebhookInfo')
            if hook.get('url'):
                raise RuntimeError('An existing webhook is configured. Remove it before starting long polling; see README.md.')
            commands=[('start','Открыть главное меню'),('create','Создать комнату'),('join','Войти по коду'),
                      ('room','Вернуться к текущей игре'),('history','Посмотреть историю игр'),
                      ('nick','Сменить ник'),('top','Посмотреть рейтинг за месяц'),('leave','Выйти из игры'),
                      ('help','Как играть')]
            client.call('setMyCommands',commands=[{'command':c,'description':d} for c,d in commands])
            log.info('Bot @%s started. Stop with Ctrl+C.',me['username'])
            Runner(db,client,me['username'],me['id'],admin_ids=admin_ids).run()
        finally:
            db.close()


if __name__=='__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nBot stopped. Unfinished games will be cancelled on the next start.')
    except (RuntimeError,TelegramError,ValueError,OSError) as exc:
        log.error('%s',exc)
        sys.exit(1)
