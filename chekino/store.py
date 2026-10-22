"""SQLite is the source of truth; Telegram is a retriable projection."""
import json
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS movies (
          id INTEGER PRIMARY KEY, data TEXT NOT NULL, added INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS ratings (
          movie INTEGER REFERENCES movies(id), user INTEGER NOT NULL,
          value INTEGER NOT NULL CHECK(value BETWEEN 1 AND 5),
          PRIMARY KEY(movie,user));
        CREATE TABLE IF NOT EXISTS rating_history (
          movie INTEGER NOT NULL, user INTEGER NOT NULL, value INTEGER NOT NULL,
          changed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, state TEXT NOT NULL, message INTEGER,
          dirty INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS callbacks (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS recommendations (
          user INTEGER NOT NULL, movie INTEGER NOT NULL,
          shown INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE IF NOT EXISTS deletions (
          chat INTEGER NOT NULL, message INTEGER NOT NULL, PRIMARY KEY(chat,message));
        PRAGMA user_version=1;
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def user(self, uid):
        row = self.db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        return dict(row) | {'state': json.loads(row['state'])} if row else None

    def set_state(self, uid, state):
        self.db.execute('''INSERT INTO users(id,state) VALUES(?,?)
          ON CONFLICT(id) DO UPDATE SET state=excluded.state,dirty=1,revision=revision+1''',
                        (uid, json.dumps(state, ensure_ascii=False)))

    def cache(self, movie):
        previous = self.movie(movie['id'])
        if previous and previous.get('why') and not movie.get('why'):
            movie = {**movie, 'why': previous['why']}
        self.db.execute('''INSERT INTO movies(id,data) VALUES(?,?)
          ON CONFLICT(id) DO UPDATE SET data=excluded.data''',
                        (movie['id'], json.dumps(movie, ensure_ascii=False)))

    def prune_transient(self):
        # Search candidates/current recommendations are temporary, never user catalog data.
        keep = set()
        for row in self.db.execute('SELECT state FROM users'):
            state = json.loads(row[0])
            if state.get('mid'):
                keep.add(state['mid'])
            keep.update(state.get('choices', []))
        for row in self.db.execute('SELECT id FROM movies WHERE added=0').fetchall():
            if row[0] not in keep:
                self.db.execute('DELETE FROM movies WHERE id=? AND added=0', (row[0],))

    def movie(self, mid):
        row = self.db.execute('SELECT data FROM movies WHERE id=?', (mid,)).fetchone()
        return json.loads(row[0]) if row else None

    def add(self, mid):
        self.db.execute('UPDATE movies SET added=1 WHERE id=?', (mid,))
        self.invalidate()

    def rate(self, mid, uid, value):
        if type(value) is not int or not 1 <= value <= 5:
            raise ValueError('rating must be 1–5')
        self.add(mid)
        self.db.execute('''INSERT INTO ratings VALUES(?,?,?)
          ON CONFLICT(movie,user) DO UPDATE SET value=excluded.value''', (mid, uid, value))
        self.db.execute('INSERT INTO rating_history(movie,user,value) VALUES(?,?,?)',
                        (mid, uid, value))

    def invalidate(self):
        self.db.execute('UPDATE users SET dirty=1,revision=revision+1')

    def ids(self, mode, uid):
        if mode == 'rate':
            query = '''SELECT id FROM movies WHERE added=1
              AND EXISTS(SELECT 1 FROM ratings WHERE movie=id)
              AND NOT EXISTS(SELECT 1 FROM ratings WHERE movie=id AND user=?) ORDER BY id'''
            return [r[0] for r in self.db.execute(query, (uid,))]
        return [r[0] for r in self.db.execute('''SELECT id FROM movies WHERE added=1
          AND NOT EXISTS(SELECT 1 FROM ratings WHERE movie=id) ORDER BY id''')]

    def votes(self, mid):
        return dict(self.db.execute('SELECT user,value FROM ratings WHERE movie=?', (mid,)))

    def catalog(self):
        return {r[0] for r in self.db.execute('SELECT id FROM movies WHERE added=1')}

    def watched(self):
        return {r[0] for r in self.db.execute('SELECT DISTINCT movie FROM ratings')}

    def preferences(self, users):
        """Average genre affinity per person, then per couple (equal influence)."""
        result = {}
        for uid in users:
            totals = {}
            for row in self.db.execute('''SELECT data,value FROM ratings
              JOIN movies ON movie=movies.id WHERE user=?''', (uid,)):
                for genre in json.loads(row[0]).get('genres', []):
                    totals.setdefault(genre, []).append(row[1] - 3)
            result[uid] = {g: sum(v) / len(v) for g, v in totals.items()}
        return result

    def recently_shown(self, uid):
        return {r[0] for r in self.db.execute(
            'SELECT movie FROM recommendations WHERE user=? ORDER BY shown DESC LIMIT 200', (uid,))}

    def record_recommendation(self, uid, mid):
        self.db.execute('INSERT INTO recommendations(user,movie) VALUES(?,?)', (uid, mid))
        self.db.execute("""DELETE FROM recommendations WHERE user=? AND shown NOT IN
          (SELECT shown FROM recommendations WHERE user=? ORDER BY shown DESC LIMIT 200)""", (uid, uid))

    def offset(self):
        return self.db.execute('SELECT COALESCE(MAX(id)+1,0) FROM updates').fetchone()[0]

    def backup(self, path):
        with sqlite3.connect(path) as dest:
            self.db.backup(dest)
