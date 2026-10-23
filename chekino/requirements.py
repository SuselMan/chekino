"""Private, reloadable owner preferences; never part of the public repository."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Requirements:
    min_year: int = 1880
    excluded_countries: tuple = ()
    instructions: str = ''
    error: str = ''

    def payload(self):
        return {'min_year': self.min_year, 'excluded_countries': self.excluded_countries,
                'instructions': self.instructions}

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.payload(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def allows(self, movie):
        year = movie.get('year')
        if self.error or type(year) is not int or year < self.min_year:
            return False
        countries = movie.get('countries', [])
        if self.excluded_countries and not countries:
            return False  # Unknown origin cannot prove compliance with a country exclusion.
        def canonical(country):
            name = country.strip().casefold()
            return 'ссср' if name in {'ussr', 'soviet union', 'советский союз'} else name
        return not {canonical(c) for c in countries} & {canonical(c) for c in self.excluded_countries}


def load_requirements(path=None):
    if path is None:
        return Requirements()
    try:
        content = Path(path).read_bytes()
        if len(content) > 32768:
            raise ValueError()
        raw = tomllib.loads(content.decode())
        if set(raw) - {'min_year', 'excluded_countries', 'instructions'}:
            raise ValueError()
        year, countries, instructions = raw.get('min_year', 1880), raw.get('excluded_countries', []), raw.get('instructions', '')
        if (type(year) is not int or not 1880 <= year <= 2100 or not isinstance(countries, list)
                or any(not isinstance(c, str) or not c.strip() for c in countries)
                or not isinstance(instructions, str)):
            raise ValueError()
        return Requirements(year, tuple(countries), instructions)
    except (OSError, ValueError, UnicodeError):
        return Requirements(error='Не удалось прочитать требования Sol. Проверь приватный файл настроек.')
