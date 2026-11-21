"""Game rules and persistent state. All mutations run inside caller transactions."""
import html
import json
import math
import random
import secrets
import sqlite3
import time
import unicodedata

DIFFICULTIES = {'easy': 'Лёгкая', 'medium': 'Средняя', 'hard': 'Сложная', 'mixed': 'Смешанная'}
POINTS = {'easy': 10, 'medium': 15, 'hard': 20}
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def new_game_code():
    # Avoid 0/O, 1/I/L for easier manual input.
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(5))


class RuleError(Exception):
    pass


def keyboard(*rows):
    return {'inline_keyboard': [[{'text': text, 'callback_data': data} for text, data in row] for row in rows]}


def mention(platform_id, nickname):
    return '<a href="tg://user?id=%s">%s</a>' % (int(platform_id), html.escape(nickname))


def format_count(number, singular, few, many):
    last_digit = number % 10
    if 11 <= number % 100 <= 14:
        unit = many
    elif last_digit == 1:
        unit = singular
    elif 2 <= last_digit <= 4:
        unit = few
    else:
        unit = many
    return f'{number} {unit}'


def points_for(base, elapsed_ms, limit_ms, correct):
    if not correct:
        return 0
    maximum = base+5
    if elapsed_ms<=10000 or limit_ms<=10000:
        return maximum
    fraction = min(1,max(0,(elapsed_ms-10000)/(limit_ms-10000)))
    return max(math.ceil(maximum/2),math.floor(maximum*(1-0.5*fraction)+0.5))


