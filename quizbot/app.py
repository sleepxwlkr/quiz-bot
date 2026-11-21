import html
import json
import secrets
from datetime import datetime, timedelta, timezone

from .admin import QuestionAdmin
from .engine import DIFFICULTIES, QUESTION_COUNTS, QUESTION_TIMES, RuleError, format_count, keyboard, mention

LOCAL_TZ = timezone(timedelta(hours=5))


def menu():
    return keyboard([('Создать комнату','create'),('Войти по коду','join')],
                    [('Моя игра','room'),('История игр','history:0')],
                    [('Общий рейтинг','top'),('Правила','help')])


class Application:
    def __init__(self, engine, bot_username, admin_ids=()):
        self.e = engine
        self.bot_username = bot_username
        self.admin = QuestionAdmin(engine, admin_ids, menu)

    def handle(self, update, received_at=None):
        callback = update.get('callback_query')
        msg = (callback or {}).get('message') or update.get('message')
        sender = (callback or {}).get('from') or (msg or {}).get('from')
        if not msg or not sender or sender.get('is_bot'):
            return ''
        pid = str(sender['id'])
        if msg['chat']['type']!='private':
            if (msg.get('text') or '').startswith(('/start','/help')):
                self.e.send(msg['chat']['id'],'Откройте личный чат с ботом, чтобы создать комнату или войти в игру.\n\n'
                            'https://t.me/'+self.bot_username)
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
            message='Эта кнопка устарела. Отправьте /menu, чтобы открыть главное меню.'
            if callback:
                return message
            self.show_error(pid,message,text)
            return ''

    def pending_response(self,pid):
        return self.e.one("SELECT outbox_id,payload_json FROM outbox WHERE chat_id=? AND slot='room' ORDER BY outbox_id DESC LIMIT 1",
                          (str(pid),))

    def present_response(self,pid,before,message_id):
        response=self.pending_response(pid)
        if response is None or (before is not None and before['outbox_id']==response['outbox_id']):
            return
        if message_id is not None:
            current=self.e.one("SELECT message_id FROM ui_messages WHERE chat_id=? AND slot='room'",(str(pid),))
            if current and current[0]==message_id:
                return
        previous=self.e.retarget_panel(pid,message_id)
        payload=json.loads(response['payload_json'])
        payload['_panel_epoch']=self.e.panel_epoch(pid)
        self.e.db.execute('UPDATE outbox SET payload_json=? WHERE outbox_id=?',
                          (json.dumps(payload,ensure_ascii=False),response['outbox_id']))
        self.e.remove_old_panel(pid,previous)

    def show_error(self,pid,text,input_text):
        before=self.pending_response(pid) if input_text else None
        self.e.send(pid,text,menu() if self.e.user(pid) else None)
        if input_text:
            self.present_response(pid,before,None)

    def route(self, pid, text, data, received_at, message_id=None):
        # Answers advance the existing game screen. Other buttons use the clicked screen.
        if data.startswith('a:') or (not text and message_id is None):
            return self._route(pid,text,data,received_at)
        before=self.pending_response(pid)
        try:
            return self._route(pid,text,data,received_at)
        finally:
            self.present_response(pid,before,message_id)

    def _route(self, pid, text, data, received_at):
        state = self.e.state(pid)
        parts = text.split(maxsplit=1)
        cmd = parts[0].split('@')[0].lower() if parts and parts[0].startswith('/') else ''
        arg = parts[1].strip() if len(parts)>1 else ''
        if cmd=='/admin' or data.startswith('adm:'):
            return self.admin.handle(pid,cmd,data,text,state)
        if state.get('step')=='admin':
            player_commands = ('/start','/menu','/help','/nick','/create',
                               '/join','/room','/leave','/history','/top')
            if not data and cmd not in player_commands:
                return self.admin.handle(pid,cmd,data,text,state)
            self.e.set_state(pid,{})
            state = {}
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
                self.e.send(pid,'<b>Вы готовы к игре</b>\nВаш ник — %s.\n\n'
                            'Создайте комнату или войдите по коду.' % html.escape(u['nickname']),menu())
                if invite:
                    self.join(u,invite)
            else:
                prompt = ('После этого вы войдёте в комнату по приглашению.' if state.get('invite')
                          else 'Этот ник увидят другие игроки.')
                self.e.send(pid,'<b>Добро пожаловать в квиз!</b>\nВведите ник от 3 до 24 символов.\n\n'+prompt)
            return 'Сначала введите ник.' if data else ''
        uid = u['user_id']
        self.e.db.execute('UPDATE users SET last_seen_at=? WHERE user_id=?',(self.e.clock(),uid))
        if cmd in ('/start','/menu','/cancel') or data=='menu':
            self.e.set_state(pid,{})
            self.e.send(pid,'<b>Главное меню</b>\nВыберите действие.',menu())
        elif cmd=='/help' or data=='help':
            self.e.send(pid,'<b>Как играть</b>\n'
                'Создайте комнату на 2–10 игроков или войдите по ссылке или коду. '
                'Игру запускает создатель. Вопросы у всех одинаковые, каждый отвечает в своём темпе.\n\n'
                '<b>Очки за ответы</b>\n'
                'Лёгкий вопрос — до 15 очков, средний — до 20, сложный — до 25. '
                'За верный ответ в первые 10 секунд начисляется максимум. После этого очки постепенно '
                'снижаются примерно до половины. Ошибка или пропуск дают 0. '
                'Когда время выходит, бот показывает следующий вопрос.\n\n'
                '<b>Места в игре</b>\n'
                'Выше тот, кто набрал больше очков. При равенстве важнее число верных ответов, '
                'а затем меньшее суммарное время. Пропуск добавляет полный лимит времени. Если всё совпало, место общее. '
                'Игроки, которые вышли досрочно, идут после тех, кто доиграл. Свои места и очки можно посмотреть в истории.\n\n'
                '<b>Общий рейтинг за месяц</b>\n'
                'За первые три места дают 3, 2 и 1 балл. Чтобы получить баллы, завершите игру '
                'и дайте хотя бы один верный ответ.\n\n'
                'Команда /room вернёт к текущей игре, а /leave откроет подтверждение выхода.',
                keyboard([('Меню','menu')]))
        elif cmd=='/nick':
            self.e.set_state(pid,{'step':'nick'})
            self.e.send(pid,'<b>Смена ника</b>\nВведите новый ник от 3 до 24 символов. '
                        'В итогах прошлых игр останется прежний ник.\n\nДля отмены отправьте /cancel.')
        elif cmd=='/create' or data=='create':
            if self.e.active(uid):
                raise RuleError('У вас уже есть игра. Чтобы открыть её, нажмите «Моя игра» или отправьте /room.')
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
                self.e.send(pid,'<b>Вход в комнату</b>\nВведите код, который прислал создатель. '
                            'Обычно это 5 латинских букв и цифр, например <code>S23J8</code>.',
                            keyboard([('Меню','menu')]))
        elif cmd=='/room' or data=='room' or data.startswith('lobby:'):
            g = self.e.active(uid)
            if not g:
                raise RuleError('У вас пока нет активной игры. Создайте комнату или войдите по коду. '
                                'Итоги прошлых игр можно посмотреть в /history.')
            self.e.set_state(pid,{})
            self.lobby(g['game_id'],uid)
        elif data.startswith('start:'):
            self.e.start(int(data.split(':')[1]),uid)
        elif data.startswith('delete:'):
            self.confirm_lobby_deletion(u,int(data.split(':')[1]))
        elif data.startswith('delete_yes:'):
            self.delete_lobby(u,int(data.split(':')[1]))
        elif cmd=='/leave' or data=='leave':
            g = self.e.active(uid)
            if not g:
                raise RuleError('У вас нет активной игры.')
            if g['status']=='lobby' and g['created_by_user_id']==uid:
                self.confirm_lobby_deletion(u,g['game_id'])
                return ''
            self.confirm_leave(u,g)
        elif data.startswith('leave_yes:'):
            confirmation=data.split(':')
            g = self.e.active(uid)
            if not g or g['game_id']!=int(confirmation[1]):
                raise RuleError('Это подтверждение больше не действует. Откройте «Моя игра», чтобы увидеть текущую комнату.')
            if g['status']=='lobby' and g['created_by_user_id']==uid:
                self.delete_lobby(u,g['game_id'])
                return ''
            if (len(confirmation)!=3 or state.get('step')!='confirm_leave'
                    or state.get('game_id')!=g['game_id'] or state.get('token')!=confirmation[2]):
                raise RuleError('Подтверждение устарело. Нажмите «Выйти» ещё раз.')
            if state['phase']=='lobby' and g['status']!='lobby':
                # The owner may have started while the guest was deciding.
                # Explain the consequences before accepting a surrender.
                self.confirm_leave(u,g)
                return ''
            self.e.leave(uid)
            self.e.set_state(pid,{})
            self.e.send(pid,('Вы покинули комнату.' if g['status']=='lobby' else 'Вы вышли из игры.')+
                        '\n\n<b>Главное меню</b>',menu(),slot='room')
            if g['status']=='lobby':
                self.lobby(g['game_id'])
        elif data.startswith('a:'):
            _,gid,pos,selected = data.split(':')
            self.e.answer(int(gid),uid,int(pos),int(selected),received_at)
        elif cmd=='/history' or data.startswith('history:'):
            self.history(u,int(data.split(':')[1]) if data else 0)
        elif cmd=='/top' or data=='top':
            self.show_leaderboard(pid)
        elif text and not cmd and state.get('step')=='nick':
            changed = self.e.register(pid,text)
            self.e.set_state(pid,{})
            self.e.send(pid,'Теперь ваш ник — <b>%s</b>.'%html.escape(changed['nickname']),menu())
        elif text and not cmd and state.get('step')=='join':
            self.join(u,text)
        else:
            self.e.send(pid,'<b>Главное меню</b>\nВыберите действие кнопкой ниже. Во время игры отвечайте кнопками под вопросом.',menu())
        return ''

    def confirm_leave(self,u,g):
        participant=self.e.one('SELECT participant_status FROM game_participants WHERE game_id=? AND user_id=?',
                               (g['game_id'],u['user_id']))
        if not participant or participant[0] not in ('joined','playing'):
            raise RuleError('Для вас вопросы уже закончились. Итоги появятся, когда закончат остальные.')
        token=secrets.token_hex(4)
        self.e.set_state(u['platform_user_id'],{'step':'confirm_leave','game_id':g['game_id'],
                                              'phase':g['status'],'token':token})
        if g['status']=='lobby':
            text='Точно хотите покинуть комнату?'
        else:
            text='Точно хотите выйти из игры?\n\nЗа оставшиеся вопросы вы получите 0 очков.'
        self.e.send(u['platform_user_id'],text,
                    keyboard([('Да, выйти','leave_yes:%s:%s'%(g['game_id'],token)),('Отмена','room')]),slot='room')

    def confirm_lobby_deletion(self,u,gid):
        g=self.e.owned_lobby(gid,u['user_id'])
        self.e.send(u['platform_user_id'],'Удалить комнату <code>%s</code>?\n\n'
                    'Комната закроется для всех, и игра не начнётся.'%g['game_code'],
                    keyboard([('Да, удалить','delete_yes:%s'%gid),('Отмена','room')]),slot='room')

    def delete_lobby(self,u,gid):
        self.e.delete_lobby(gid,u['user_id'])
        self.e.set_state(u['platform_user_id'],{})
        self.e.send(u['platform_user_id'],'Комната удалена.\n\n<b>Главное меню</b>',menu(),slot='room')

    def join(self, u, code):
        gid = self.e.join(u['user_id'],code)
        self.e.set_state(u['platform_user_id'],{})
        self.lobby(gid)

    def wizard(self, pid, state, available=None):
        token = state['token']
        def button(label,key,value):
            return label,'w:%s:%s:%s'%(token,key,value)
        topic = state['topics'][state['topic']]
        rows = [[button(t,'topic',i)] for i,t in enumerate(state['topics'])]
        rows.append([button(v,'difficulty',k) for k,v in DIFFICULTIES.items()])
        if available is None:
            available = self.e.available_question_count(topic,state['difficulty'])
        rows.append([button(str(n)+' вопр.','count',n) for n in QUESTION_COUNTS if n<=available])
        rows.append([button(str(s)+' сек.','seconds',s) for s in QUESTION_TIMES])
        rows.append([button('Создать комнату','save','yes'),('Отмена','menu')])
        self.e.send(pid,'<b>Настройки комнаты</b>\n\nТема — %s\nСложность — %s\nВопросов — %s\nВремя на вопрос — %s с\n\n'
                    'Чтобы изменить настройку, выберите значение ниже. Когда всё готово, нажмите «Создать комнату».'%
                    (html.escape(topic),DIFFICULTIES[state['difficulty']],state['count'],state['seconds']),keyboard(*rows),slot='room')

    def wizard_action(self,u,data,state):
        _,token,key,value = data.split(':')
        if state.get('step')!='wizard' or token!=state.get('token'):
            raise RuleError('Эти настройки устарели. Отправьте /create, чтобы начать заново.')
        if key=='save':
            gid = self.e.create(u['user_id'],state['topics'][state['topic']],state['difficulty'],state['count'],state['seconds'])
            self.e.set_state(u['platform_user_id'],{})
            self.lobby(gid,u['user_id'])
            return
        allowed = {'topic':range(len(state['topics'])),'difficulty':DIFFICULTIES,
                   'count':QUESTION_COUNTS,'seconds':QUESTION_TIMES}
        if key not in allowed:
            raise RuleError('Не удалось распознать настройку. Используйте кнопки под текущими настройками.')
        value = value if key=='difficulty' else int(value)
        if value not in allowed[key]:
            raise RuleError('Этот вариант недоступен. Выберите другой из текущего меню.')
        state[key] = value
        available = self.e.available_question_count(state['topics'][state['topic']],state['difficulty'])
        if state['count']>available:
            choices = [n for n in QUESTION_COUNTS if n<=available]
            if not choices:
                raise RuleError('Для этой темы и сложности меньше 5 вопросов. Выберите другую тему или сложность.')
            state['count']=max(choices)
        self.e.set_state(u['platform_user_id'],state)
        self.wizard(u['platform_user_id'],state,available)

    def lobby(self,gid,user_id=None):
        game = self.e.one('SELECT * FROM games WHERE game_id=?',(gid,))
        players = self.e.participants(gid)
        recipients = players if user_id is None else [p for p in players if p['user_id']==user_id]
        if not recipients:
            raise RuleError('Вы не участник этой игры.')
        for player in recipients:
            self.show_lobby(game,players,player)

    def show_lobby(self,g,players,p):
        gid = g['game_id']
        if g['status']=='in_progress' and p['participant_status']=='playing':
            self.e.send(p['platform_user_id'],'',kind='question',game_id=gid,user_id=p['user_id'],position=p['current_position'])
            return
        lines = [f"<b>Комната {g['game_code']}</b>", f"Тема — {html.escape(g['topic'])}",
                 f"Сложность — {DIFFICULTIES[g['difficulty']]}",
                 f"{g['question_count']} вопросов · {g['question_time_limit_seconds']} с на вопрос",
                 '', f'<b>Игроки ({len(players)} из 10)</b>']
        for participant in players:
            label = mention(participant['platform_user_id'],participant['nickname'])
            if g['status']=='lobby':
                status = ' — создатель' if participant['user_id']==g['created_by_user_id'] else ''
            else:
                status = ' — '+{'joined':'в комнате','playing':'отвечает','finished':'закончил','forfeited':'вышел'}[participant['participant_status']]
            lines.append('• '+label+status)
        rows = [[('Обновить','room'),('Выйти','leave')]]
        if g['status']=='in_progress':
            lines.extend(['','Вы закончили отвечать.' if p['participant_status']=='finished' else 'Вы вышли из игры.',
                          'Итоги появятся здесь, когда закончат остальные.'])
            rows = [[('Обновить','room'),('Меню','menu')]]
        if g['status']=='lobby':
            if g['created_by_user_id']==p['user_id']:
                rows = [[('Обновить','room'),('Удалить','delete:%s'%gid)]]
                next_step = ('Поделитесь ссылкой или кодом. Для старта нужны хотя бы два игрока.' if len(players)<2
                             else 'Когда все соберутся, нажмите «Начать игру».')
                rows.insert(0,[('Начать игру','start:%s'%gid)])
            else:
                next_step = 'Игру запустит создатель комнаты.'
            lines.extend(['',next_step,'','<b>Как пригласить</b>',f"Код комнаты — <code>{g['game_code']}</code>",
                          'https://t.me/%s?start=join_%s'%(self.bot_username,g['game_code'])])
        self.e.send(p['platform_user_id'],'\n'.join(lines),keyboard(*rows),slot='room')

    def show_leaderboard(self,pid):
        now = datetime.fromtimestamp(self.e.clock(), LOCAL_TZ)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (month_start + timedelta(days=31)).replace(day=1)
        leaders = self.e.leaderboard(month_start.timestamp(), next_month.timestamp())
        lines = ['<b>Общий рейтинг за месяц</b>', f'{month_start:%m.%Y} · UTC+5', '']
        for player in leaders:
            nickname = html.escape(player['nickname'])
            points = format_count(player['points'],'балл','балла','баллов')
            lines.append(f"{player['place']}. {nickname} — {points}")
        if not leaders:
            lines.append('В этом месяце пока никто не набрал баллы.')
        lines.extend(['', '<b>Как получить баллы</b>',
                      '1-е место — 3 балла · 2-е — 2 балла · 3-е — 1 балл.',
                      'Чтобы получить баллы, завершите игру и дайте хотя бы один верный ответ.',
                      'В таблице до 20 игроков. При равном числе баллов место общее.'])
        self.e.send(pid,'\n'.join(lines),keyboard([('Меню','menu')]))

    def history(self,u,page):
        page=max(0,page)
        records = self.e.all('''SELECT g.game_code,g.created_at,g.status,p.total_score,p.rank
            FROM games g JOIN game_participants p USING(game_id)
            WHERE p.user_id=? ORDER BY p.game_id DESC LIMIT 6 OFFSET ?''',(u['user_id'],page*5))
        lines = [f'<b>История игр · страница {page+1}</b>', '']
        for g in records[:5]:
            date = datetime.fromtimestamp(g['created_at'],LOCAL_TZ).strftime('%d.%m.%Y')
            code = html.escape(g['game_code'])
            status = {'lobby':'Ждёт старта','in_progress':'Идёт','completed':'Завершена','cancelled':'Отменена'}[g['status']]
            if g['status']=='completed':
                place = f"{g['rank']}-е место, " if g['rank'] is not None else ''
                status += ' · '+place+format_count(g['total_score'],'очко','очка','очков')
            lines.append(f'<code>{code}</code> · {date} · {status}')
        if not records:
            lines.append('Игр пока нет.')
        navigation=[]
        if page:
            navigation.append(('← Назад','history:%s'%(page-1)))
        if len(records)>5:
            navigation.append(('Далее →','history:%s'%(page+1)))
        rows=[navigation] if navigation else []
        rows.append([('Меню','menu')])
        self.e.send(u['platform_user_id'],'\n'.join(lines),keyboard(*rows))
