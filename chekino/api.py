import json
import http.client
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, code=0, description='API temporarily unavailable', retry_after=0):
        super().__init__('API request failed')  # Never include URLs containing credentials.
        self.code, self.description, self.retry_after = code, description, retry_after


def ipv4_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    last_error = None
    for family, kind, proto, _, endpoint in socket.getaddrinfo(
            address[0], address[1], socket.AF_INET, socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(endpoint)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError('No IPv4 address')


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = ipv4_connection


def request(url, payload=None, headers=None, timeout=40):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (ValueError, UnicodeError):
            body = {}
        raise ApiError(exc.code, body.get('description', ''),
                       body.get('parameters', {}).get('retry_after', 0)) from None
    except (OSError, ValueError):
        raise ApiError() from None


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.connections = threading.local()

    def request(self, method, payload, timeout):
        connection = getattr(self.connections, 'current', None)
        if connection is None:
            connection = IPv4HTTPSConnection('api.telegram.org', timeout=timeout)
            self.connections.current = connection
        connection.timeout = timeout
        if connection.sock:
            connection.sock.settimeout(timeout)
        phase = 'send'
        try:
            path = urllib.parse.urlsplit(self.base + method).path
            if method == 'getUpdates':
                query = {key: json.dumps(value) if isinstance(value, (list, dict)) else value
                         for key, value in payload.items()}
                connection.request('GET', path + '?' + urllib.parse.urlencode(query))
            else:
                connection.request('POST', path, body=json.dumps(payload).encode(),
                                   headers={'Content-Type': 'application/json'})
            phase = 'headers'
            response = connection.getresponse()
            phase = 'body'
            body = json.loads(response.read())
            if response.status >= 400:
                raise ApiError(response.status, body.get('description', ''),
                               body.get('parameters', {}).get('retry_after', 0))
            return body
        except (OSError, ValueError, http.client.HTTPException) as exc:
            print(f'Telegram transport {method}: {phase}, {type(exc).__name__}', flush=True)
            connection.close()
            self.connections.current = None
            raise ApiError() from None

    def call(self, method, **payload):
        started = time.monotonic()
        try:
            body = self.request(method, payload,
                                timeout=3 if method == 'getUpdates' else
                                (2 if method == 'answerCallbackQuery' else 8))
        finally:
            elapsed = time.monotonic() - started
            if method != 'getUpdates' or elapsed > payload.get('timeout', 0) + 2:
                print(f'Telegram {method}: {elapsed:.3f}s', flush=True)
        if not body.get('ok'):
            raise ApiError(body.get('error_code', 0), body.get('description', ''),
                           body.get('parameters', {}).get('retry_after', 0))
        return body['result']

    def poll(self, offset):
        # Long polling on this VPS intermittently held incoming buttons for 40s.
        try:
            updates = self.call('getUpdates', offset=offset, timeout=1,
                                allowed_updates=['message', 'callback_query'])
        except ApiError as exc:
            print(f'Получение обновлений недоступно: код={exc.code}; повтор.', flush=True)
            time.sleep(min(60, max(0.5, exc.retry_after)))
            return []
        if not updates:
            time.sleep(1.0)
        return updates


class CallbackAcker:
    """Best-effort spinner dismissal must never delay domain changes or card delivery."""
    def __init__(self, telegram):
        self.telegram = telegram
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='chekino-ack')
        self.future = None

    def submit(self, callback_id):
        # Do not accumulate obsolete acknowledgements during network trouble.
        if self.future is not None and not self.future.done():
            return False
        self.future = self.executor.submit(self.answer, callback_id)
        return True

    def answer(self, callback_id):
        try:
            self.telegram.call('answerCallbackQuery', callback_query_id=callback_id)
        except ApiError:
            print('Подтверждение кнопки недоступно; карточка обрабатывается независимо.', flush=True)


def canonical_id(text):
    value = text.strip()
    if not re.match(r'https?://', value, re.I):
        return None
    url = urllib.parse.urlsplit(value)
    if url.hostname not in {'kinopoisk.ru', 'www.kinopoisk.ru', 'm.kinopoisk.ru'}:
        raise ValueError('Пришли название или ссылку kinopoisk.ru/film/… либо /series/…')
    match = re.fullmatch(r'/(?:film|series)/(\d+)/?', url.path)
    if not match or not 0 < int(match[1]) < 2**63:
        raise ValueError('Не удалось прочитать ссылку Кинопоиска. Пришли название.')
    return int(match[1])


