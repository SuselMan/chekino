"""Timeless metadata cache, bounded SQLite file, watched-first LRU eviction."""
import json
import math
import sqlite3
import time
from .api import ApiError


class MetadataCache:
    def __init__(self, path, budget=400, max_bytes=1024**3, watched=None):
        if max_bytes < 65536:
            raise ValueError('Cache size must be at least 64 KiB')
        self.path = str(path)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.budget, self.max_bytes = budget, max_bytes
        self.watched = watched or (lambda: set())
        self.db.execute('PRAGMA journal_mode=DELETE')
        if self.db.execute('PRAGMA auto_vacuum').fetchone()[0] != 1:
            self.db.execute('PRAGMA auto_vacuum=FULL')
            self.db.execute('VACUUM')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS responses (
          key TEXT PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL,
          accessed REAL NOT NULL DEFAULT 0, movie_ids TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS requests (at REAL NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(responses)')}
        if 'accessed' not in columns:
            self.db.execute('ALTER TABLE responses ADD COLUMN accessed REAL NOT NULL DEFAULT 0')
            self.db.execute('UPDATE responses SET accessed=fetched')
        if 'movie_ids' not in columns:
            self.db.execute("ALTER TABLE responses ADD COLUMN movie_ids TEXT NOT NULL DEFAULT '[]'")
            for row in self.db.execute('SELECT key,data FROM responses'):
                self.db.execute('UPDATE responses SET movie_ids=? WHERE key=?',
                                (json.dumps(self.movie_ids(json.loads(row[1]))), row[0]))
        self.page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
        self.trim(max_bytes)
        self.db.execute(f'PRAGMA max_page_count={max_bytes // self.page_size}')

    @staticmethod
    def movie_ids(data):
        items = data.get('items', []) if isinstance(data, dict) else []
        if isinstance(data, dict) and ('kinopoiskId' in data or 'filmId' in data):
            items = [data]
        return sorted({item.get('kinopoiskId') or item.get('filmId') for item in items
                       if isinstance(item, dict) and
                       isinstance(item.get('kinopoiskId') or item.get('filmId'), int)})

    def size(self):
        return self.db.execute('PRAGMA page_count').fetchone()[0] * self.page_size

    def trim(self, target):
        if self.size() <= target:
            return
        watched = self.watched()
        # Only keys and small ID lists enter memory, never all cached payloads.
        candidates = []
        for key, ids, accessed in self.db.execute('SELECT key,movie_ids,accessed FROM responses'):
            ids = json.loads(ids)
            priority = 0 if ids and all(mid in watched for mid in ids) else 1
            candidates.append((priority, accessed, key))
        for _, _, key in sorted(candidates):
            self.db.execute('DELETE FROM responses WHERE key=?', (key,))
            # auto_vacuum=FULL returns freed pages instead of retaining a giant file.
            if self.size() <= target:
                break

    def get(self, key):
        row = self.db.execute('SELECT data FROM responses WHERE key=?', (key,)).fetchone()
        if row:
            self.db.execute('UPDATE responses SET accessed=? WHERE key=?', (time.time(), key))
            return json.loads(row[0])
        return None

    def set(self, key, data):
        encoded = json.dumps(data, ensure_ascii=False)
        ids = json.dumps(self.movie_ids(data))
        required = (math.ceil((len(encoded.encode()) + len(key.encode()) + len(ids)) /
                              self.page_size) + 4) * self.page_size
        # Keep room for schema, indexes and the independent rolling request ledger.
        if required > self.max_bytes - 8 * self.page_size:
            return  # A huge response remains usable but is not cached.
        self.trim(self.max_bytes - required)
        for _ in range(2):
            try:
                now = time.time()
                self.db.execute('''INSERT OR REPLACE INTO responses
                  (key,data,fetched,accessed,movie_ids) VALUES(?,?,?,?,?)''',
                                (key, encoded, now, now, ids))
                self.trim(self.max_bytes - 4 * self.page_size)
                return
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_FULL:
                    raise
                self.trim(max(0, self.size() - required))
        # Cache pressure must not discard a successfully fetched movie or input.

    def wait_seconds(self):
        now = time.time()
        count, oldest = self.db.execute(
            'SELECT COUNT(*),MIN(at) FROM requests WHERE at>?', (now - 86400,)).fetchone()
        return max(1, math.ceil(oldest + 86400 - now)) if count >= self.budget else 0

    def reserve(self):
        # Attempts, including failures, count before HTTP; cache eviction never resets them.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            now = time.time()
            self.db.execute('DELETE FROM requests WHERE at<=?', (now - 86400,))
            count = self.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            if count >= self.budget:
                raise ApiError(429, 'Local metadata API budget exhausted', self.wait_seconds())
            self.db.execute('INSERT INTO requests VALUES(?)', (now,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
