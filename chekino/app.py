import html
import json
import random
from .api import ApiError, canonical_id


def button(text, data):
    return {'text': text, 'callback_data': data}


class App:
    def __init__(self, store, provider, telegram, users, reveal_partner=False):
        self.s, self.provider, self.tg = store, provider, telegram
        self.users, self.reveal_partner = tuple(users), reveal_partner

    def recommend(self, uid, state):
        excluded = self.s.catalog() | self.s.recently_shown(uid)
        movie = self.provider.recommend(excluded, self.s.preferences(self.users))
        if movie is None:
            return {'mode': 'new', 'notice': 'Новых рекомендаций сейчас нет. Попробуй позже.'}
        self.s.cache(movie)
        self.s.record_recommendation(uid, movie['id'])
        return {'mode': 'new', 'mid': movie['id']}

    def action(self, uid, state, action, argument=''):
        if action in {'queue', 'rate', 'history'}:
            return {'mode': action}
        if action == 'random':
            ids = self.s.ids('queue', uid)
            return {'mode': 'queue', **({'mid': random.choice(ids)} if ids else {})}
        if action == 'new':
            return self.recommend(uid, state)
        if action == 'page':
            ids = self.visible_ids(uid, state)
            if not ids:
                return {'mode': state['mode']}
            pos = ids.index(state['mid']) if state.get('mid') in ids else 0
            return {**state, 'mid': ids[(pos + int(argument)) % len(ids)], 'scoring': False}
        if action == 'choose':
            mid = int(argument)
            if mid not in state.get('choices', []):
                raise ValueError('Этот выбор устарел. Повтори поиск.')
            movie = self.provider.detail(mid)
            self.s.cache(movie)
            self.s.add(mid)
            return {'mode': 'detail', 'mid': mid}
        mid = state.get('mid')
        if not mid or not self.s.movie(mid):
            raise ValueError('Карточка устарела. Открой /queue.')
        if action == 'add':
            self.s.add(mid)
            return {'mode': 'detail', 'mid': mid}
        if action == 'score':
            return {**state, 'scoring': True}
        if action == 'cancel':
            return {**state, 'scoring': False}
        if action == 'vote' and state.get('scoring'):
            self.s.rate(mid, uid, int(argument))
            return {'mode': 'detail', 'mid': mid}
        raise ValueError('Кнопка устарела. Открой /queue.')

    def visible_ids(self, uid, state):
        if state['mode'] == 'history':
            return sorted(self.s.catalog())
        return self.s.ids(state['mode'], uid)

    def handle(self, update):
        """Commit domain state + dedup + UI outbox together, before any Telegram effect."""
        cb = update.get('callback_query')
        msg = (cb or {}).get('message') if cb else update.get('message')
        actor = (cb or msg or {}).get('from', {}).get('id')
        chat = (msg or {}).get('chat', {})
        with self.s.transaction():
            if self.s.db.execute('SELECT 1 FROM updates WHERE id=?', (update['update_id'],)).fetchone():
                return
            self.s.db.execute('INSERT INTO updates VALUES(?)', (update['update_id'],))
            if actor not in self.users or chat.get('type') != 'private' or chat.get('id') != actor:
                return
            user = self.s.user(actor)
            state = user['state'] if user else {'mode': 'queue'}
            if cb:
                if self.s.db.execute('SELECT 1 FROM callbacks WHERE id=?', (cb['id'],)).fetchone():
                    return
                self.s.db.execute('INSERT INTO callbacks VALUES(?)', (cb['id'],))
                parts = cb.get('data', '').split(':')
                if (not user or user['dirty'] or msg.get('message_id') != user['message']
                        or len(parts) not in (2, 3) or parts[0] != str(user['revision'])):
                    # Never retarget an old button to a different film.
                    if user:
                        self.s.db.execute('UPDATE users SET dirty=1 WHERE id=?', (actor,))
                    return
                state = self.action(actor, state, parts[1], parts[2] if len(parts) == 3 else '')
            else:
                text = msg.get('text', '').strip()
                if not text:
                    raise ValueError('Пришли название или ссылку на фильм, либо /queue.')
                command = text.split()[0].split('@')[0]
                if command in {'/start', '/help'}:
                    state = {'mode': 'queue', 'notice': 'Пришли название или ссылку Кинопоиска. '
                             '/queue — очередь, /new — рекомендации, /random — случайный фильм, '
                             '/rate — ждут оценки, /history — каталог и исправление оценок.'}
                elif command in {'/queue', '/new', '/random', '/rate', '/history'}:
                    state = self.action(actor, state, command[1:])
                else:
                    movies = self.provider.search(text)
                    for movie in movies:
                        self.s.cache(movie)
                    if not movies:
                        raise ValueError('Ничего не найдено. Уточни название или пришли ссылку Кинопоиска.')
                    if len(movies) == 1 and canonical_id(text) is not None:
                        movie = movies[0]
                        self.s.add(movie['id'])
                        state = {'mode': 'detail', 'mid': movie['id']}
                    else:
                        state = {'mode': 'search', 'choices': [m['id'] for m in movies]}
                self.s.db.execute('INSERT OR IGNORE INTO deletions VALUES(?,?)',
                                  (actor, msg['message_id']))
            self.s.set_state(actor, state)
            self.s.prune_transient()

    def error(self, update, notice):
        # Failed input is intentionally not scheduled for deletion.
        cb = update.get('callback_query')
        msg = (cb or {}).get('message') if cb else update.get('message')
        uid = (cb or msg or {}).get('from', {}).get('id')
        with self.s.transaction():
            self.s.db.execute('INSERT OR IGNORE INTO updates VALUES(?)', (update['update_id'],))
            if uid in self.users and (msg or {}).get('chat', {}).get('type') == 'private' \
                    and msg['chat']['id'] == uid:
                user = self.s.user(uid)
                state = user['state'] if user else {'mode': 'queue'}
                self.s.set_state(uid, {**state, 'notice': notice})

    def render(self, uid):
        user = self.s.user(uid)
        state = user['state']
        revision = user['revision']
        def b(label, action):
            return button(label, f'{revision}:{action}')
        rows, poster, lines = [], '', []
        if state.get('notice'):
            lines.append(html.escape(state['notice']))
        mode = state['mode']
        mid = state.get('mid')
        if mode in {'queue', 'rate', 'history'}:
            ids = self.visible_ids(uid, state)
            mid = mid if mid in ids else (ids[0] if ids else None)
            if mid != state.get('mid'):
                state = {**state, 'mid': mid, 'scoring': False}
                self.s.db.execute('UPDATE users SET state=? WHERE id=?',
                                  (json.dumps(state, ensure_ascii=False), uid))
            if mid:
                lines.append(f'{ids.index(mid) + 1} / {len(ids)}')
                rows.append([b('⬅️', 'page:-1'), b('➡️', 'page:1')])
            else:
                lines.append({'queue': 'Очередь пуста. Пришли название или открой /new.',
                              'rate': 'Все просмотренные фильмы оценены.',
                              'history': 'Каталог пока пуст.'}[mode])
        if mode == 'search':
            lines.append('Выбери произведение — оно попадёт в общую очередь:')
            for choice in state['choices']:
                m = self.s.movie(choice)
                label = f"{m['title']} · {m.get('year') or 'год неизвестен'} · {kind(m)}"
                rows.append([b(label[:90], f'choose:{choice}')])
        elif mid:
            movie = self.s.movie(mid)
            votes = self.s.votes(mid)
            poster = movie['poster']
            lines += [f"<b>{html.escape(movie['title'][:180])}</b>",
                      f"{movie.get('year') or 'Год неизвестен'} · {kind(movie)}",
                      html.escape(movie['description'][:1200]) or 'Описание недоступно.',
                      f'<a href="https://www.kinopoisk.ru/{"series" if movie["kind"] in {"TV_SERIES", "MINI_SERIES", "TV_SHOW"} else "film"}/{mid}/">Кинопоиск</a>']
            if votes:
                lines.append('ПРОСМОТРЕНО' + (' — ждёт твоей оценки' if uid not in votes else ''))
            elif mid in self.s.catalog():
                lines.append('В общей очереди')
            else:
                lines.append('Новая рекомендация для совместного просмотра')
            if uid in votes:
                lines.append(f'Твоя оценка: {votes[uid]} / 5')
            if uid in votes or self.reveal_partner:
                for other, value in votes.items():
                    if other != uid:
                        lines.append(f'Оценка партнёра: {value} / 5')
            if state.get('scoring'):
                rows += [[b(str(n), f'vote:{n}') for n in range(1, 6)], [b('Отмена', 'cancel')]]
            else:
                actions = [b('Изменить оценку' if uid in votes else 'Оценить', 'score')]
                if mid not in self.s.catalog():
                    actions.insert(0, b('В очередь', 'add'))
                if mode == 'new':
                    actions.append(b('Ещё', 'new'))
                rows.append(actions)
        pending = len(self.s.ids('rate', uid))
        rows += [[b('Очередь', 'queue'), b('Новое', 'new'), b('🎲', 'random')],
                 [b(f'⭐ Ждут оценки · {pending}', 'rate'), b('Каталог', 'history')]]
        payload = {'text': '\n\n'.join(lines), 'parse_mode': 'HTML',
                   'reply_markup': {'inline_keyboard': rows},
                   'link_preview_options': ({'url': poster, 'prefer_large_media': True,
                                              'show_above_text': True} if poster else {'is_disabled': True})}
        return payload

    def deliver(self):
        """Retry UI before deleting input. Missing cards are recreated only on confirmed 400."""
        for row in self.s.db.execute('SELECT id FROM users WHERE dirty=1').fetchall():
            uid = row[0]
            if uid not in self.users:
                continue
            user, payload = self.s.user(uid), self.render(uid)
            message = user['message']
            if message:
                try:
                    self.tg.call('editMessageText', chat_id=uid, message_id=message, **payload)
                except ApiError as exc:
                    if exc.code == 403:
                        # A blocked partner must not stop polling for the other person.
                        self.s.db.execute('UPDATE users SET dirty=0 WHERE id=?', (uid,))
                        continue
                    if exc.code == 400 and 'message is not modified' in exc.description.lower():
                        pass
                    elif exc.code == 400 and 'message to edit not found' in exc.description.lower():
                        message = None
                    else:
                        raise
            if not message:
                message = self.tg.call('sendMessage', chat_id=uid, **payload)['message_id']
            self.s.db.execute('UPDATE users SET message=?,dirty=0 WHERE id=?', (message, uid))
        for row in self.s.db.execute('SELECT chat,message FROM deletions').fetchall():
            if row[0] not in self.users:
                continue
            try:
                self.tg.call('deleteMessage', chat_id=row[0], message_id=row[1])
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
                # Includes older-than-48-hours inputs; do not retry these forever.
            self.s.db.execute('DELETE FROM deletions WHERE chat=? AND message=?', tuple(row))


def kind(movie):
    return {'FILM': 'Фильм', 'TV_SERIES': 'Сериал', 'MINI_SERIES': 'Мини-сериал',
            'TV_SHOW': 'Телешоу'}.get(movie['kind'], 'Тип неизвестен')
