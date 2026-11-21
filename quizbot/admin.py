"""Private Telegram screens for administering the question bank."""
import html
import secrets
from datetime import datetime, timezone

from .engine import DIFFICULTIES, RuleError, keyboard
from .questions import (
    QUESTION_DIFFICULTIES, QuestionBank, QuestionChanged, validate_options,
    validate_text, validate_topic,
)

PAGE_SIZE = 5
TOPIC_PAGE_SIZE = 8
MAX_TOPIC = 100
MAX_EXPLANATION = 1200
FIELDS = ('text', 'topic', 'difficulty', 'options', 'correct', 'explanation')
ADD_FIELDS = ('text', 'options', 'correct', 'explanation')
FIELD_LABELS = dict(zip(FIELDS, ('Текст', 'Тема', 'Сложность', 'Варианты', 'Верный ответ', 'Пояснение')))
ACTIONS = {'add': 'Добавлен', 'edit': 'Изменён', 'enable': 'Включён', 'disable': 'Отключён', 'delete': 'Удалён'}
TOPIC_PICKERS = {'topics': 'topic', 'rename_topics': 'rename', 'delete_topics': 'delete_topic'}
ADMIN_VERSION = 3


def parse_admin_ids(value):
    if not value.strip():
        return frozenset()
    entries = [entry.strip() for entry in value.split(',')]
    if any(not entry.isascii() or not entry.isdecimal() or not 0 < int(entry) < 2**52 for entry in entries):
        raise ValueError('ADMIN_IDS must contain positive Telegram user IDs separated by commas.')
    return frozenset(str(int(entry)) for entry in entries)


def shorten(text, limit, single_line=False):
    if single_line:
        text = ' '.join(text.split())
    encoded = text.encode('utf-16-le')
    if len(encoded) <= 2 * limit:
        return text
    return encoded[:2 * (limit - 1)].decode('utf-16-le', errors='ignore') + '…'


def pager(page, has_next, action):
    row = []
    if page:
        row.append(('← Назад', action+':prev'))
    if has_next:
        row.append(('Далее →', action+':next'))
    return [row] if row else []


