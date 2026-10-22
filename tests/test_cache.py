from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from chekino.api import ApiError, Kinopoisk
from chekino.cache import MetadataCache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = MetadataCache(':memory:')
        self.p = Kinopoisk('placeholder', self.cache)

    def tearDown(self):
        self.cache.db.close()

    def test_search_and_detail_repeat_without_network(self):
        with patch('chekino.api.request', return_value={'items': []}) as http:
            self.p.search('Дюна')
            self.p.search('Дюна')
            self.assertEqual(http.call_count, 1)
        with patch('chekino.api.request', return_value={'kinopoiskId': 1, 'type': 'FILM'}) as http:
            self.p.detail(1)
            self.p.detail(1)
            self.assertEqual(http.call_count, 1)

    def test_recommendation_pages_cached_and_param_order_canonical(self):
        with patch('chekino.api.request', return_value={'items': []}) as http:
            self.p.get('', page=2, order='RATING')
            self.p.get('', order='RATING', page=2)
            self.assertEqual(http.call_count, 1)

    def test_entries_have_no_expiration(self):
        with patch('chekino.cache.time.time', return_value=1000), \
                patch('chekino.api.request', return_value={'kinopoiskId': 1}):
            self.p.get('', keyword='Название')
            self.p.detail(1)
        with patch('chekino.cache.time.time', return_value=10**12), \
                patch('chekino.api.request') as http:
            self.p.get('', keyword='Название')
            self.p.detail(1)
            http.assert_not_called()

    def test_cached_metadata_works_without_api_even_if_budget_exhausted(self):
        self.cache.budget = 0
        self.cache.set('?keyword=test', {'items': [1]})
        with patch('chekino.api.request') as http:
            self.assertEqual(self.p.get('', keyword='test'), {'items': [1]})
            http.assert_not_called()

    def test_watched_first_then_lru_and_physical_size_bound(self):
        cache = MetadataCache(':memory:', max_bytes=128 * 1024, watched=lambda: {2})
        try:
            for mid in range(1, 5):
                with patch('chekino.cache.time.time', return_value=mid * 100):
                    cache.set('/' + str(mid), {'kinopoiskId': mid, 'description': 'x' * 24000})
            self.assertIsNone(cache.get('/2'), 'watched metadata must be evicted first')
            self.assertIsNotNone(cache.get('/1'), 'older unwatched metadata is retained before watched')
            for mid in range(5, 20):
                cache.set('/' + str(mid), {'kinopoiskId': mid, 'description': 'x' * 24000})
                self.assertLessEqual(cache.size(), 128 * 1024)
            self.assertIsNone(cache.get('/1'), 'remaining entries evict by least recent access')
        finally:
            cache.db.close()

    def test_lru_reads_preserve_recently_used_entry_and_ledger(self):
        cache = MetadataCache(':memory:', max_bytes=128 * 1024)
        try:
            cache.reserve()
            for mid in range(1, 4):
                with patch('chekino.cache.time.time', return_value=mid * 100):
                    cache.set('/' + str(mid), {'kinopoiskId': mid, 'description': 'x' * 24000})
            with patch('chekino.cache.time.time', return_value=350):
                self.assertIsNotNone(cache.get('/1'))
            with patch('chekino.cache.time.time', return_value=400):
                cache.set('/4', {'kinopoiskId': 4, 'description': 'x' * 24000})
            self.assertIsNone(cache.get('/2'))
            self.assertIsNotNone(cache.get('/1'))
            self.assertEqual(cache.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
        finally:
            cache.db.close()

    def test_large_response_is_not_cached(self):
        cache = MetadataCache(':memory:', max_bytes=65536)
        try:
            cache.set('/1', {'kinopoiskId': 1, 'description': 'x' * 100000})
            self.assertIsNone(cache.get('/1'))
            self.assertLessEqual(cache.size(), 65536)
        finally:
            cache.db.close()

    def test_budget_counts_failures_and_blocks_before_network(self):
        self.cache.budget = 2
        with patch('chekino.api.request', side_effect=ApiError(503)) as http:
            for keyword in ('a', 'b', 'c'):
                with self.assertRaises(ApiError):
                    self.p.get('', keyword=keyword)
            self.assertEqual(http.call_count, 2)
        with patch('chekino.cache.time.time', return_value=10**12):
            self.cache.reserve()  # Expired rolling-window attempts release budget.

    def test_old_cache_migration_keeps_payload_and_budget(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'old.sqlite'
            old = sqlite3.connect(path)
            old.executescript("""CREATE TABLE responses(key TEXT PRIMARY KEY,data TEXT,fetched REAL);
              CREATE TABLE requests(at REAL); INSERT INTO responses VALUES('/1?',
              '{"kinopoiskId":1}',1); INSERT INTO requests VALUES(1);""")
            old.commit()
            old.close()
            cache = MetadataCache(path, max_bytes=128 * 1024)
            try:
                self.assertEqual(cache.get('/1?'), {'kinopoiskId': 1})
                self.assertEqual(cache.db.execute('SELECT movie_ids FROM responses').fetchone()[0], '[1]')
                self.assertEqual(cache.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
                for mid in range(2, 20):
                    cache.set('/' + str(mid), {'kinopoiskId': mid, 'description': 'x' * 24000})
                    self.assertLessEqual(path.stat().st_size, 128 * 1024)
            finally:
                cache.db.close()

    def test_disk_cache_survives_restart(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'cache.sqlite'
            first = MetadataCache(path)
            first.set('/1?', {'kinopoiskId': 1})
            first.reserve()
            first.db.close()
            second = MetadataCache(path, budget=1)
            try:
                with patch('chekino.api.request') as http:
                    self.assertEqual(Kinopoisk('placeholder', second).detail(1)['id'], 1)
                    http.assert_not_called()
                with self.assertRaises(ApiError):
                    second.reserve()
            finally:
                second.db.close()
