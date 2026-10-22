import io
import json
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
from chekino.api import ApiError, CallbackAcker, Kinopoisk, Telegram, ipv4_connection, request
from chekino.store import Store


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class ApiTests(unittest.TestCase):
    def test_blocked_ack_does_not_delay_card_or_queue_more_acks(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class SlowTelegram:
            def call(self, method, **payload):
                calls.append(method)
                if method == 'answerCallbackQuery':
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError('worker not released')
                    raise ApiError()
                return True
        tg = SlowTelegram()
        ack = CallbackAcker(tg)
        try:
            self.assertTrue(ack.submit('first'))
            self.assertTrue(entered.wait(1))
            self.assertFalse(ack.submit('second'))
            # Delivery proceeds while the acknowledgement is still blocked.
            self.assertTrue(tg.call('editMessageText'))
            self.assertFalse(ack.future.done())
        finally:
            release.set()
            ack.executor.shutdown(wait=True)
        ack.future.result()
        self.assertEqual(calls, ['answerCallbackQuery', 'editMessageText'])

    def test_short_poll_uses_durable_offset_without_long_server_wait(self):
        tg = Telegram('placeholder')
        with patch('chekino.api.request', return_value={'ok': True, 'result': []}) as http, \
                patch('chekino.api.time.sleep') as sleep:
            self.assertEqual(tg.poll(42), [])
        self.assertEqual(http.call_args.args[1]['offset'], 42)
        self.assertEqual(http.call_args.args[1]['timeout'], 0)
        self.assertEqual(http.call_args.kwargs['timeout'], 3)
        self.assertTrue(http.call_args.kwargs['ipv4'])
        sleep.assert_called_once_with(0.35)

    def test_short_poll_failure_recovers_without_global_backoff(self):
        tg = Telegram('placeholder')
        with patch.object(tg, 'call', side_effect=[ApiError(), [{'update_id': 42}]]), \
                patch('chekino.api.time.sleep') as sleep:
            self.assertEqual(tg.poll(42), [])
            self.assertEqual(tg.poll(42), [{'update_id': 42}])
        sleep.assert_called_once_with(0.5)
        with patch.object(tg, 'call', side_effect=ApiError(429, retry_after=7)), \
                patch('chekino.api.time.sleep') as sleep:
            tg.poll(42)
        sleep.assert_called_once_with(7)

    def test_telegram_ipv4_connection_keeps_failover_and_closes_failed_socket(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.2', 443))]
        from unittest.mock import MagicMock
        first, second = MagicMock(), MagicMock()
        first.connect.side_effect = OSError()
        with patch('chekino.api.socket.getaddrinfo', return_value=addresses) as resolve, \
                patch('chekino.api.socket.socket', side_effect=[first, second]):
            self.assertIs(ipv4_connection(('api.telegram.org', 443), 3), second)
        resolve.assert_called_once_with('api.telegram.org', 443, socket.AF_INET, socket.SOCK_STREAM)
        first.close.assert_called_once()
        second.settimeout.assert_called_once_with(3)
        second.close.assert_not_called()

    def test_provider_paths_and_normalization(self):
        with patch('urllib.request.urlopen', return_value=Response({'items': [
                {'kinopoiskId': 7, 'nameRu': 'Имя', 'type': 'FILM', 'year': 2020,
                 'genres': [{'genre': 'драма'}]}]})) as http:
            result = Kinopoisk('placeholder').search('Имя')
            self.assertEqual(result[0]['id'], 7)
            self.assertEqual(result[0]['genres'], ['драма'])
            req = http.call_args.args[0]
            self.assertTrue(req.full_url.startswith('https://kinopoiskapiunofficial.tech/api/v2.2/films?'))
            self.assertIn('keyword=', req.full_url)
            self.assertEqual(req.get_header('X-api-key'), 'placeholder')
        with patch('chekino.api.Kinopoisk.get', return_value={'kinopoiskId': 7}) as api:
            Kinopoisk('placeholder').search('https://kinopoisk.ru/film/7/')
            api.assert_called_once_with('/7')

    def test_telegram_payload_and_redacted_errors(self):
        with patch('chekino.api.TELEGRAM_OPENER.open', return_value=Response({'ok': True, 'result': []})) as http:
            self.assertEqual(Telegram('placeholder').call('getUpdates', offset=2), [])
            self.assertEqual(json.loads(http.call_args.args[0].data), {'offset': 2})
        body = io.BytesIO(b'{"description":"Too Many Requests", "parameters":{"retry_after":7}}')
        failure = urllib.error.HTTPError('https://secret.invalid/token', 429, 'error', {}, body)
        with patch('urllib.request.urlopen', side_effect=failure):
            with self.assertRaises(ApiError) as caught:
                request('https://secret.invalid/token')
            self.assertEqual(caught.exception.retry_after, 7)
            self.assertNotIn('secret', str(caught.exception))

    def test_recommendations_exclude_catalog_and_use_both_preferences(self):
        raw = lambda mid, genre: {'kinopoiskId': mid, 'type': 'FILM', 'genres': [{'genre': genre}]}
        def fake_get(path, **params):
            if path:
                return raw(int(path[1:]), 'драма')
            return {'items': [raw(1, 'драма'), raw(2, 'драма'), raw(3, 'ужасы'),
                             dict(raw(4, 'драма'), type='TV_SHOW')]}
        with patch('chekino.api.Kinopoisk.get', side_effect=fake_get):
            result = Kinopoisk('placeholder').recommend({1}, {11: {'драма': 2}, 22: {'ужасы': -2}})
            self.assertEqual(result['id'], 2)

    def test_recommendations_respect_actual_page_count(self):
        pages = []
        def get(path, **params):
            if path:
                return {'kinopoiskId': int(path[1:]), 'type': 'FILM'}
            pages.append(params['page'])
            self.assertLessEqual(params['page'], 2)
            return {'totalPages': 2, 'items': [
                {'kinopoiskId': params['page'], 'type': 'FILM'}]}
        with patch('chekino.api.Kinopoisk.get', side_effect=get):
            result = Kinopoisk('placeholder').recommend({1}, {})
        self.assertEqual(result['id'], 2)
        self.assertEqual(pages, [1, 2])

    def test_recommendations_check_remaining_pages_after_exclusions(self):
        pages = []
        def get(path, **params):
            if path:
                return {'kinopoiskId': int(path[1:]), 'type': 'FILM'}
            page = params['page']
            pages.append(page)
            return {'totalPages': 4, 'items': [{'kinopoiskId': page, 'type': 'FILM'}]}
        with patch('chekino.api.Kinopoisk.get', side_effect=get), \
                patch('chekino.api.random.shuffle'):
            result = Kinopoisk('placeholder').recommend({1, 2, 3}, {})
        self.assertEqual(result['id'], 4)
        self.assertEqual(pages, [1, 2, 3, 4])

    def test_two_connections_simultaneous_add_and_rate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'concurrent.sqlite'
            initial = Store(path)
            initial.db.close()
            barrier = threading.Barrier(2)
            failures = []
            def worker(uid, score):
                store = Store(path)
                try:
                    barrier.wait(timeout=5)
                    with store.transaction():
                        store.cache({'id': 1, 'title': 'Фильм'})
                        store.add(1)
                        store.rate(1, uid, score)
                except BaseException as exc:
                    failures.append(type(exc).__name__)
                finally:
                    store.db.close()
            threads = [threading.Thread(target=worker, args=(11, 5)),
                       threading.Thread(target=worker, args=(22, 2))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
                self.assertFalse(t.is_alive())
            self.assertEqual(failures, [])
            final = Store(path)
            try:
                self.assertEqual(final.catalog(), {1})
                self.assertEqual(final.votes(1), {11: 5, 22: 2})
                self.assertEqual(final.ids('queue', 11), [])
            finally:
                final.db.close()
