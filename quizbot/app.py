import html
import json
import secrets
from datetime import datetime, timedelta, timezone

from .engine import DIFFICULTIES, MODES, RuleError, keyboard, mention

LOCAL_TZ = timezone(timedelta(hours=5))


def menu():
    return keyboard([('Создать комнату','create'),('Дуэли','duel')],
                    [('Войти по коду','join'),('Моя игра','room')],
                    [('Профиль','profile'),('История','history:0')],
                    [('Рейтинг игроков','top'),('Правила','help')])


class Application:
    def __init__(self, engine, bot_username):
        self.e = engine
        self.bot_username = bot_username

    def handle(self, update, received_at=None):
        callback = update.get('callback_query')
        msg = (callback or {}).get('message') or update.get('message')
        sender = (callback or {}).get('from') or (msg or {}).get('from')
        if not msg or not sender or sender.get('is_bot'):
            return ''
        pid = str(sender['id'])
        if msg['chat']['type']!='private':
            if (msg.get('text') or '').startswith(('/start','/help')):
                self.e.send(msg['chat']['id'],'Игры проходят в личном чате: https://t.me/'+self.bot_username)
            return 'Откройте личный чат с ботом.'
        text = (msg.get('text') or '').strip() if not callback else ''
        data = callback.get('data','') if callback else ''
        try:
            return self.route(pid,text,data,received_at,msg.get('message_id') if callback else None)
        except RuleError as exc:
            if callback:
                return str(exc)
            self.show_error(pid,html.escape(str(exc)),text)
            return ''
        except (ValueError,IndexError,KeyError):
            # Stale or fabricated callback data must not poison the update queue.
            message='Эта кнопка недействительна. Откройте /menu заново.'
            if callback:
                return message
            self.show_error(pid,message,text)
            return ''

    def pending_response(self,pid):
        return self.e.one("SELECT * FROM outbox WHERE chat_id=? AND slot='room' ORDER BY outbox_id DESC LIMIT 1",
                          (str(pid),))

    def present_response(self,pid,before,text,data,message_id):
        response=self.pending_response(pid)
        if response is None or (before is not None and before['outbox_id']==response['outbox_id']):
            return
        # Answers advance the existing game screen; every other button uses
        # the message actually clicked. Text input moves the screen to the bottom.
        if data.startswith('a:') or (not text and message_id is None):
            return
        current=self.e.one("SELECT message_id FROM ui_messages WHERE chat_id=? AND slot='room'",(str(pid),))
        if message_id is not None and current and current[0]==message_id:
            return
        previous=self.e.retarget_panel(pid,message_id)
        payload=json.loads(response['payload_json'])
        payload['_panel_epoch']=self.e.panel_epoch(pid)
        self.e.db.execute('UPDATE outbox SET payload_json=? WHERE outbox_id=?',
                          (json.dumps(payload,ensure_ascii=False),response['outbox_id']))
        self.e.remove_old_panel(pid,previous)

    def show_error(self,pid,text,input_text):
        before=self.pending_response(pid)
        self.e.send(pid,text,menu() if self.e.user(pid) else None)
        self.present_response(pid,before,input_text,'',None)

    def route(self, pid, text, data, received_at, message_id=None):
        before=self.pending_response(pid)
        try:
            return self._route(pid,text,data,received_at,message_id)
        finally:
            self.present_response(pid,before,text,data,message_id)

    def _route(self, pid, text, data, received_at, message_id=None):
        state = self.e.state(pid)
        parts = text.split(maxsplit=1)
        cmd = parts[0].split('@')[0].lower() if parts and parts[0].startswith('/') else ''
        arg = parts[1].strip() if len(parts)>1 else ''
        u = self.e.user(pid)
        if cmd=='/start' and arg.startswith('join_'):
            code = arg[5:].upper()
            if u:
                self.join(u,code)
                return ''
            state = {'step':'register','invite':code}
            self.e.set_state(pid,state)
        if not u:
            if text and not cmd:
                u = self.e.register(pid,text)
                invite = state.get('invite')
                self.e.set_state(pid,{})
                self.e.send(pid,'Регистрация завершена. Ваш ник: <b>%s</b>.' % html.escape(u['nickname']),menu())
                if invite:
                    self.join(u,invite)
            else:
                self.e.send(pid,'Добро пожаловать! Введите ник для игры (3–24 символа). Больше ничего не нужно.')
            return 'Сначала введите ник.' if data else ''
        uid = u['user_id']
        self.e.db.execute('UPDATE users SET last_seen_at=? WHERE user_id=?',(self.e.clock(),uid))
        if cmd in ('/start','/menu','/cancel') or data=='menu':
            self.e.set_state(pid,{})
            self.e.send(pid,'Выберите действие. /cancel сбрасывает ввод; для выхода из игры — /leave.',menu())
        elif cmd=='/help' or data=='help':
            self.e.send(pid,'<b>Квиз-бот</b>\n\n'
                'Комната: 2–10 игроков, общая тема и настройки, запуск создателем. '
                'У всех одинаковый случайный порядок вопросов; каждый отвечает в своём темпе.\n\n'
                'Дуэль: поиск соперника или приглашение друга по ссылке. '
                '10 вопросов по разным темам, по 60 секунд. Рейтинг Elo: старт 1000, K=32. '
                'Приглашённые дуэли не попадают в общий поиск и начинаются, когда друг присоединится.\n\n'
                'Максимум за вопрос: лёгкий — 15, средний — 20, сложный — 25. '
                'Первые 10 секунд после доставки — полный максимум. Затем очки плавно уменьшаются '
                'до половины максимума к сроку ответа; округление до целого, штраф не больше 50%. '
                'Неверный ответ и тайм-аут — 0. Время вышло — следующий вопрос автоматически. '
                'Сначала сравниваются очки, затем верные ответы, затем меньшее суммарное время. '
                'Тайм-аут добавляет полный лимит времени. При полном равенстве место общее. '
                'Вышедшие досрочно располагаются ниже завершивших.\n\n'
                '/create — комната\n/join КОД — присоединиться\n/duel — выбор дуэли\n/challenge — вызов другу\n/room — текущая игра\n'
                '/profile — профиль\n/nick — сменить ник\n/history — история\n/game КОД — итог игры\n'
                '/top — общий рейтинг дуэлей\n/leave — выход с подтверждением\n/cancel — отмена ввода',menu())
        elif cmd=='/profile' or data=='profile':
            self.profile(u)
        elif cmd=='/nick' or data=='nick':
            self.e.set_state(pid,{'step':'nick'})
            self.e.send(pid,'Введите новый ник. /cancel — отмена. В истории сохранится ник на момент игры.')
        elif cmd=='/create' or data=='create':
            if self.e.active(uid):
                raise RuleError('Вы уже в игре. Откройте /room или выйдите через /leave.')
            topics = [r[0] for r in self.e.all('SELECT DISTINCT topic FROM questions WHERE is_active=1 ORDER BY topic')]
            topics.insert(0,'Все темы')
            state = {'step':'wizard','token':secrets.token_hex(3),'topics':topics,
                     'topic':0,'difficulty':'mixed','count':10,'seconds':60}
            self.e.set_state(pid,state)
            self.wizard(pid,state)
        elif data.startswith('w:'):
            self.wizard_action(u,data,state)
        elif cmd=='/join' or data=='join':
            if arg:
                self.join(u,arg)
            else:
                self.e.set_state(pid,{'step':'join'})
                self.e.send(pid,'Введите код из 5 латинских букв или цифр, например S23J8. /cancel — отмена.')
        elif cmd=='/duel' or data=='duel':
            if self.e.active(uid):
                raise RuleError('Вы уже участвуете в игре. Откройте /room.')
            self.e.set_state(pid,{})
            self.e.send(pid,'<b>Дуэли</b>\nНайти соперника или отправить вызов другу?\n'
                        'Оба варианта: 10 вопросов, 60 секунд на вопрос, рейтинг Elo.',
                        keyboard([('Найти соперника','duel_search')],[('Вызвать друга','duel_friend')],[('Назад','menu')]),slot='room')
        elif data in ('duel_search','duel_friend') or cmd=='/challenge':
            self.begin_duel(u,data=='duel_friend' or cmd=='/challenge',message_id)
        elif cmd=='/room' or data=='room' or data.startswith('lobby:'):
            g = self.e.active(uid)
            if not g:
                raise RuleError('У вас нет активной игры. Создайте комнату или выберите дуэль. Результаты прошлых игр: /history.')
            self.e.set_state(pid,{})
            self.lobby(u,g['game_id'])
        elif data.startswith('start:'):
            self.e.start(int(data.split(':')[1]),uid)
        elif data.startswith('delete:'):
            self.confirm_lobby_deletion(u,int(data.split(':')[1]),message_id)
        elif data.startswith('delete_yes:'):
            self.delete_lobby(u,int(data.split(':')[1]),message_id)
        elif cmd=='/leave' or data=='leave':
            g = self.e.active(uid)
            if not g:
                raise RuleError('У вас нет активной игры.')
            if g['status']=='lobby' and g['created_by_user_id']==uid:
                self.confirm_lobby_deletion(u,g['game_id'],message_id)
                return ''
            self.confirm_leave(u,g,message_id)
        elif data.startswith('leave_yes:'):
            confirmation=data.split(':')
            g = self.e.active(uid)
            if not g or g['game_id']!=int(confirmation[1]):
                raise RuleError('Подтверждение относится к другой или завершённой игре.')
            if g['status']=='lobby' and g['created_by_user_id']==uid:
                self.delete_lobby(u,g['game_id'],message_id)
                return ''
            if (len(confirmation)!=3 or state.get('step')!='confirm_leave'
                    or state.get('game_id')!=g['game_id'] or state.get('token')!=confirmation[2]):
                raise RuleError('Подтверждение устарело. Нажмите «Выйти» ещё раз.')
            if state['phase']=='lobby' and g['status']!='lobby':
                # The owner may have started while the guest was deciding.
                # Explain the consequences before accepting a surrender.
                self.confirm_leave(u,g,message_id)
                return ''
            self.e.leave(uid)
            self.e.set_state(pid,{})
            self.e.send(pid,('Вы покинули комнату.' if g['status']=='lobby' else 'Вы вышли из игры.')+
                        '\n\nГлавное меню:',menu(),slot='room')
            if g['status']=='lobby':
                for participant in self.e.participants(g['game_id']):
                    self.lobby(self.e.user(participant['platform_user_id']),g['game_id'])
        elif data.startswith('a:'):
            _,gid,pos,selected = data.split(':')
            return self.e.answer(int(gid),uid,int(pos),int(selected),received_at)
        elif cmd=='/history' or data.startswith('history:'):
            self.history(u,int(data.split(':')[1]) if data else 0)
        elif cmd=='/game':
            g = self.e.one('SELECT * FROM games WHERE game_code=?',(arg.upper(),))
            self.result_for(u,g)
        elif data.startswith('result:'):
            self.result_for(u,self.e.one('SELECT * FROM games WHERE game_id=?',(int(data.split(':')[1]),)))
        elif data.startswith('review:'):
            _,gid,pos = data.split(':')
            self.review(u,int(gid),int(pos))
        elif cmd=='/top' or data=='top':
            rows = self.e.all('''SELECT u.* FROM users u WHERE EXISTS(
                SELECT 1 FROM game_participants p JOIN games g USING(game_id)
                WHERE p.user_id=u.user_id AND g.mode='duel' AND g.status='completed')
                ORDER BY rating DESC,nickname_normalized LIMIT 20''')
            lines = ['<b>Рейтинг дуэлей · топ 20</b>']
            lines.extend('%s. %s — %s'%(i,mention(r['platform_user_id'],r['nickname']),r['rating']) for i,r in enumerate(rows,1))
            if not rows:
                lines.append('Пока нет завершённых дуэлей.')
            self.e.send(pid,'\n'.join(lines),menu())
        elif text and not cmd and state.get('step')=='nick':
            changed = self.e.register(pid,text)
            self.e.set_state(pid,{})
            self.e.send(pid,'Теперь ваш ник: <b>%s</b>.'%html.escape(changed['nickname']),menu())
        elif text and not cmd and state.get('step')=='join':
            self.join(u,text)
        else:
            self.e.send(pid,'Выберите действие кнопкой. Для ответа используйте варианты последнего вопроса.',menu())
        return ''

    def confirm_leave(self,u,g,message_id):
        participant=self.e.one('SELECT participant_status FROM game_participants WHERE game_id=? AND user_id=?',
                               (g['game_id'],u['user_id']))
        if not participant or participant[0] not in ('joined','playing'):
            raise RuleError('Ваш тест уже завершён. Дождитесь итогов общей игры.')
        token=secrets.token_hex(4)
        self.e.set_state(u['platform_user_id'],{'step':'confirm_leave','game_id':g['game_id'],
                                              'phase':g['status'],'token':token})
        if g['status']=='lobby':
            text='Точно хотите покинуть комнату?'
        else:
            text='Точно хотите выйти из игры?\nОставшиеся вопросы получат 0 очков.'
            if g['mode']=='duel':
                text+=' В дуэли засчитается сдача.'
        self.e.send(u['platform_user_id'],text,
                    keyboard([('Да, выйти','leave_yes:%s:%s'%(g['game_id'],token)),('Отмена','room')]),slot='room')

    def confirm_lobby_deletion(self,u,gid,message_id):
        g=self.e.owned_lobby(gid,u['user_id'])
        self.e.send(u['platform_user_id'],'Удалить лобби <code>%s</code>?\n'
                    'Оно закроется для всех участников.'%g['game_code'],
                    keyboard([('Да, удалить','delete_yes:%s'%gid),('Отмена','room')]),slot='room')

    def delete_lobby(self,u,gid,message_id):
        self.e.delete_lobby(gid,u['user_id'])
        self.e.set_state(u['platform_user_id'],{})
        self.e.send(u['platform_user_id'],'Лобби удалено.\n\nГлавное меню:',menu(),slot='room')

    def begin_duel(self,u,invite,message_id):
        if self.e.active(u['user_id']):
            raise RuleError('Вы уже участвуете в игре. Откройте /room или выйдите через /leave.')
        if invite:
            gid=self.e.challenge(u['user_id'])
            started=False
        else:
            gid,started=self.e.find_duel(u['user_id'])
        self.e.set_state(u['platform_user_id'],{})
        if not started:
            self.lobby(u,gid)

    def join(self, u, code):
        gid = self.e.join(u['user_id'],code)
        self.e.set_state(u['platform_user_id'],{})
        g = self.e.one('SELECT * FROM games WHERE game_id=?',(gid,))
        if g['mode']=='duel' and len(self.e.participants(gid))==2:
            self.e.start(gid,g['created_by_user_id'])
        else:
            for p in self.e.participants(gid):
                self.lobby(self.e.user(p['platform_user_id']),gid)

    def wizard(self, pid, state):
        token = state['token']
        def button(label,key,value):
            return label,'w:%s:%s:%s'%(token,key,value)
        topic = state['topics'][state['topic']]
        rows = [[button(t,'topic',i)] for i,t in enumerate(state['topics'])]
        rows.append([button(v,'difficulty',k) for k,v in DIFFICULTIES.items()])
        count = len(self.e.available_questions(topic,state['difficulty']))
        rows.append([button(str(n)+' вопр.','count',n) for n in (5,10,20,30) if n<=count])
        rows.append([button(str(s)+' сек.','seconds',s) for s in (15,30,60,120)])
        rows.append([button('Создать','save','yes'),('Отмена','menu')])
        self.e.send(pid,'<b>Настройки комнаты</b>\nТема: %s\nСложность: %s\nВопросов: %s\nНа вопрос: %s сек.\n\n'
                    'Нажимайте параметры ниже, затем «Создать».'%
                    (html.escape(topic),DIFFICULTIES[state['difficulty']],state['count'],state['seconds']),keyboard(*rows),slot='room')

    def wizard_action(self,u,data,state):
        _,token,key,value = data.split(':')
        if state.get('step')!='wizard' or token!=state.get('token'):
            raise RuleError('Эти настройки устарели. Откройте /create заново.')
        if key=='save':
            gid = self.e.create(u['user_id'],'room',state['topics'][state['topic']],state['difficulty'],state['count'],state['seconds'])
            self.e.set_state(u['platform_user_id'],{})
            self.lobby(u,gid)
            return
        allowed = {'topic':range(len(state['topics'])),'difficulty':DIFFICULTIES,'count':(5,10,20,30),'seconds':(15,30,60,120)}
        if key not in allowed:
            raise RuleError('Неизвестный параметр.')
        value = value if key=='difficulty' else int(value)
        if value not in allowed[key]:
            raise RuleError('Недопустимое значение.')
        state[key] = value
        available = len(self.e.available_questions(state['topics'][state['topic']],state['difficulty']))
        if state['count']>available:
            choices = [n for n in (5,10,20,30) if n<=available]
            if not choices:
                raise RuleError('В этой категории меньше 5 вопросов. Выберите другую.')
            state['count']=max(choices)
        self.e.set_state(u['platform_user_id'],state)
        self.wizard(u['platform_user_id'],state)

    def lobby(self,u,gid):
        g = self.e.one('SELECT * FROM games WHERE game_id=?',(gid,))
        players = self.e.participants(gid)
        if not any(p['user_id']==u['user_id'] for p in players):
            raise RuleError('Вы не участник этой игры.')
        if g['status']=='completed':
            return self.result_for(u,g)
        p = next(p for p in players if p['user_id']==u['user_id'])
        if g['status']=='in_progress' and p['participant_status']=='playing':
            self.e.send(u['platform_user_id'],'',kind='question',game_id=gid,user_id=u['user_id'],position=p['current_position'])
            return
        lines = ['<b>%s</b> · <code>%s</code>'%(MODES[g['mode']],g['game_code']),
                 '%s · %s · %s вопросов · %s сек.'%(html.escape(g['topic']),DIFFICULTIES[g['difficulty']],
                      g['question_count'],g['question_time_limit_seconds']),'','Участники:']
        for participant in players:
            status = {'joined':'в лобби','playing':'отвечает','finished':'завершил','forfeited':'вышел'}[participant['participant_status']]
            lines.append('%s — %s'%(mention(participant['platform_user_id'],participant['nickname']),status))
        rows = [[('Обновить','room'),('Выйти','leave')]]
        if g['status']=='in_progress':
            lines.extend(['','Ваш тест завершён. Ожидаем остальных участников.' if p['participant_status']=='finished'
                          else 'Вы вышли из игры. Ожидаем завершения остальных участников.',
                          'Итог придёт автоматически.'])
            rows = [[('Обновить','room'),('Меню','menu')]]
        if g['status']=='lobby':
            if g['created_by_user_id']==u['user_id']:
                rows = [[('Обновить','room'),('Удалить','delete:%s'%gid)]]
            if g['mode']=='duel' and g['matchmaking']=='public':
                lines.extend(['','Ищем соперника с ближайшим рейтингом. Дуэль начнётся автоматически.',
                              'Поиск действует 30 минут.'])
            else:
                if g['mode']=='duel':
                    lines.extend(['','Отправьте ссылку другу. Когда он присоединится, дуэль начнётся автоматически.',
                                  'Случайные соперники сюда не подбираются. Приглашение действует 30 минут.'])
                lines.extend(['','Приглашение: https://t.me/%s?start=join_%s'%(self.bot_username,g['game_code']),
                              'Можно также отправить боту /join '+g['game_code']])
            if g['mode']=='room' and u['user_id']==g['created_by_user_id']:
                rows.insert(0,[('Начать игру','start:%s'%gid)])
        self.e.send(u['platform_user_id'],'\n'.join(lines),keyboard(*rows),slot='room')

    def profile(self,u):
        s = self.e.one('SELECT * FROM user_overall_stats WHERE user_id=?',(u['user_id'],))
        lines = ['<b>Профиль</b> · '+mention(u['platform_user_id'],u['nickname']),'Рейтинг дуэлей: %s'%u['rating']]
        if s:
            lines.extend(['','За всё время: %s игр · %s побед · %s общих первых мест'%
                          (s['games_played'],s['wins'],s['shared_first_places']),
                          'Сдач: %s · очков: %s · в среднем: %.1f'%(s['forfeits'],s['total_points'],s['average_points_per_game']),
                          'Верных ответов: %s/%s (%.1f%%)'%(s['correct_answers'],s['total_questions'],s['accuracy_percent'])])
        else:
            lines.append('Завершённых игр пока нет.')
        for m in self.e.all('SELECT * FROM user_mode_stats WHERE user_id=? ORDER BY mode',(u['user_id'],)):
            avg_time = '—' if m['average_response_ms'] is None else '%.1f сек.'%(m['average_response_ms']/1000)
            lines.extend(['','<b>%s</b>'%MODES[m['mode']],
                'Игр: %s · побед: %s · общих первых мест: %s'%(m['games_played'],m['wins'],m['shared_first_places']),
                'Средние очки: %.1f · лучший счёт: %s'%(m['average_points_per_game'],m['best_score']),
                'Точность: %.1f%% · среднее место: %.1f'%(m['accuracy_percent'],m['average_rank']),
                'Среднее время ответа: '+avg_time])
        lines.append('\nВремя усредняется по отправленным ответам; тайм-ауты входят в точность как ошибки.')
        self.e.send(u['platform_user_id'],'\n'.join(lines),keyboard([('Сменить ник','nick'),('История','history:0')],[('Меню','menu')]))

    def history(self,u,page):
        page=max(0,page)
        records = self.e.all('''SELECT g.*,p.total_score,p.rank FROM games g JOIN game_participants p USING(game_id)
            WHERE p.user_id=? ORDER BY g.game_id DESC LIMIT 6 OFFSET ?''',(u['user_id'],page*5))
        lines = ['<b>История игр · страница %s</b>'%(page+1)]
        rows=[]
        for g in records[:5]:
            date = datetime.fromtimestamp(g['created_at'],LOCAL_TZ).strftime('%d.%m.%Y %H:%M')
            label = {'lobby':'лобби','in_progress':'идёт','completed':'завершена','cancelled':'отменена'}[g['status']]
            lines.append('\n<code>%s</code> · %s (UTC+5)\n%s · %s · %s\nОчки: %s · место: %s'%
                         (g['game_code'],date,MODES[g['mode']],html.escape(g['topic']),label,g['total_score'],g['rank'] or '—'))
            if g['status']=='completed':
                rows.append([('Итоги '+g['game_code'],'result:%s'%g['game_id'])])
        if not records:
            lines.append('Игр пока нет.')
        navigation=[]
        if page:
            navigation.append(('← Назад','history:%s'%(page-1)))
        if len(records)>5:
            navigation.append(('Далее →','history:%s'%(page+1)))
        if navigation:
            rows.append(navigation)
        rows.append([('Профиль','profile'),('Меню','menu')])
        self.e.send(u['platform_user_id'],'\n'.join(lines),keyboard(*rows))

    def result_for(self,u,g):
        if not g or g['status']!='completed' or not self.e.one(
                'SELECT 1 FROM game_participants WHERE game_id=? AND user_id=?',(g['game_id'],u['user_id'])):
            raise RuleError('Завершённая игра не найдена в вашей истории.')
        self.e.send(u['platform_user_id'],self.e.results(g['game_id']),
                    keyboard([('Разобрать ответы','review:%s:1'%g['game_id']),('История','history:0')]))

    def review(self,u,gid,pos):
        g = self.e.one('''SELECT g.* FROM games g JOIN game_participants p USING(game_id)
            WHERE g.game_id=? AND p.user_id=? AND g.status='completed' ''',(gid,u['user_id']))
        if not g or not 1<=pos<=g['question_count']:
            raise RuleError('Разбор доступен участникам только после завершения всей игры.')
        q = self.e.one('SELECT * FROM game_questions WHERE game_id=? AND position=?',(gid,pos))
        a = self.e.one('SELECT * FROM answers WHERE game_id=? AND user_id=? AND question_position=?',(gid,u['user_id'],pos))
        options = json.loads(q['options_snapshot'])
        chosen = options[a['submitted_answer']] if a['submitted_answer'] is not None else (
            'Пропуск по таймеру' if a['answer_status']=='timed_out' else 'Выход из игры')
        lines = ['<code>%s</code> · вопрос %s/%s'%(g['game_code'],pos,g['question_count']),
                 html.escape(q['question_snapshot']),'','Ваш ответ: '+html.escape(chosen),
                 'Верный ответ: '+html.escape(options[q['correct_answer_snapshot']]),
                 'Начислено: %s очк.'%a['points_awarded']]
        if q['explanation_snapshot']:
            lines.append(html.escape(q['explanation_snapshot']))
        rows=[]
        nav=[]
        if pos>1:
            nav.append(('← Назад','review:%s:%s'%(gid,pos-1)))
        if pos<g['question_count']:
            nav.append(('Далее →','review:%s:%s'%(gid,pos+1)))
        if nav:
            rows.append(nav)
        rows.append([('Итоги','result:%s'%gid)])
        self.e.send(u['platform_user_id'],'\n'.join(lines),keyboard(*rows))
