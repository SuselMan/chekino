import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from chekino.app import App
from chekino.lookup import LunaClient, TitleLookup
from chekino.sol import RecommendationPending
from chekino.store import Store
from test_app import Telegram


class Client:
    def __init__(self):
        self.calls = 0
        self.result = [{'title': 'Интерстеллар', 'original_title': 'Interstellar', 'year': 2014}]

    def normalize(self, query):
        self.calls += 1
        return self.result


class Metadata:
    def search(self, query):
        return []

    def get(self, path, **params):
        return {'items': [{'kinopoiskId': 258687, 'nameRu': 'Интерстеллар',
                           'nameOriginal': 'Interstellar', 'year': 2014, 'type': 'FILM'},
                          {'kinopoiskId': 1, 'nameRu': 'Совсем другое',
                           'year': 2014, 'type': 'FILM'}]}


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.directory.name) / 'agents.sqlite'
        self.metadata, self.client = Metadata(), Client()
        self.lookup = TitleLookup(self.metadata, self.client, self.path)
        self.s, self.tg = Store(':memory:'), Telegram()
        self.app = App(self.s, self.metadata, self.tg, (11, 22), normalizer=self.lookup)

    def tearDown(self):
        self.lookup.executor.shutdown(wait=True)
        self.lookup.db.close()
        self.s.db.close()
        self.directory.cleanup()

    def message(self):
        return {'update_id': 1, 'message': {'message_id': 10, 'text': 'интерстелар',
                'from': {'id': 11}, 'chat': {'id': 11, 'type': 'private'}}}

    def test_typo_waits_then_offers_verified_choice_without_adding(self):
        self.app.handle(self.message())
        self.app.deliver()
        self.assertTrue(self.s.user(11)['state']['lookup_pending'])
        self.assertFalse(any(method == 'deleteMessage' for method, _ in self.tg.calls))
        next(iter(self.lookup.jobs.values())).result(timeout=2)
        self.app.refresh_lookups()
        self.assertEqual(self.s.user(11)['state']['choices'], [258687])
        self.assertEqual(self.s.catalog(), set())
        self.app.deliver()
        self.assertTrue(any(method == 'deleteMessage' for method, _ in self.tg.calls))

    def test_unknown_title_preserves_original_input(self):
        self.client.result = []
        self.app.handle(self.message())
        next(iter(self.lookup.jobs.values())).result(timeout=2)
        self.app.refresh_lookups()
        self.app.deliver()
        self.assertIn('Уточни', self.s.user(11)['state']['notice'])
        self.assertFalse(any(method == 'deleteMessage' for method, _ in self.tg.calls))

    def test_cached_normalization_does_not_run_agent_again(self):
        with self.assertRaises(RecommendationPending):
            self.lookup.lookup('интерстелар')
        next(iter(self.lookup.jobs.values())).result(timeout=2)
        self.assertEqual(self.lookup.lookup('интерстелар')[0]['id'], 258687)
        self.assertEqual(self.lookup.lookup('  ИНТЕРСТЕЛАР  ')[0]['id'], 258687)
        self.assertEqual(self.client.calls, 1)

    def test_navigation_while_luna_works_is_not_overwritten(self):
        self.app.handle(self.message())
        self.s.set_state(11, {'mode': 'queue'})
        next(iter(self.lookup.jobs.values())).result(timeout=2)
        self.app.refresh_lookups()
        self.assertEqual(self.s.user(11)['state'], {'mode': 'queue'})

    def test_luna_client_pins_cheaper_model_without_sending_ratings(self):
        event = {'type': 'item.completed', 'item': {'type': 'agent_message',
                 'text': json.dumps({'candidates': self.client.result})}}
        with patch('chekino.sol.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(event), '')) as runner:
            result = LunaClient(self.directory.name).normalize('интерстелар')
        self.assertEqual(result[0]['year'], 2014)
        self.assertIn('gpt-6-luna', runner.call_args.args[0])
        self.assertNotIn('ratings', runner.call_args.kwargs['input'])
