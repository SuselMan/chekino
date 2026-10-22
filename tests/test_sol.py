import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from chekino.app import App
from chekino.sol import MODEL, SolClient, SolRecommender, RecommendationError, RecommendationPending
from chekino.store import Store
from test_app import movie, Telegram


def suggestion(mid):
    return {'title': f'Фильм {mid}', 'original_title': f'Film {mid}', 'year': 2000,
            'why': 'Вам может подойти необычная структура сюжета. Здесь есть мягкий абсурд без спойлеров.'}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.failure = False

    def generate(self, profile, avoid):
        self.calls.append((profile, avoid))
        if self.gate:
            self.gate.wait(timeout=5)
        if self.failure:
            raise RecommendationError('Агент временно недоступен.')
        return [suggestion(2), suggestion(3)]


class Metadata:
    def get(self, path, **params):
        mid = int(params['keyword'].split()[-1])
        return {'items': [{'kinopoiskId': mid, 'nameRu': f'Фильм {mid}',
                           'nameOriginal': f'Film {mid}', 'year': 2000, 'type': 'FILM'}]}

    def detail(self, mid):
        return movie(mid)


class SolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.s = Store(':memory:')
        self.s.cache(movie(1))
        self.s.rate(1, 111222333, 5)
        self.client, self.metadata, self.tg = FakeClient(), Metadata(), Telegram()
        self.path = Path(self.directory.name) / 'sol.sqlite'
        self.rec = SolRecommender(self.s, self.metadata, self.client, (111222333, 444555666), self.path)
        self.app = App(self.s, self.metadata, self.tg, (111222333, 444555666), recommender=self.rec)

    def tearDown(self):
        if self.client.gate:
            self.client.gate.set()
        self.rec.executor.shutdown(wait=True)
        self.rec.db.close()
        self.s.db.close()
        self.directory.cleanup()

    def test_context_has_separate_anonymous_scores_and_no_ids(self):
        self.s.rate(1, 444555666, 2)
        _, profile, avoid = self.rec.context()
        self.assertEqual(profile[0]['ratings'], {'A': 5, 'B': 2})
        encoded = json.dumps([profile, avoid])
        self.assertNotIn('111222333', encoded)
        self.assertNotIn('444555666', encoded)
        self.assertEqual(avoid, [{'title': 'Фильм 1', 'year': 2000}])

    def test_async_generation_and_cached_next_pick(self):
        self.client.gate = threading.Event()
        with self.assertRaises(RecommendationPending):
            self.rec.recommend({1})
        self.assertTrue(self.rec.pending())
        with self.assertRaises(RecommendationPending):
            self.rec.recommend({1})
        self.client.gate.set()
        self.rec.future.result(timeout=2)
        first = self.rec.recommend({1})
        second = self.rec.recommend({1, 2})
        self.assertEqual((first['id'], second['id']), (2, 3))
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(first['recommendation_model'], MODEL)
        self.assertTrue(first['why'])
        self.assertEqual(self.s.catalog(), {1}, 'recommendations do not add to the shared queue')

    def test_batch_survives_restart_without_generating_again(self):
        with self.assertRaises(RecommendationPending):
            self.rec.recommend({1})
        self.rec.future.result(timeout=2)
        restarted = SolRecommender(self.s, self.metadata, self.client, self.rec.users, self.path)
        try:
            self.assertEqual(restarted.recommend({1})['id'], 2)
            self.assertEqual(len(self.client.calls), 1)
        finally:
            restarted.executor.shutdown(wait=True)
            restarted.db.close()

    def test_changed_rating_invalidates_batch_but_queue_addition_does_not(self):
        before = self.rec.context()[0]
        self.s.cache(movie(4))
        self.s.add(4)
        self.assertEqual(self.rec.context()[0], before)
        self.s.rate(1, 111222333, 2)
        self.assertNotEqual(self.rec.context()[0], before)

    def test_pending_card_refreshes_and_escapes_reason_and_keeps_it_after_add(self):
        state = self.app.recommend(111222333, {})
        self.s.set_state(111222333, state)
        self.assertTrue(state['ai_pending'])
        self.rec.future.result(timeout=2)
        self.app.refresh_recommendations()
        state = self.s.user(111222333)['state']
        self.assertEqual(state['mid'], 2)
        payload = self.app.render(111222333)
        self.assertIn('Почему вам может понравиться', payload['text'])
        self.s.cache(movie(2, why='<b>Причина</b>'))
        self.assertIn('&lt;b&gt;Причина&lt;/b&gt;', self.app.render(111222333)['text'])
        self.s.add(2)
        self.s.cache(movie(2))
        self.assertEqual(self.s.movie(2)['why'], '<b>Причина</b>')

    def test_worker_failure_becomes_card_error_without_automatic_retry(self):
        self.client.failure = True
        self.s.set_state(111222333, self.app.recommend(111222333, {}))
        try:
            self.rec.future.result(timeout=2)
        except RecommendationError:
            pass
        self.app.refresh_recommendations()
        state = self.s.user(111222333)['state']
        self.assertNotIn('ai_pending', state)
        self.assertIn('недоступен', state['notice'])
        self.app.refresh_recommendations()
        self.assertEqual(len(self.client.calls), 1)

    def test_ambiguous_metadata_not_silently_selected(self):
        raw = {'kinopoiskId': 10, 'nameRu': 'Фильм 2', 'year': 2000, 'type': 'FILM'}
        with patch.object(self.metadata, 'get', return_value={'items': [raw, raw | {'kinopoiskId': 11}]}):
            self.assertIsNone(self.rec.resolve(suggestion(2)))

    def seed_reserve(self, count=60):
        profile = self.rec.context()[0]
        for mid in range(100, 100 + count):
            data = movie(mid, why='Первая причина. Вторая причина.')
            self.rec.db.execute('INSERT INTO reserve_pool VALUES(?,?,?,?)',
                                (mid, profile, json.dumps(data), mid))

    def test_five_ready_cards_need_no_model_or_metadata_calls(self):
        self.seed_reserve()
        with patch.object(self.metadata, 'get', side_effect=AssertionError('network')), \
                patch.object(self.metadata, 'detail', side_effect=AssertionError('network')):
            state = self.app.recommend(111222333, {})
        self.assertEqual(len(state['rec_batch']), 5)
        self.assertEqual(len(self.rec.available()), 59)  # First displayed item is marked seen.
        self.s.set_state(111222333, state)
        self.assertEqual(len(self.rec.available()), 55)  # Remaining four are reserved to this chat.
        self.assertEqual(len(self.client.calls), 0)
        self.assertEqual(self.s.catalog(), {1})
        self.assertEqual(self.app.visible_ids(111222333, state), state['rec_batch'])

    def test_arrows_extend_recommendations_and_keep_back_history(self):
        self.seed_reserve()
        uid = 111222333
        state = self.app.recommend(uid, {})
        first = state['mid']
        self.s.set_state(uid, state)
        self.assertNotIn('Следующие 5', str(self.app.render(uid)))
        self.assertIn('1 / 60', self.app.render(uid)['text'])
        for _ in range(5):
            state = self.app.action(uid, state, 'page', '1')
            self.s.set_state(uid, state)
        self.assertEqual(len(state['rec_batch']), 10)
        self.assertIn('6 / 60', self.app.render(uid)['text'])
        self.assertEqual(state['mid'], state['rec_batch'][5])
        self.assertEqual(len(set(state['rec_batch'])), 10)
        for _ in range(6):
            state = self.app.action(uid, state, 'page', '-1')
            self.s.set_state(uid, state)
        self.assertEqual(state['mid'], first)
        self.assertEqual(len(self.client.calls), 0)

    def test_pending_extension_preserves_history_and_back_cancels_auto_jump(self):
        from chekino.sol import RecommendationPending
        self.seed_reserve()
        uid = 111222333
        state = self.app.recommend(uid, {})
        state['mid'] = state['rec_batch'][-1]
        self.s.set_state(uid, state)
        with patch.object(self.rec, 'recommend_many', side_effect=RecommendationPending):
            pending = self.app.action(uid, state, 'page', '1')
        self.assertEqual(pending['rec_batch'], state['rec_batch'])
        self.assertEqual(pending['mid'], state['mid'])
        self.assertTrue(pending['ai_pending'])
        back = self.app.action(uid, pending, 'page', '-1')
        self.assertFalse(back.get('ai_pending'))
        self.s.set_state(uid, pending)
        self.app.refresh_recommendations()
        complete = self.s.user(uid)['state']
        self.assertEqual(len(complete['rec_batch']), 10)
        self.assertEqual(complete['mid'], complete['rec_batch'][5])
        self.s.set_state(uid, pending)
        with patch.object(self.rec, 'recommend_many', side_effect=RecommendationError('retry')):
            self.app.refresh_recommendations()
        failed = self.s.user(uid)['state']
        self.assertEqual(failed['rec_batch'], pending['rec_batch'])
        self.assertFalse(failed.get('ai_pending'))

    def test_not_interested_is_permanent_feedback_not_a_rating(self):
        self.seed_reserve()
        state = self.app.recommend(111222333, {})
        self.s.set_state(111222333, state)
        mid = state['mid']
        before_votes = dict(self.s.votes(1))
        following = self.app.action(111222333, state, 'reject')
        self.s.set_state(111222333, following)
        self.assertIn(mid, self.s.rejected())
        self.assertNotIn(mid, self.s.catalog())
        self.assertEqual(self.s.votes(mid), {})
        self.assertNotIn(mid, self.s.watched())
        self.assertEqual(self.s.votes(1), before_votes)
        self.assertNotIn(mid, [m['id'] for m in self.rec.available()])
        _, profile, avoid = self.rec.context()
        feedback = [e for e in profile if e.get('not_interested_by')]
        self.assertEqual(feedback[0]['not_interested_by'], ['A'])
        self.assertEqual(feedback[0]['title'], movie(mid)['title'])
        self.assertIn({'title': movie(mid)['title'], 'year': 2000}, avoid)
        self.assertNotIn(str(111222333), json.dumps(profile))

    def test_reject_advances_to_unseen_card_instead_of_repeating_first(self):
        self.seed_reserve()
        state = self.app.recommend(111222333, {})
        self.s.set_state(111222333, state)
        second = self.app.action(111222333, state, 'page', '1')
        self.s.set_state(111222333, second)
        following = self.app.action(111222333, second, 'reject')
        self.assertEqual(following['mid'], state['rec_batch'][2])
        self.assertIn(state['mid'], following['rec_batch'])
        self.assertNotIn(second['mid'], following['rec_batch'])

    def test_consumed_batch_never_renders_empty_telegram_text(self):
        self.seed_reserve()
        state = self.app.recommend(111222333, {})
        self.s.set_state(111222333, state)
        for mid in state['rec_batch']:
            self.s.add(mid)
        self.assertTrue(self.app.render(111222333)['text'].strip())

    def test_buffer_refills_below_thirty_and_survives_restart(self):
        self.seed_reserve(50)
        self.rec.maintain()
        self.assertIsNone(self.rec.future)
        self.assertFalse(self.rec.warming)
        excluded = set(range(100, 121))
        for mid in excluded:
            self.s.record_recommendation(111222333, mid)
        self.assertEqual(len(self.rec.available()), 29)
        self.rec.maintain()
        self.assertIsNotNone(self.rec.future)
        self.rec.future.result(timeout=2)
        restarted = SolRecommender(self.s, self.metadata, self.client, self.rec.users, self.path)
        try:
            self.assertGreaterEqual(len(restarted.available()), 29)
            offered = restarted.recommend_many(set(), 5)
            self.assertFalse({m['id'] for m in offered} & excluded)
        finally:
            restarted.executor.shutdown(wait=True)
            restarted.db.close()

    def test_codex_client_pins_sol_and_only_parses_final_message(self):
        events = '\n'.join(json.dumps(e) for e in [
            {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'not a result'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message',
             'text': json.dumps({'recommendations': [suggestion(2)]})}}])
        completed = subprocess.CompletedProcess([], 0, events, '')
        with patch('chekino.sol.subprocess.run', return_value=completed) as runner:
            result = SolClient(self.directory.name).generate([], [])
        self.assertEqual(result[0]['title'], 'Фильм 2')
        args, kwargs = runner.call_args
        command = args[0]
        self.assertIn(MODEL, command)
        self.assertIn('--ephemeral', command)
        self.assertIn('read-only', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertIn('taste_history', kwargs['input'])
        self.assertNotIn('taste_history', ' '.join(command), 'private prompt belongs on stdin')

    def test_client_errors_do_not_echo_private_output(self):
        failed = subprocess.CompletedProcess([], 1, 'private ratings', 'private secret')
        with patch('chekino.sol.subprocess.run', return_value=failed):
            with self.assertRaises(RecommendationError) as exc:
                SolClient(self.directory.name).generate([], [])
        self.assertNotIn('private', str(exc.exception))
