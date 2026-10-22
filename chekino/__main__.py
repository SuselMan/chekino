"""Entrypoint. Secrets are read only from an explicit owner-only JSON file."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from .api import ApiError, CallbackAcker, Kinopoisk, Telegram
from .app import App
from .store import Store
from .cache import MetadataCache
from .sol import SolClient, SolRecommender
from .lookup import LunaClient, TitleLookup


def load_config(path):
    path = Path(path).expanduser()
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Конфигурация должна принадлежать текущему пользователю и иметь права 0600.')
    config = json.loads(path.read_text())
    users = config.get('users')
    if not isinstance(users, list) or len(users) != 2 or any(type(x) is not int or x <= 0 for x in users) \
            or len(set(users)) != 2:
        raise ValueError('Нужны ровно два разных положительных Telegram user ID.')
    if not isinstance(config.get('token'), str) or ':' not in config['token']:
        raise ValueError('Отсутствует токен Telegram.')
    if not isinstance(config.get('metadata_key'), str) or not config['metadata_key'].strip():
        raise ValueError('Отсутствует ключ метаданных.')
    if not Path(config.get('database', '')).is_absolute():
        raise ValueError('Путь database должен быть абсолютным.')
    if type(config.get('reveal_partner', False)) is not bool:
        raise ValueError('reveal_partner должен быть boolean.')
    return config


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Chekino')
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check-config')
    sub.add_parser('run')
    backup = sub.add_parser('backup')
    backup.add_argument('destination')
    export = sub.add_parser('export')
    export.add_argument('destination')
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError):
        parser.exit(2, 'Некорректная приватная конфигурация; проверь README. Секреты не выводятся.\n')
    if args.command == 'check-config':
        print('Конфигурация корректна. Сетевые запросы не выполнялись.')
        return
    dbpath = Path(cfg['database'])
    dbpath.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dbpath.exists() and (dbpath.stat().st_uid != os.getuid() or dbpath.stat().st_mode & 0o077):
        parser.exit(2, 'База должна принадлежать текущему пользователю и иметь права 0600.\n')
    store = Store(dbpath)
    if args.command in {'backup', 'export'}:
        destination = Path(args.destination).expanduser()
        # Never overwrite existing files (especially the live database).
        with destination.open('xb'):
            pass
        if args.command == 'backup':
            store.backup(destination)
        else:
            with store.transaction():
                data = {table: [dict(r) for r in store.db.execute('SELECT * FROM ' + table)]
                        for table in ('movies', 'ratings', 'rating_history', 'rejections', 'recommendation_seen')}
            destination.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print('Приватная копия создана.')
        return
    stored_users = {r[0] for r in store.db.execute('SELECT id FROM users UNION SELECT user FROM ratings')}
    if not stored_users.issubset(set(cfg['users'])):
        parser.exit(2, 'Участники конфигурации отличаются от сохранённых. Нужна явная миграция базы.\n')
    with open(str(dbpath) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, 'Уже работает другой экземпляр Chekino.\n')
        tg = Telegram(cfg['token'])
        acknowledgements = CallbackAcker(tg)
        cache = MetadataCache(str(dbpath) + '.metadata.sqlite', watched=store.watched)
        metadata = Kinopoisk(cfg['metadata_key'], cache)
        recommender = SolRecommender(
            store, metadata, SolClient(dbpath.parent / 'agent-work'), cfg['users'],
            str(dbpath) + '.sol.sqlite')
        normalizer = TitleLookup(metadata, LunaClient(dbpath.parent / 'agent-work'),
                                 str(dbpath) + '.sol.sqlite')
        app = App(store, metadata, tg, cfg['users'],
                  cfg.get('reveal_partner', False), recommender=recommender, normalizer=normalizer)
        while True:
            try:
                recommender.maintain()
                app.refresh_recommendations()
                verify_lookup = app.refresh_lookups()
                app.deliver()
                if verify_lookup:
                    app.refresh_lookups()
                    app.deliver()
                waiting = any((store.user(uid) or {}).get('state', {}).get('ai_pending') or
                              (store.user(uid) or {}).get('state', {}).get('lookup_pending')
                              for uid in cfg['users'])
                updates = tg.call('getUpdates', offset=store.offset(), timeout=2 if waiting or recommender.future else 25,
                                  allowed_updates=['message', 'callback_query'])
                for update in updates:
                    started = time.monotonic()
                    cb = update.get('callback_query')
                    if cb:
                        acknowledgements.submit(cb['id'])
                    try:
                        app.handle(update)
                    except ValueError as exc:
                        app.error(update, str(exc))
                    except ApiError:
                        app.error(update, 'Источник метаданных недоступен. Ввод сохранён; повтори позже.')
                    actor = (cb or update.get('message') or {}).get('from', {}).get('id')
                    app.deliver(priority=actor)
                    print(f'Обработка {"кнопки" if cb else "сообщения"}: '
                          f'{time.monotonic() - started:.3f}s', flush=True)
            except ApiError as exc:
                print(f'Временная ошибка API: код={exc.code}, retry_after={exc.retry_after}; '
                      'повтор с сохранённым состоянием.', flush=True)
                time.sleep(min(60, max(3, exc.retry_after)))
            except (sqlite3.Error, OSError):
                # Fail visibly; systemd may restart, durable updates stay in SQLite.
                parser.exit(1, 'Ошибка локального хранилища. Проверь диск и права.\n')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