def external_rating(value):
    return value if type(value) in (int, float) and 0 <= value <= 10 else None


def normalize(raw):
    mid = raw.get('kinopoiskId') or raw.get('filmId')
    if not isinstance(mid, int) or mid <= 0:
        raise ApiError()
    poster = raw.get('posterUrlPreview') or raw.get('posterUrl') or ''
    if not poster.startswith('https://'):
        poster = ''
    return {'id': mid, 'title': raw.get('nameRu') or raw.get('nameOriginal')
            or raw.get('nameEn') or 'Без названия', 'year': raw.get('year'),
            'countries': [c['country'] for c in raw.get('countries', []) if isinstance(c.get('country'), str)],
            'rating_kp': external_rating(raw.get('ratingKinopoisk')),
            'rating_imdb': external_rating(raw.get('ratingImdb')),
            'kind': raw.get('type', 'UNKNOWN'), 'description': raw.get('description') or '',
            'poster': poster, 'genres': [g['genre'] for g in raw.get('genres', []) if g.get('genre')]}


class Kinopoisk:
    """Fixed trusted API origin; user links never become HTTP request targets."""
    def __init__(self, key, cache=None):
        self.key, self.cache = key, cache

    def fork(self, watched):
        # Called inside the worker: every SQLite connection belongs to its own thread.
        from .cache import MetadataCache
        cache = MetadataCache(self.cache.path, budget=self.cache.budget,
                              max_bytes=self.cache.max_bytes, watched=lambda: watched) if self.cache else None
        return Kinopoisk(self.key, cache)

    def get(self, path, **params):
        url = 'https://kinopoiskapiunofficial.tech/api/v2.2/films' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        cache_key = path + '?' + urllib.parse.urlencode(sorted(params.items()))
        if self.cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit
            self.cache.reserve()
        result = request(url, headers={'X-API-KEY': self.key})
        if self.cache:
            self.cache.set(cache_key, result)
        return result

    def cached_match(self, names, year):
        if not self.cache:
            return None
        if not hasattr(self, '_cached_names'):
            index = {}
            # Store only small aliases/keys, not copies of the cached descriptions.
            for key, data in self.cache.db.execute("SELECT key,data FROM responses WHERE key GLOB '/[0-9]*'"):
                raw = json.loads(data)
                for field in ('nameRu', 'nameOriginal', 'nameEn'):
                    name = re.sub(r'[^\w]', '', (raw.get(field) or '').casefold().replace('ё', 'е'))
                    if name:
                        index.setdefault((name, str(raw.get('year'))), set()).add(key)
            self._cached_names = index
        keys = set().union(*(self._cached_names.get((name, str(year)), set()) for name in names))
        if len(keys) == 1:
            raw = self.cache.get(next(iter(keys)))
            if raw is not None:
                return normalize(raw)
        return None

    def detail(self, mid):
        return normalize(self.get('/' + str(mid)))

    def search(self, text):
        mid = canonical_id(text)
        if mid:
            return [self.detail(mid)]
        raw = self.get('', keyword=text[:200], page=1)
        # A title can match remakes, series and translations: always offer choices.
        return [normalize(item) for item in raw.get('items', [])][:20]

    def recommend(self, excluded, preferences):
        params = dict(order='RATING', type='ALL', ratingFrom=7,
                      ratingTo=10, yearFrom=1970)
        first = self.get('', **params, page=1)
        # The provider/account can expose fewer pages than the API's general limit.
        total_pages = max(1, min(20, int(first.get('totalPages') or 1)))
        remaining = list(range(2, total_pages + 1))
        random.shuffle(remaining)
        candidates = {}
        for index, page in enumerate([1] + remaining):
            response = first if page == 1 else self.get('', **params, page=page)
            for item in response.get('items', []):
                if (item.get('kinopoiskId') not in excluded
                        and item.get('type') in {'FILM', 'TV_SERIES', 'MINI_SERIES'}):
                    movie = normalize(item)
                    candidates[movie['id']] = movie
            # Normally sample three real pages; continue if exclusions exhausted them.
            if index >= 2 and candidates:
                break
        if not candidates:
            return None
        def score(movie):
            genres = movie['genres']
            affinity = sum(sum(p.get(g, 0) for g in genres) / max(1, len(genres))
                           for p in preferences.values()) / max(1, len(preferences))
            return affinity + random.random() * 0.75
        return self.detail(max(candidates.values(), key=score)['id'])
