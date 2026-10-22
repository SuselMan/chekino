import json
import random
import re
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, code=0, description='API temporarily unavailable', retry_after=0):
        super().__init__('API request failed')  # Never include URLs containing credentials.
        self.code, self.description, self.retry_after = code, description, retry_after


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

    def call(self, method, **payload):
        body = request(self.base + method, payload)
        if not body.get('ok'):
            raise ApiError(body.get('error_code', 0), body.get('description', ''),
                           body.get('parameters', {}).get('retry_after', 0))
        return body['result']


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


def normalize(raw):
    mid = raw.get('kinopoiskId') or raw.get('filmId')
    if not isinstance(mid, int) or mid <= 0:
        raise ApiError()
    poster = raw.get('posterUrlPreview') or raw.get('posterUrl') or ''
    if not poster.startswith('https://'):
        poster = ''
    return {'id': mid, 'title': raw.get('nameRu') or raw.get('nameOriginal')
            or raw.get('nameEn') or 'Без названия', 'year': raw.get('year'),
            'kind': raw.get('type', 'UNKNOWN'), 'description': raw.get('description') or '',
            'poster': poster, 'genres': [g['genre'] for g in raw.get('genres', []) if g.get('genre')]}


class Kinopoisk:
    """Fixed trusted API origin; user links never become HTTP request targets."""
    def __init__(self, key):
        self.key = key

    def get(self, path, **params):
        url = 'https://kinopoiskapiunofficial.tech/api/v2.2/films' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        return request(url, headers={'X-API-KEY': self.key})

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
        candidates = []
        for page in random.sample(range(1, 11), 3):
            items = self.get('', order='RATING', type='ALL', ratingFrom=7,
                             ratingTo=10, yearFrom=1970, page=page).get('items', [])
            candidates.extend(normalize(x) for x in items if x.get('kinopoiskId') not in excluded)
        if not candidates:
            return None
        def score(movie):
            genres = movie['genres']
            affinity = sum(sum(p.get(g, 0) for g in genres) / max(1, len(genres))
                           for p in preferences.values()) / max(1, len(preferences))
            return affinity + random.random() * 0.75
        return self.detail(max(candidates, key=score)['id'])