class QuestionAdmin:
    def __init__(self, engine, admin_ids, player_menu):
        self.e = engine
        self.bank = QuestionBank(engine.db)
        self.admin_ids = frozenset(map(str, admin_ids))
        self.player_menu = player_menu

    def handle(self, pid, command, data, text, state):
        if pid not in self.admin_ids:
            raise RuleError('У вас нет доступа к управлению вопросами.')
        if command == '/admin':
            self.home(pid, {})
        elif command == '/cancel':
            self.home(pid, state, 'Действие отменено.')
        elif state.get('admin_version') != ADMIN_VERSION:
            raise RuleError('Меню изменилось. Откройте /admin заново.')
        elif data:
            parts = data.split(':')
            if (len(parts) not in (3, 4) or state.get('step') != 'admin'
                    or parts[1] != state.get('token')):
                raise RuleError('Кнопка устарела. Откройте /admin заново.')
            action = parts[2]
            value = parts[3] if len(parts) == 4 else None
            if action in ('open', 'topic', 'rename', 'delete_topic') and (
                    value is None or not value.isascii() or not value.isdecimal()
                    or len(value)>19 or int(value)>=2**63):
                raise RuleError('Выберите строку из списка.')
            self.callback(pid, state, action, value)
        elif text:
            self.receive_text(pid, state, text)
        return ''

    def view(self, pid, state, screen, body, rows):
        token = secrets.token_hex(6)
        state.update(screen=screen, token=token)
        self.e.set_state(pid, state)
        markup = keyboard(*[[(label, f'adm:{token}:{action}') for label, action in row] for row in rows])
        self.e.send(pid, body, markup)

    def home(self, pid, state, note=''):
        page = state.get('topic_page', 0)
        state = {'step': 'admin', 'admin_version': ADMIN_VERSION}
        self.show_topics(pid, state, page, note=note)

    def callback(self, pid, state, action, value):
        screen = state['screen']
        if action == 'exit' and screen == 'topics':
            self.e.set_state(pid, {})
            self.e.send(pid, '<b>Главное меню</b>\nВыберите действие.', self.player_menu())
        elif action == 'home':
            self.home(pid, state)
        elif action == 'topics' and screen in TOPIC_PICKERS:
            page = state.get('topic_page', 0)
            if value in ('prev', 'next'):
                page = max(0, page + (1 if value == 'next' else -1))
            self.show_topics(pid, state, page, screen)
        elif action in ('rename_topics', 'delete_topics') and screen == 'topics':
            self.show_topics(pid, state, state.get('topic_page', 0), action)
        elif action == 'topic' and screen == 'topics':
            state['topic_id'] = int(value)
            self.show_difficulties(pid, state)
        elif action == 'new_topic' and screen == 'topics':
            state['topic_edit_id'] = None
            self.ask_topic_name(pid, state)
        elif action == 'rename' and screen in ('rename_topics', 'difficulties'):
            topic = self.topic(int(value))
            state.update(topic_edit_id=topic['topic_id'], topic_edit_revision=topic['revision'],
                         topic_edit_name=topic['name'])
            self.ask_topic_name(pid, state)
        elif action == 'save_topic' and screen == 'topic_preview':
            self.save_topic(pid, state)
        elif action == 'delete_topic' and screen == 'delete_topics':
            self.confirm_topic_deletion(pid, state, self.topic(int(value)))
        elif action == 'apply_topic_deletion' and screen == 'confirm_topic_deletion':
            self.apply_topic_deletion(pid, state)
        elif action == 'difficulties' and screen == 'list':
            self.show_difficulties(pid, state)
        elif action == 'difficulty' and screen == 'difficulties' and value in QUESTION_DIFFICULTIES:
            state.update(difficulty=value, page=0, filters={})
            self.show_list(pid, state)
        elif action == 'list' and screen in ('list', 'search', 'filters', 'question'):
            if value in ('prev', 'next') and screen == 'list':
                state['page'] = max(0, state['page'] + (1 if value == 'next' else -1))
            self.show_list(pid, state)
        elif action == 'search' and screen == 'list':
            self.search_prompt(pid, state)
        elif action == 'filters' and screen == 'list':
            self.show_filters(pid, state)
        elif action in ('active', 'reset') and screen == 'filters':
            self.change_filter(pid, state, action, value)
        elif action == 'open' and screen == 'list':
            self.show_question(pid, state, self.question(int(value)))
        elif action == 'add' and screen == 'list':
            topic = self.topic(state['topic_id'])
            state['draft'] = {'topic': topic['name'], 'difficulty': state['difficulty']}
            state.update(adding=True, draft_topic_id=topic['topic_id'])
            self.ask_field(pid, state, 'text')
        elif action == 'edit' and screen == 'question':
            question = self.question(state['question_id'])
            topic = self.bank.topic_by_name(question['topic'])
            state.update(draft=self.bank.draft(question), revision=question['revision'], adding=False,
                         draft_topic_id=topic['topic_id'])
            self.preview(pid, state)
        elif action == 'field' and screen == 'preview' and value in FIELDS:
            self.ask_field(pid, state, value)
        elif action == 'choose' and screen == 'field':
            self.choose_field(pid, state, value)
        elif action == 'save' and screen == 'preview':
            self.save(pid, state)
        elif action == 'toggle' and screen == 'question':
            self.confirm_toggle(pid, state)
        elif action == 'apply_toggle' and screen == 'confirm_toggle':
            self.apply_toggle(pid, state)
        elif action == 'delete_question' and screen == 'question':
            self.confirm_question_deletion(pid, state)
        elif action == 'apply_question_deletion' and screen == 'confirm_question_deletion':
            self.apply_question_deletion(pid, state)
        elif action == 'back_question' and screen in ('confirm_toggle', 'confirm_question_deletion'):
            self.show_question(pid, state, self.question(state['question_id']))
        elif action == 'log' and screen in ('topics', 'log'):
            page = state.get('log_page', 0) if screen == 'log' else 0
            if value in ('prev', 'next'):
                page = max(0, page + (1 if value == 'next' else -1))
            self.show_log(pid, state, page)
        else:
            raise RuleError('Кнопка устарела. Откройте /admin заново.')

    def show_list(self, pid, state, note=''):
        topic = self.bank.topic(state['topic_id'])
        if topic is None:
            self.home(pid, state, 'Эта тема уже удалена.')
            return
        filters, page = self.scope_filters(state, topic), state['page']
        questions = self.bank.list(filters, PAGE_SIZE+1, page*PAGE_SIZE)
        if page and not questions:
            page = state['page'] = 0
            questions = self.bank.list(filters, PAGE_SIZE+1, 0)
        lines = [f'<b>Вопросы · страница {page+1}</b>', self.filter_summary(filters)]
        if note:
            lines.extend(['', note])
        rows = []
        for question in questions[:PAGE_SIZE]:
            qid = question['question_id']
            excerpt = shorten(question['question_text'], 55, single_line=True)
            status = 'включён' if question['is_active'] else 'отключён'
            lines.extend(['', f'<b>#{qid} · {html.escape(excerpt)}</b> · {status}'])
            rows.append([(f'#{qid} · {excerpt}', f'open:{qid}')])
        if not questions:
            lines.extend(['', 'Вопросы не найдены. Добавьте вопрос или измените поиск и статус.'])
        rows.extend(pager(page, len(questions)>PAGE_SIZE, 'list'))
        rows.extend([[('Добавить вопрос', 'add')], [('Поиск', 'search'), ('Фильтры', 'filters')],
                     [('К сложности', 'difficulties'), ('К темам', 'home')]])
        self.view(pid, state, 'list', '\n'.join(lines), rows)

    def scope_filters(self, state, topic):
        return {**state['filters'], 'topic': topic['name'], 'difficulty': state['difficulty']}

    def filter_summary(self, filters):
        topic = shorten(filters.get('topic', 'Все темы'), 70, single_line=True)
        difficulty = DIFFICULTIES.get(filters.get('difficulty'), 'Любая сложность')
        active = {0: 'Отключённые', 1: 'Включённые'}.get(filters.get('active'), 'Любой статус')
        lines = [f'{html.escape(topic)} · {difficulty} · {active}']
        if filters.get('query'):
            lines.append('Поиск — '+html.escape(filters['query']))
        return '\n'.join(lines)

    def search_prompt(self, pid, state, error=''):
        body = '<b>Поиск по тексту вопроса</b>\nОтправьте слово или фразу до 150 символов. Поиск действует в выбранной теме и сложности.'
        if error:
            body += '\n\n'+error
        self.view(pid, state, 'search', body, [[('К списку', 'list')]])

    def show_filters(self, pid, state):
        body = '<b>Фильтры</b>\n'+self.filter_summary(self.scope_filters(state, self.topic(state['topic_id'])))
        rows = [
            [('Включённые', 'active:1'), ('Отключённые', 'active:0'), ('Все статусы', 'active:all')],
            [('Сбросить поиск и фильтры', 'reset')],
            [('К списку', 'list')],
        ]
        self.view(pid, state, 'filters', body, rows)

    def show_topics(self, pid, state, page, screen='topics', note=''):
        topics = self.bank.topics(TOPIC_PAGE_SIZE+1, page*TOPIC_PAGE_SIZE)
        if page and not topics:
            page = 0
            topics = self.bank.topics(TOPIC_PAGE_SIZE+1, 0)
        state['topic_page'] = page
        action = TOPIC_PICKERS[screen]
        rows = [[(shorten(topic['name'], 52, single_line=True), f"{action}:{topic['topic_id']}")]
                for topic in topics[:TOPIC_PAGE_SIZE]]
        rows.extend(pager(page, len(topics)>TOPIC_PAGE_SIZE, 'topics'))
        if screen != 'topics':
            verb = 'переименовать' if screen == 'rename_topics' else 'удалить'
            body = f'<b>Какую тему {verb}?</b>'
            rows.append([('К темам', 'home')])
        else:
            body = '<b>Банк вопросов · Темы</b>\n'+('Выберите тему, чтобы открыть её вопросы.' if topics
                                                    else 'Создайте первую тему, чтобы добавить в неё вопросы.')
            manage = [('Создать тему', 'new_topic')]
            rows.append(manage)
            if topics:
                manage.append(('Переименовать тему', 'rename_topics'))
                rows.append([('Удалить тему', 'delete_topics')])
            rows.append([('Журнал изменений', 'log'), ('Главное меню', 'exit')])
        if note:
            body += '\n\n'+note
        self.view(pid, state, screen, body, rows)

    def topic(self, topic_id):
        topic = self.bank.topic(topic_id)
        if topic is None:
            raise RuleError('Тема не найдена. Откройте список тем заново.')
        return topic

    def show_difficulties(self, pid, state):
        topic = self.topic(state['topic_id'])
        body = '<b>'+html.escape(shorten(topic['name'], MAX_TOPIC))+'</b>\nВыберите сложность вопросов.'
        rows = [[(DIFFICULTIES[key], f'difficulty:{key}')] for key in QUESTION_DIFFICULTIES]
        rows.append([('К темам', 'home')])
        self.view(pid, state, 'difficulties', body, rows)

    def ask_topic_name(self, pid, state, error=''):
        if state['topic_edit_id'] is None:
            body = '<b>Новая тема</b>\nОтправьте название темы, до 100 символов в одну строку.'
        else:
            old_name = html.escape(shorten(state['topic_edit_name'], MAX_TOPIC))
            body = f'<b>Переименование темы</b>\nТекущее название — {old_name}.\nОтправьте новое название.'
        if error:
            body += '\n\n'+html.escape(error)
        self.view(pid, state, 'topic_name', body, [[('Отмена', 'home')]])

    def preview_topic(self, pid, state, name):
        topic_id = state['topic_edit_id']
        try:
            self.validate_topic_name(name)
            if topic_id is None or name != state['topic_edit_name']:
                self.bank.ensure_topic_name_available(name, topic_id)
        except ValueError as exc:
            self.ask_topic_name(pid, state, str(exc))
            return
        state['topic_new_name'] = name
        escaped = html.escape(name)
        if topic_id is None:
            body = f'<b>Создать тему?</b>\n\n{escaped}'
        else:
            old_name = html.escape(shorten(state['topic_edit_name'], MAX_TOPIC))
            body = f'<b>Переименовать тему?</b>\n\n{old_name} → {escaped}\n\nНазвание обновится у вопросов этой темы.'
        self.view(pid, state, 'topic_preview', body, [[('Подтвердить', 'save_topic'), ('Отмена', 'home')]])

    def save_topic(self, pid, state):
        tid, name = state['topic_edit_id'], state['topic_new_name']
        try:
            if tid is None:
                self.bank.create_topic(name)
                note = 'Тема создана.'
            else:
                renamed = self.bank.rename_topic(tid, state['topic_edit_revision'], name, pid, self.e.clock())
                note = 'Тема переименована.' if renamed else 'Название темы не изменилось.'
        except ValueError as exc:
            self.ask_topic_name(pid, state, str(exc))
            return
        except QuestionChanged:
            self.home(pid, state, 'Тема уже изменилась. Выберите её заново.')
            return
        state['topic_page'] = 0
        self.home(pid, state, note)

    def confirm_topic_deletion(self, pid, state, topic):
        state.update(topic_id=topic['topic_id'], topic_revision=topic['revision'])
        name = html.escape(shorten(topic['name'], MAX_TOPIC))
        body = (f'<b>Удалить тему «{name}»?</b>\n\n'
                'Из банка исчезнут тема и все её вопросы, включая отключённые. '
                'Уже начатые игры и их результаты сохранятся. '
                'Для новых игр выберите другую тему.')
        self.view(pid, state, 'confirm_topic_deletion', body,
                  [[('Да, удалить', 'apply_topic_deletion'), ('Отмена', 'home')]])

    def apply_topic_deletion(self, pid, state):
        try:
            self.bank.delete_topic(state['topic_id'], state['topic_revision'], pid, self.e.clock())
            note = 'Тема и её вопросы удалены.'
        except QuestionChanged:
            note = 'Тема уже изменилась или была удалена. Проверьте список тем.'
        self.home(pid, state, note)

    def change_filter(self, pid, state, key, value):
        filters = state['filters']
        if key == 'reset':
            filters.clear()
        elif key == 'active' and value == 'all':
            filters.pop('active', None)
        elif key == 'active' and value in ('0', '1'):
            filters['active'] = int(value)
        else:
            raise RuleError('Выберите фильтр из списка.')
        state['page'] = 0
        self.show_list(pid, state)

    def question(self, question_id):
        question = self.bank.get(question_id)
        if question is None:
            raise RuleError('Вопрос не найден. Вернитесь к списку.')
        return question

    def question_text(self, draft):
        topic = html.escape(shorten(draft['topic'], MAX_TOPIC))
        question = html.escape(shorten(draft['text'], 1500))
        lines = [f'Тема — {topic}', f"Сложность — {DIFFICULTIES[draft['difficulty']]}", '', question, '']
        for index, option in enumerate(draft['options']):
            mark = ' ✓' if index == draft.get('correct') else ''
            lines.append(f'{index+1}. {html.escape(shorten(option, 150))}{mark}')
        if draft.get('explanation'):
            lines.extend(['', '<b>Пояснение</b>', html.escape(shorten(draft['explanation'], MAX_EXPLANATION))])
        return '\n'.join(lines)

    def show_question(self, pid, state, question, note=''):
        for key in ('draft', 'draft_topic_id', 'adding', 'field', 'revision', 'target_active'):
            state.pop(key, None)
        qid = question['question_id']
        active = question['is_active']
        status = 'Включён' if active else 'Отключён'
        source_key = html.escape(shorten(question['source_key'], 80))
        body = f'<b>Вопрос #{qid} · {status}</b>\nКлюч для импорта — <code>{source_key}</code>\n\n'
        body += self.question_text(self.bank.draft(question))
        if note:
            body = note+'\n\n'+body
        state.update(question_id=qid)
        self.view(pid, state, 'question', body, [
            [('Редактировать', 'edit'), ('Отключить' if active else 'Включить', 'toggle')],
            [('Удалить вопрос', 'delete_question')],
            [('К списку', 'list'), ('В админку', 'home')],
        ])

    def ask_field(self, pid, state, field, error=''):
        draft = state['draft']
        rows = []
        if field == 'text':
            hint = 'Отправьте текст вопроса. Максимум 1500 символов.'
        elif field == 'topic':
            hint = 'Отправьте точное название существующей темы. Список тем доступен в /admin.'
        elif field == 'difficulty':
            hint = 'Выберите сложность.'
            rows.append([(DIFFICULTIES[key], f'choose:{key}') for key in QUESTION_DIFFICULTIES])
        elif field == 'options':
            hint = 'Отправьте от 2 до 6 вариантов одним сообщением, по одному на строку. До 150 символов на вариант.'
        elif field == 'correct':
            hint = 'Выберите номер верного варианта.\n\n'+'\n'.join(
                f'{index+1}. {html.escape(shorten(option, 150))}' for index, option in enumerate(draft['options']))
            rows.append([(str(index+1), f'choose:{index}') for index in range(len(draft['options']))])
        else:
            hint = 'Отправьте пояснение к правильному ответу. Его можно оставить пустым.'
            rows.append([('Пропустить' if not draft.get('explanation') else 'Убрать пояснение', 'choose:empty')])
        if 'explanation' in draft and 'correct' in draft:
            rows.append([('К предпросмотру', 'choose:preview')])
        rows.append([('Отмена', 'home')])
        title = FIELD_LABELS[field]
        if state['adding'] and field in ADD_FIELDS and 'explanation' not in draft:
            title = f'Шаг {ADD_FIELDS.index(field)+1} из {len(ADD_FIELDS)} · {title}'
        body = f'<b>{title}</b>\n{hint}'
        if error:
            body += '\n\n'+html.escape(error)
        state['field'] = field
        self.view(pid, state, 'field', body, rows)

    def receive_text(self, pid, state, text):
        if state['screen'] == 'topic_name':
            self.preview_topic(pid, state, text)
            return
        if state['screen'] == 'search':
            if len(text) > 150:
                self.search_prompt(pid, state, 'Сократите поисковую фразу до 150 символов.')
            else:
                state['filters']['query'] = text
                state['page'] = 0
                self.show_list(pid, state)
            return
        if state['screen'] != 'field':
            raise RuleError('Выберите действие кнопкой или откройте /admin.')
        field = state['field']
        try:
            value = self.parse_field(field, text)
        except ValueError as exc:
            self.ask_field(pid, state, field, str(exc))
            return
        self.accept_field(pid, state, field, value)

    def parse_field(self, field, text):
        if field == 'text':
            validate_text(text)
        elif field == 'topic':
            if self.bank.topic_by_name(text) is None:
                raise ValueError('Такой темы нет. Проверьте название или создайте тему в /admin.')
        elif field == 'options':
            options = [line.strip() for line in text.splitlines()]
            validate_options(options)
            return options
        elif field in ('difficulty', 'correct'):
            raise ValueError('Выберите вариант кнопкой под сообщением.')
        elif field == 'explanation' and len(text) > MAX_EXPLANATION:
            raise ValueError(f'Сократите пояснение до {MAX_EXPLANATION} символов.')
        return text

    def validate_topic_name(self, name):
        validate_topic(name)
        if len(name) > MAX_TOPIC or len(name.splitlines()) != 1:
            raise ValueError(f'Название темы должно занимать одну строку и содержать до {MAX_TOPIC} символов.')
        if name.casefold() == 'все темы':
            raise ValueError('«Все темы» уже используется в меню. Выберите другое название.')

    def choose_field(self, pid, state, value):
        draft, field = state['draft'], state['field']
        if value == 'preview' and 'explanation' in draft and 'correct' in draft:
            self.preview(pid, state)
            return
        if field == 'difficulty' and value in QUESTION_DIFFICULTIES:
            chosen = value
        elif field == 'correct' and value is not None and value.isdecimal() and 0 <= int(value) < len(draft['options']):
            chosen = int(value)
        elif field == 'explanation' and value == 'empty':
            chosen = ''
        else:
            raise RuleError('Выберите вариант под текущим вопросом.')
        self.accept_field(pid, state, field, chosen)

    def accept_field(self, pid, state, field, value):
        draft = state['draft']
        if field == 'options' and draft.get('options') != value:
            draft.pop('correct', None)
        if field == 'topic':
            state['draft_topic_id'] = self.bank.topic_by_name(value)['topic_id']
        draft[field] = value
        missing = next((key for key in FIELDS if key not in draft), None)
        if missing is None:
            self.preview(pid, state)
        else:
            self.ask_field(pid, state, missing)

    def preview(self, pid, state):
        draft = state['draft']
        body = '<b>Предпросмотр</b>\n\n'+self.question_text(draft)
        body += '\n\nВерный вариант отмечен ✓. Проверьте вопрос и подтвердите сохранение.'
        rows = [[(FIELD_LABELS[field], f'field:{field}') for field in group]
                for group in (FIELDS[:3], FIELDS[3:])]
        rows.append([('Подтвердить сохранение', 'save'), ('Отмена', 'home')])
        self.view(pid, state, 'preview', body, rows)

    def save(self, pid, state):
        qid = None if state['adding'] else state['question_id']
        topic = self.bank.topic(state['draft_topic_id'])
        if topic is None or topic['name'] != state['draft']['topic']:
            raise RuleError('Тема изменилась или была удалена. Выберите поле «Тема» и укажите действующее название.')
        try:
            if qid is not None:
                current = self.question(qid)
                if current['revision'] != state['revision']:
                    raise QuestionChanged
                if self.bank.draft(current) == state['draft']:
                    self.show_question(pid, state, current, 'Изменений нет.')
                    return
            qid = self.bank.save(state['draft'], pid, self.e.clock(), qid, state.get('revision'))
        except QuestionChanged:
            self.show_question(pid, state, self.question(qid), 'Вопрос уже изменился. Откройте редактирование заново.')
            return
        except ValueError as exc:
            raise RuleError(str(exc)) from None
        self.show_question(pid, state, self.question(qid), 'Вопрос сохранён.')

    def confirm_toggle(self, pid, state):
        question = self.question(state['question_id'])
        active = not question['is_active']
        state.update(revision=question['revision'], target_active=int(active))
        verb = 'Включить' if active else 'Отключить'
        effect = ('Вопрос снова сможет попасть в новые игры.' if active
                  else 'Вопрос исчезнет из подбора для новых игр. История сохранится.')
        body = f"<b>{verb} вопрос #{question['question_id']}?</b>\n\n{effect}"
        self.view(pid, state, 'confirm_toggle', body, [[('Подтвердить', 'apply_toggle'), ('Назад', 'back_question')]])

    def apply_toggle(self, pid, state):
        qid = state['question_id']
        try:
            self.bank.set_active(qid, state['target_active'], state['revision'], pid, self.e.clock())
            note = 'Вопрос включён.' if state['target_active'] else 'Вопрос отключён.'
        except QuestionChanged:
            note = 'Вопрос уже изменился. Проверьте его текущий статус.'
        self.show_question(pid, state, self.question(qid), note)

    def confirm_question_deletion(self, pid, state):
        question = self.question(state['question_id'])
        state['revision'] = question['revision']
        text = html.escape(shorten(question['question_text'], 180, single_line=True))
        body = (f'<b>Удалить вопрос #{question["question_id"]}?</b>\n\n{text}\n\n'
                'Вопрос исчезнет из банка и новых игр. В сыгранных и уже начатых играх он сохранится.')
        self.view(pid, state, 'confirm_question_deletion', body,
                  [[('Да, удалить', 'apply_question_deletion'), ('Отмена', 'back_question')]])

    def apply_question_deletion(self, pid, state):
        try:
            self.bank.delete_question(state['question_id'], state['revision'], pid, self.e.clock())
            note = 'Вопрос удалён.'
        except QuestionChanged:
            note = 'Вопрос уже изменился или был удалён. Проверьте список вопросов.'
        self.show_list(pid, state, note)

    def show_log(self, pid, state, page):
        entries = self.bank.log_page(PAGE_SIZE+1, page*PAGE_SIZE)
        state['log_page'] = page
        lines = [f'<b>Журнал изменений · страница {page+1}</b>', 'Время указано по UTC.']
        for entry in entries[:PAGE_SIZE]:
            date = datetime.fromtimestamp(entry['occurred_at'], timezone.utc)
            actor = html.escape(entry['actor_telegram_id'])
            if entry['action'] == 'delete_topic':
                name = html.escape(shorten(entry['topic_name'], MAX_TOPIC))
                action = f'Удалена тема «{name}»'
            else:
                action = f"{ACTIONS[entry['action']]} вопрос #{entry['question_id']}"
            lines.extend(['', f'{date:%d.%m.%Y %H:%M} · {action}', f'Администратор · <code>{actor}</code>'])
        if not entries:
            lines.append('\nИзменений через Telegram пока нет.')
        rows = pager(page, len(entries)>PAGE_SIZE, 'log') + [[('К темам', 'home')]]
        self.view(pid, state, 'log', '\n'.join(lines), rows)
