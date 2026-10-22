import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from chekino.api import ApiError, canonical_id, normalize
from chekino.app import App
from chekino.store import Store
from chekino.__main__ import load_config


def movie(mid, **extra):
    return dict(id=mid, title=f'Фильм {mid}', year=2000, kind='FILM',
                description='Описание', poster='https://example.org/poster.jpg', genres=['драма']) | extra


class Provider:
    def __init__(self):
        self.results = [movie(1)]
        self.exclusions = []
        self.fail = False

    def search(self, text):
        if self.fail:
            raise ApiError(429)
        return self.results

    def detail(self, mid):
        return movie(mid)

    def recommend(self, excluded, preferences):
        self.exclusions.append(excluded)
        return next((movie(n) for n in range(1, 20) if n not in excluded), None)


class Telegram:
    def __init__(self):
        self.calls = []
        self.next_id = 1000
        self.fail_edit = None
        self.fail_send = False

    def call(self, method, **payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == 'editMessageText' and self.fail_edit:
            error, self.fail_edit = self.fail_edit, None
            raise error
        if method == 'sendMessage':
            if self.fail_send:
                raise ApiError(503)
            self.next_id += 1
            return {'message_id': self.next_id}
        return True


class AppTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(':memory:')
        self.p, self.tg = Provider(), Telegram()
        self.app = App(self.s, self.p, self.tg, (11, 22))
        self.sequence = 0

    def tearDown(self):
        self.s.db.close()

    def message(self, text, uid=11, deliver=True, chat_type='private'):
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {'message_id': self.sequence,
                  'from': {'id': uid}, 'chat': {'id': uid, 'type': chat_type}, 'text': text}}
        self.app.handle(update)
        if deliver:
            self.app.deliver()
        return update

    def callback(self, action, uid=11, update=None, deliver=True):
        if update is None:
            self.sequence += 1
            user = self.s.user(uid)
            update = {'update_id': self.sequence, 'callback_query': {
                'id': str(self.sequence), 'from': {'id': uid},
                'data': f"{user['revision']}:{action}", 'message': {
                    'message_id': user['message'], 'chat': {'id': uid, 'type': 'private'}}}}
        self.app.handle(update)
        if deliver:
            self.app.deliver()
        return update

    def test_shared_queue_first_vote_and_independent_second(self):
        self.message('https://kinopoisk.ru/film/1/')
        self.message('/queue', 22)
        self.assertEqual(self.s.ids('queue', 22), [1])
        self.callback('score')
        self.assertEqual(self.s.ids('queue', 22), [1], 'opening stars must not mark watched')
        update = self.callback('vote:5')
        self.assertEqual(self.s.ids('queue', 11), [])
        self.assertEqual(self.s.ids('queue', 22), [])
        self.assertEqual(self.s.ids('rate', 22), [1])
        self.assertEqual(self.s.votes(1), {11: 5})
        self.callback('rate', 22)
        self.assertIn('ждёт твоей оценки', self.app.render(22)['text'])
        self.assertNotIn('Оценка партнёра', self.app.render(22)['text'])
        self.callback('score', 22)
        self.callback('vote:2', 22)
        self.assertEqual(self.s.votes(1), {11: 5, 22: 2})
        self.assertIn('Оценка партнёра: 5', self.app.render(22)['text'])
        self.callback('score')
        self.callback('vote:4')
        self.assertEqual(self.s.votes(1), {11: 4, 22: 2})
        self.callback('', update=update)
        self.assertEqual(self.s.votes(1), {11: 4, 22: 2})
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM rating_history').fetchone()[0], 3)

    def test_dedup_duplicate_updates_and_callbacks(self):
        self.message('https://kinopoisk.ru/film/1/')
        self.callback('score')
        update = self.callback('vote:3')
        self.app.handle(update)
        duplicate = copy.deepcopy(update)
        duplicate['update_id'] += 100
        self.app.handle(duplicate)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM rating_history').fetchone()[0], 1)
        self.message('https://kinopoisk.ru/film/1/')
        self.assertEqual(self.s.catalog(), {1})
        self.assertEqual(self.s.ids('queue', 11), [])

    def test_new_all_actions_and_preferences(self):
        self.message('/new')
        self.assertEqual(self.s.catalog(), set())
        self.callback('new')
        self.assertEqual(self.s.user(11)['state']['mid'], 2)
        self.assertIn(1, self.p.exclusions[-1])
        self.message('/queue')
        self.message('/new')
        self.assertTrue({1, 2}.issubset(self.p.exclusions[-1]))
        self.callback('add')
        self.assertEqual(self.s.catalog(), {3})
        self.message('/new')
        self.callback('score')
        mid = self.s.user(11)['state']['mid']
        self.assertNotIn(mid, self.s.catalog())
        self.callback('vote:5')
        self.assertIn(mid, self.s.catalog())
        self.assertEqual(self.s.ids('rate', 22), [mid])
        self.assertEqual(self.s.preferences([11, 22]), {11: {'драма': 2}, 22: {}})
        self.message('/new')
        self.assertTrue(self.s.catalog().issubset(self.p.exclusions[-1]))

    def test_single_fuzzy_result_requires_confirmation(self):
        self.message('Похожее название')
        self.assertEqual(self.s.catalog(), set())
        self.callback('choose:1')
        self.assertEqual(self.s.catalog(), {1})

    def test_search_disambiguation(self):
        self.p.results = [movie(1), movie(2, kind='TV_SERIES', year=2020)]
        self.message('Название')
        self.assertEqual(self.s.catalog(), set())
        payload = self.app.render(11)
        self.assertIn('Сериал', payload['reply_markup']['inline_keyboard'][1][0]['text'])
        self.callback('choose:2')
        self.assertEqual(self.s.catalog(), {2})

    def test_stale_button_never_rates_another_film(self):
        self.message('https://kinopoisk.ru/film/1/')
        self.callback('score')
        old = self.s.user(11)
        self.p.results = [movie(2)]
        self.message('https://kinopoisk.ru/film/2/')
        self.sequence += 1
        update = {'update_id': self.sequence, 'callback_query': {'id': 'old',
                  'from': {'id': 11}, 'data': f"{old['revision']}:vote:1",
                  'message': {'message_id': old['message'], 'chat': {'id': 11, 'type': 'private'}}}}
        self.app.handle(update)
        self.assertEqual(self.s.votes(1), {})
        self.assertEqual(self.s.votes(2), {})

    def test_blocked_partner_does_not_block_other_chat(self):
        self.message('/queue', 22)
        self.tg.fail_edit = ApiError(403, 'Forbidden: bot was blocked by the user')
        self.message('https://kinopoisk.ru/film/1/')
        self.assertEqual(self.s.user(11)['dirty'], 0)
        self.assertEqual(self.s.catalog(), {1})

    def test_removed_queue_item_does_not_transfer_open_stars(self):
        self.message('https://kinopoisk.ru/film/1/')
        self.p.results = [movie(2)]
        self.message('https://kinopoisk.ru/film/2/')
        self.message('/queue')
        self.callback('score')
        with self.s.transaction():
            self.s.rate(1, 22, 3)
        self.app.deliver()
        state = self.s.user(11)['state']
        self.assertEqual(state['mid'], 2)
        self.assertFalse(state['scoring'])
        actions = [b['callback_data'].split(':')[1] for row in
                   self.app.render(11)['reply_markup']['inline_keyboard'] for b in row]
        self.assertNotIn('vote', actions)

    def test_private_access_only(self):
        self.message('Фильм', uid=33)
        self.message('Фильм', uid=11, chat_type='group')
        self.assertEqual(self.s.catalog(), set())
        self.assertEqual(self.tg.calls, [])

    def test_reuses_card_and_restores_deleted_card(self):
        self.message('https://kinopoisk.ru/film/1/')
        self.message('/queue')
        self.message('/random')
        self.assertEqual(sum(m == 'sendMessage' for m, _ in self.tg.calls), 1)
        self.tg.fail_edit = ApiError(400, 'Bad Request: message to edit not found')
        self.message('/queue')
        self.assertEqual(sum(m == 'sendMessage' for m, _ in self.tg.calls), 2)
        self.assertEqual(self.s.catalog(), {1})

    def test_delivery_failure_keeps_input_and_retries(self):
        self.tg.fail_send = True
        self.message('https://kinopoisk.ru/film/1/', deliver=False)
        with self.assertRaises(ApiError):
            self.app.deliver()
        self.assertEqual(self.s.catalog(), {1})
        self.assertEqual(self.s.user(11)['dirty'], 1)
        self.assertEqual(sum(m == 'deleteMessage' for m, _ in self.tg.calls), 0)
        self.tg.fail_send = False
        self.app.deliver()
        self.assertEqual(self.s.user(11)['dirty'], 0)
        self.assertEqual(sum(m == 'deleteMessage' for m, _ in self.tg.calls), 1)
        self.tg.fail_edit = ApiError(429, retry_after=5)
        self.message('/queue', deliver=False)
        with self.assertRaises(ApiError):
            self.app.deliver()
        self.assertEqual(sum(m == 'sendMessage' for m, _ in self.tg.calls), 2)  # includes failed send

    def test_errors_preserve_input_and_transaction_rolls_back(self):
        self.p.fail = True
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {'message_id': 1, 'text': 'Фильм',
                  'from': {'id': 11}, 'chat': {'id': 11, 'type': 'private'}}}
        with self.assertRaises(ApiError):
            self.app.handle(update)
        self.assertEqual(self.s.offset(), 0)
        self.app.error(update, 'Повтори поиск')
        self.app.deliver()
        self.assertEqual(sum(m == 'deleteMessage' for m, _ in self.tg.calls), 0)
        self.assertIn('Повтори', self.app.render(11)['text'])

    def test_independent_navigation_and_empty_random(self):
        self.message('/random')
        self.assertIn('Очередь пуста', self.app.render(11)['text'])
        self.message('https://kinopoisk.ru/film/1/')
        self.p.results = [movie(2)]
        self.message('https://kinopoisk.ru/film/2/')
        self.message('/queue')
        self.message('/queue', 22)
        other = self.s.user(22)['state']
        self.callback('page:1')
        self.assertEqual(self.s.user(11)['state']['mid'], 2)
        self.assertEqual(self.s.user(22)['state'], other)
        self.message('/history')
        self.assertEqual(self.s.user(11)['state']['mode'], 'history')

    def test_restart_backup_restore_and_rating_constraints(self):
        self.message('https://kinopoisk.ru/film/1/')
        self.callback('score')
        last = self.callback('vote:4')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            backup = Path(directory) / 'backup.sqlite'
            self.s.backup(backup)
            restored = Store(backup)
            try:
                self.assertEqual(restored.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(restored.votes(1), {11: 4})
                self.assertEqual(restored.ids('rate', 22), [1])
                restarted = App(restored, self.p, self.tg, (11, 22))
                restarted.handle(last)
                self.assertEqual(restored.db.execute('SELECT COUNT(*) FROM rating_history').fetchone()[0], 1)
                self.assertEqual(restored.offset(), last['update_id'] + 1)
            finally:
                restored.db.close()
        with self.assertRaises(ValueError):
            self.s.rate(1, 22, 6)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('INSERT INTO ratings VALUES(1,22,0)')

    def test_html_escaping_and_preview(self):
        self.message('https://kinopoisk.ru/film/1/')
        self.s.cache(movie(1, title='<script>&', description='<b>Пример</b>'))
        payload = self.app.render(11)
        self.assertIn('&lt;script&gt;&amp;', payload['text'])
        self.assertIn('&lt;b&gt;', payload['text'])
        self.assertEqual(payload['link_preview_options']['url'], movie(1)['poster'])


class ConfigTests(unittest.TestCase):
    def test_config_permissions_and_exactly_two_users(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            p = Path(directory) / 'config.json'
            cfg = {'token': '123:placeholder', 'metadata_key': 'placeholder',
                   'users': [11, 22], 'database': '/not/a/real/database'}
            p.write_text(json.dumps(cfg))
            p.chmod(0o600)
            self.assertEqual(load_config(p)['users'], [11, 22])
            p.chmod(0o644)
            with self.assertRaises(ValueError):
                load_config(p)
            p.chmod(0o600)
            for users in ([11], [11, 11], [True, 22], [-1, 22], [11, 22, 33]):
                cfg['users'] = users
                p.write_text(json.dumps(cfg))
                with self.assertRaises(ValueError):
                    load_config(p)

    def test_canonical_links_and_metadata(self):
        self.assertEqual(canonical_id('https://www.kinopoisk.ru/film/123/?utm_source=test'), 123)
        self.assertEqual(canonical_id('https://kinopoisk.ru/series/42/'), 42)
        self.assertIsNone(canonical_id('Название 2020'))
        for link in ['https://evil.test/film/123', 'https://kinopoisk.ru.evil.test/film/1/',
                     'https://kinopoisk.ru/film/0/', 'https://kinopoisk.ru/lists/1/']:
            with self.assertRaises(ValueError):
                canonical_id(link)
        m = normalize({'kinopoiskId': 1, 'nameOriginal': 'Original', 'posterUrl': 'http://unsafe'})
        self.assertEqual(m['title'], 'Original')
        self.assertEqual(m['poster'], '')


if __name__ == '__main__':
    unittest.main()