class Engine:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def user(self, platform_id):
        return self.one('SELECT * FROM users WHERE platform_user_id=?', (str(platform_id),))

    def state(self, platform_id):
        row = self.one('SELECT state_json FROM conversations WHERE platform_user_id=?', (str(platform_id),))
        return json.loads(row[0]) if row else {}

    def set_state(self, platform_id, state):
        self.db.execute('INSERT OR REPLACE INTO conversations VALUES(?,?)',
                        (str(platform_id), json.dumps(state, ensure_ascii=False)))

    def register(self, platform_id, nickname):
        nickname = unicodedata.normalize('NFC', nickname.strip())
        if not 3 <= len(nickname) <= 24 or not all(c.isalnum() or c in ' _-' for c in nickname):
            raise RuleError('Введите ник от 3 до 24 символов. Можно использовать буквы, цифры, пробел, дефис и подчёркивание.')
        normalized = nickname.casefold()
        existing = self.user(platform_id)
        taken = self.one('SELECT * FROM users WHERE nickname_normalized=?', (normalized,))
        if taken and (not existing or existing['user_id'] != taken['user_id']):
            raise RuleError('Этот ник уже занят. Выберите другой.')
        now = self.clock()
        if existing:
            self.db.execute('UPDATE users SET nickname=?,nickname_normalized=?,last_seen_at=? WHERE user_id=?',
                            (nickname, normalized, now, existing['user_id']))
        else:
            self.db.execute('INSERT INTO users(platform_user_id,nickname,nickname_normalized,created_at,last_seen_at) VALUES(?,?,?,?,?)',
                            (str(platform_id), nickname, normalized, now, now))
        return self.user(platform_id)

    def send(self, platform_id, text, markup=None, kind='message', game_id=None, user_id=None, position=None, slot='room'):
        if kind=='question':
            slot='room'
        if slot:
            self.db.execute('DELETE FROM outbox WHERE chat_id=? AND slot=?',(str(platform_id),slot))
        payload = {'text': text, 'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True}}
        if markup:
            payload['reply_markup'] = markup
        self.enqueue(platform_id,payload,kind,game_id,user_id,position,slot)

    def enqueue(self, platform_id, payload, kind='message', game_id=None, user_id=None, position=None, slot=None):
        if slot:
            payload={**payload,'_panel_epoch':self.panel_epoch(platform_id)}
        self.db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='outbox_sequence'")
        sequence = int(self.one("SELECT value FROM settings WHERE key='outbox_sequence'")[0])
        self.db.execute('''INSERT INTO outbox(outbox_id,chat_id,kind,payload_json,game_id,user_id,position,created_at,slot)
            VALUES(?,?,?,?,?,?,?,?,?)''', (sequence,str(platform_id),kind,json.dumps(payload,ensure_ascii=False),
                                         game_id,user_id,position,self.clock(),slot))

    def panel_epoch(self, platform_id):
        row=self.one('SELECT value FROM settings WHERE key=?',('panel_epoch:'+str(platform_id),))
        return int(row[0]) if row else 0

    def retarget_panel(self, platform_id, message_id=None):
        """Open navigation at the clicked menu, or send a fresh panel for a command."""
        chat_id=str(platform_id)
        previous=self.one("SELECT message_id FROM ui_messages WHERE chat_id=? AND slot='room'",(chat_id,))
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                        ('panel_epoch:'+chat_id,str(self.panel_epoch(chat_id)+1)))
        self.db.execute("DELETE FROM ui_messages WHERE chat_id=? AND slot='room'",(chat_id,))
        if message_id is not None:
            self.db.execute('INSERT INTO ui_messages VALUES(?,?,?)',(chat_id,'room',message_id))
        return previous[0] if previous and previous[0]!=message_id else None

    def remove_old_panel(self, platform_id, message_id):
        if message_id is not None:
            self.enqueue(platform_id,{'method':'deleteMessage','params':{'message_id':message_id}},kind='api')

    def confirm_delivery(self,row,message_id,delivered_at):
        # Even if navigation changed the target message, this was a real publication
        # of the same question. Reopening /room must not postpone the scoring clock.
        if row['kind']=='question':
            self.db.execute('''UPDATE game_participants SET question_delivered_at=?
                WHERE game_id=? AND user_id=? AND current_position=? AND participant_status='playing'
                AND question_delivered_at IS NULL''',
                (delivered_at,row['game_id'],row['user_id'],row['position']))
        if row['slot']:
            epoch=json.loads(row['payload_json']).get('_panel_epoch',0)
            if epoch!=self.panel_epoch(row['chat_id']):
                current=self.one('SELECT message_id FROM ui_messages WHERE chat_id=? AND slot=?',
                                 (row['chat_id'],row['slot']))
                if not current or current[0]!=message_id:
                    self.remove_old_panel(row['chat_id'],message_id)
                return
            self.db.execute('INSERT OR REPLACE INTO ui_messages VALUES(?,?,?)',
                            (row['chat_id'],row['slot'],message_id))

    def active(self, user_id):
        return self.one('''SELECT g.* FROM games g JOIN game_participants p USING(game_id)
            WHERE p.user_id=? AND g.status IN ('lobby','in_progress')
            ORDER BY g.game_id DESC LIMIT 1''', (user_id,))

    def available_questions(self, topic, difficulty):
        return self.all('''SELECT * FROM questions WHERE is_active=1
            AND (?='Все темы' OR topic=?) AND (?='mixed' OR difficulty=?)''',
                        (topic,topic,difficulty,difficulty))

    def create(self, user_id, topic, difficulty, count, seconds):
        if self.active(user_id):
            raise RuleError('У вас уже есть игра. Откройте её через /room или выйдите с помощью /leave.')
        if difficulty not in DIFFICULTIES or count not in (5,10,20,30) or seconds not in (15,30,60,120):
            raise RuleError('Эти настройки недоступны. Выберите значения из кнопок в меню комнаты.')
        if len(self.available_questions(topic,difficulty)) < count:
            raise RuleError('Для этой темы и сложности мало вопросов. Выберите меньше вопросов или другую сложность.')
        now = self.clock()
        for _ in range(100):
            code = new_game_code()
            try:
                cur = self.db.execute('''INSERT INTO games(game_code,created_by_user_id,topic,difficulty,
                    question_count,question_time_limit_seconds,created_at,lobby_expires_at,scoring_version)
                    VALUES(?,?,?,?,?,?,?,?,2)''',
                    (code,user_id,topic,difficulty,count,seconds,now,now+1800))
                break
            except sqlite3.IntegrityError as exc:
                if 'games.game_code' not in str(exc):
                    raise
        else:
            raise RuleError('Не удалось подобрать свободный код. Попробуйте ещё раз.')
        self.join(user_id, code)
        return cur.lastrowid

    def join(self, user_id, code):
        game = self.one('SELECT * FROM games WHERE game_code=?', (code.strip().upper(),))
        if not game:
            raise RuleError('Комната не найдена. Проверьте код и попробуйте войти снова.')
        if game['status'] in ('in_progress','completed'):
            raise RuleError('Игра в этой комнате уже началась. Попросите у создателя код следующей игры.')
        if game['status']!='lobby' or game['lobby_expires_at']<=self.clock():
            raise RuleError('Комната закрыта. Попросите у создателя новое приглашение.')
        if self.one('SELECT 1 FROM game_participants WHERE game_id=? AND user_id=?', (game['game_id'],user_id)):
            return game['game_id']
        if self.active(user_id):
            raise RuleError('Вы уже в другой игре. Сначала доиграйте или выйдите с помощью /leave.')
        size = self.one('SELECT COUNT(*) FROM game_participants WHERE game_id=?', (game['game_id'],))[0]
        if size >= 10:
            raise RuleError('В комнате заняты все 10 мест. Попробуйте войти позже или выберите другую комнату.')
        u = self.one('SELECT * FROM users WHERE user_id=?', (user_id,))
        self.db.execute('INSERT INTO game_participants(game_id,user_id,nickname_at_game,joined_at) VALUES(?,?,?,?)',
                        (game['game_id'],user_id,u['nickname'],self.clock()))
        return game['game_id']

    def participants(self, gid):
        return self.all('''SELECT p.*,u.platform_user_id,u.nickname FROM game_participants p
            JOIN users u USING(user_id) WHERE game_id=? ORDER BY p.rank,p.joined_at,p.user_id''', (gid,))

    def start(self, gid, owner_id):
        g = self.one('SELECT * FROM games WHERE game_id=?', (gid,))
        if not g or g['created_by_user_id'] != owner_id:
            raise RuleError('Запустить игру может только создатель комнаты.')
        if g['status'] != 'lobby' or g['lobby_expires_at'] <= self.clock():
            raise RuleError('Комната закрыта или игра уже началась. Проверьте её через /room.')
        players = self.participants(gid)
        if len(players) < 2:
            raise RuleError('Для игры нужно хотя бы два игрока. Поделитесь ссылкой или кодом комнаты.')
        bank = self.available_questions(g['topic'],g['difficulty'])
        if len(bank) < g['question_count']:
            raise RuleError('Для этих настроек уже не хватает вопросов. Создайте комнату с другой темой, '
                            'сложностью или меньшим числом вопросов.')
        for pos,q in enumerate(random.SystemRandom().sample(bank,g['question_count']),1):
            options = json.loads(q['options_json'])
            order = list(range(len(options)))
            random.SystemRandom().shuffle(order)
            self.db.execute('INSERT INTO game_questions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (gid,pos,q['question_id'],q['topic'],q['difficulty'],q['question_text'],
                 json.dumps([options[i] for i in order],ensure_ascii=False),
                 order.index(q['correct_answer']),q['explanation'],POINTS[q['difficulty']]))
        now = self.clock()
        self.db.execute("UPDATE games SET status='in_progress',started_at=?,hard_deadline_at=? WHERE game_id=?",
                        (now,now+g['question_count']*g['question_time_limit_seconds'],gid))
        for p in players:
            self.db.execute('''UPDATE game_participants SET nickname_at_game=?,participant_status='playing'
                WHERE game_id=? AND user_id=?''', (p['nickname'],gid,p['user_id']))
            self.open_question(gid,p['user_id'],1,now)

    def open_question(self, gid, uid, position, opened_at):
        g = self.one('SELECT * FROM games WHERE game_id=?', (gid,))
        u = self.one('SELECT platform_user_id FROM users WHERE user_id=?', (uid,))
        if position > g['question_count']:
            self.db.execute('''UPDATE game_participants SET participant_status='finished',
                question_deadline_at=NULL,finished_at=? WHERE game_id=? AND user_id=?''', (opened_at,gid,uid))
            self.send(u[0],'<b>Вы закончили отвечать</b>\nИтоги появятся здесь, когда закончат остальные игроки.',slot='room')
            return None
        deadline = min(opened_at+g['question_time_limit_seconds'],g['hard_deadline_at'])
        self.db.execute('''UPDATE game_participants SET current_position=?,question_opened_at=?,
            question_deadline_at=?,question_delivered_at=NULL WHERE game_id=? AND user_id=?''', (position,opened_at,deadline,gid,uid))
        self.send(u[0],'',kind='question',game_id=gid,user_id=uid,position=position)
        return deadline

    def answer(self, gid, uid, position, selected, received_at=None):
        now = self.clock() if received_at is None else received_at
        p = self.one('''SELECT p.*,g.status FROM game_participants p
            JOIN games g USING(game_id) WHERE p.game_id=? AND p.user_id=?''', (gid,uid))
        if not p or p['status']!='in_progress' or p['participant_status']!='playing' or p['current_position']!=position:
            raise RuleError('Этот вопрос уже закрыт. Откройте текущий вопрос через /room.')
        if now >= p['question_deadline_at']:
            self.tick()
            raise RuleError('Время на этот вопрос вышло. Ответ не засчитан.')
        q = self.one('SELECT * FROM game_questions WHERE game_id=? AND position=?', (gid,position))
        if not 0 <= selected < len(json.loads(q['options_snapshot'])):
            raise RuleError('Такого варианта ответа нет.')
        start = p['question_delivered_at']
        if start is None:
            raise RuleError('Бот ещё обновляет вопрос. Подождите немного и нажмите ответ ещё раз.')
        elapsed = max(0,round((now-start)*1000))
        correct = int(selected==q['correct_answer_snapshot'])
        window_ms = max(0,round((p['question_deadline_at']-start)*1000))
        score = points_for(q['base_points'],elapsed,window_ms,correct)
        self.db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?)',
                        (gid,uid,position,'answered',selected,correct,elapsed,score,now))
        self.db.execute('''UPDATE game_participants SET total_score=total_score+?,correct_count=correct_count+?,
            total_response_ms=total_response_ms+? WHERE game_id=? AND user_id=?''', (score,correct,elapsed,gid,uid))
        self.open_question(gid,uid,position+1,now)
        self.finalize(gid)

    def tick(self):
        now = self.clock()
        for g in self.all("SELECT * FROM games WHERE status='lobby' AND lobby_expires_at<=?", (now,)):
            self.cancel(g['game_id'],'Комната закрылась через 30 минут ожидания.')
        due = self.all('''SELECT game_id,user_id,current_position,question_deadline_at,question_time_limit_seconds
            FROM game_participants p JOIN games g USING(game_id)
            WHERE g.status='in_progress' AND p.participant_status='playing' AND p.question_deadline_at<=?''', (now,))
        affected = set()
        for gid,uid,position,deadline,seconds in due:
            while deadline is not None and deadline<=now:
                self.db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?)',
                                (gid,uid,position,'timed_out',None,0,None,0,deadline))
                self.db.execute('UPDATE game_participants SET total_response_ms=total_response_ms+? WHERE game_id=? AND user_id=?',
                                (seconds*1000,gid,uid))
                position += 1
                # A delayed check must not extend the time allowed for the game.
                deadline = self.open_question(gid,uid,position,deadline)
            affected.add(gid)
        for gid in affected:
            self.finalize(gid)

    def finalize(self, gid):
        g = self.one('SELECT * FROM games WHERE game_id=?', (gid,))
        if not g or g['status']!='in_progress':
            return
        players = self.participants(gid)
        if any(p['participant_status']=='playing' for p in players):
            return
        def key(p):
            return (p['participant_status']=='forfeited',-p['total_score'],-p['correct_count'],p['total_response_ms'])
        ordered = sorted(players,key=key)
        previous,rank = None,0
        for index,p in enumerate(ordered,1):
            if key(p)!=previous:
                rank=index
            self.db.execute('UPDATE game_participants SET rank=? WHERE game_id=? AND user_id=?', (rank,gid,p['user_id']))
            previous=key(p)
        self.db.execute("UPDATE games SET status='completed',finished_at=? WHERE game_id=?", (self.clock(),gid))
        result = self.results(gid)
        markup = keyboard([('История игр','history:0'),('Меню','menu')])
        for p in players:
            self.send(p['platform_user_id'],result,markup,slot='room')

    def owned_lobby(self, gid, owner_id):
        g=self.one('SELECT * FROM games WHERE game_id=?',(gid,))
        if not g or g['created_by_user_id']!=owner_id:
            raise RuleError('Удалить комнату может только её создатель.')
        if g['status']!='lobby':
            raise RuleError('Комната уже закрыта или игра началась.')
        return g

    def delete_lobby(self, gid, owner_id):
        self.owned_lobby(gid,owner_id)
        self.cancel(gid,'Создатель удалил комнату.')

    def cancel(self, gid, reason):
        self.db.execute("UPDATE games SET status='cancelled',finished_at=? WHERE game_id=? AND status='lobby'", (self.clock(),gid))
        for p in self.participants(gid):
            self.send(p['platform_user_id'],'<b>Игра отменена</b>\n'+html.escape(reason)+'\n\nОткройте /menu, чтобы выбрать другую игру.',slot='room')

    def leave(self, uid):
        g = self.active(uid)
        if not g:
            raise RuleError('У вас нет активной игры.')
        if g['status']=='lobby':
            if g['created_by_user_id']==uid:
                self.cancel(g['game_id'],'Создатель закрыл комнату.')
            else:
                self.db.execute('DELETE FROM game_participants WHERE game_id=? AND user_id=?', (g['game_id'],uid))
            return
        p = self.one('SELECT * FROM game_participants WHERE game_id=? AND user_id=?', (g['game_id'],uid))
        if p['participant_status']!='playing':
            raise RuleError('Для вас вопросы уже закончились. Итоги появятся, когда закончат остальные.')
        for pos in range(p['current_position'],g['question_count']+1):
            self.db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?)',
                            (g['game_id'],uid,pos,'forfeited',None,0,None,0,self.clock()))
        self.db.execute('''UPDATE game_participants SET participant_status='forfeited',
            question_deadline_at=NULL,finished_at=? WHERE game_id=? AND user_id=?''', (self.clock(),g['game_id'],uid))
        self.finalize(g['game_id'])

    def results(self, gid):
        g = self.one('SELECT * FROM games WHERE game_id=?', (gid,))
        lines = ['🏆 <b>Итоги игры</b> · <code>%s</code>' % g['game_code'],
                 '%s · %s вопросов' % (html.escape(g['topic']),g['question_count']),'',
                 '<b>Таблица лидеров</b>']
        for p in self.participants(gid):
            label = mention(p['platform_user_id'],p['nickname_at_game'])
            score = format_count(p['total_score'],'очко','очка','очков')
            line = f"{p['rank']}. {label} — {score} · верно {p['correct_count']} из {g['question_count']}"
            if p['participant_status']=='forfeited':
                line += ' · вышел'
            lines.append(line)
        return '\n'.join(lines)

    def leaderboard(self, start_at, end_at):
        return self.all('''
            WITH scores AS (
                SELECT p.user_id, SUM(4 - p.rank) AS points
                FROM games g JOIN game_participants p USING (game_id)
                WHERE g.status='completed' AND g.finished_at>=? AND g.finished_at<?
                  AND p.participant_status='finished' AND p.correct_count>0
                  AND p.rank BETWEEN 1 AND 3
                GROUP BY p.user_id
            )
            SELECT u.nickname, scores.points,
                   RANK() OVER (ORDER BY scores.points DESC) AS place
            FROM scores JOIN users u USING (user_id)
            ORDER BY scores.points DESC, u.nickname_normalized
            LIMIT 20
        ''', (start_at, end_at))

    def question_payload(self, row):
        p = self.one('''SELECT p.*,g.status,g.question_count,g.game_code FROM game_participants p
            JOIN games g USING(game_id) WHERE p.game_id=? AND p.user_id=?''', (row['game_id'],row['user_id']))
        if not p or p['status']!='in_progress' or p['participant_status']!='playing' or p['current_position']!=row['position']:
            return None
        remaining = math.ceil(p['question_deadline_at']-self.clock())
        if remaining<=0:
            return None
        q = self.one('SELECT * FROM game_questions WHERE game_id=? AND position=?', (p['game_id'],p['current_position']))
        rows = [[(text,'a:%s:%s:%s'%(p['game_id'],p['current_position'],idx))]
                for idx,text in enumerate(json.loads(q['options_snapshot']))]
        timeout_note = ''
        previous = self.one('SELECT answer_status FROM answers WHERE game_id=? AND user_id=? AND question_position=?',
                            (p['game_id'],p['user_id'],p['current_position']-1))
        if previous and previous[0]=='timed_out':
            timeout_note='⏰ Время на предыдущий вопрос вышло. За него 0 очков.'
        lines = [f"<b>Вопрос {p['current_position']} из {p['question_count']}</b> · <code>{p['game_code']}</code>",
                 '',html.escape(q['question_snapshot']),'',
                 f"{DIFFICULTIES[q['difficulty_snapshot']]} сложность · до {q['base_points']+5} очков",
                 f'Осталось {remaining} с. За верный ответ в первые 10 секунд — максимум очков.']
        if timeout_note:
            lines.extend(['',timeout_note])
        return {'text':'\n'.join(lines),'parse_mode':'HTML','reply_markup':keyboard(*rows)}
