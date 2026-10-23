from pathlib import Path
import tempfile
import unittest
from chekino.requirements import load_requirements


class RequirementsTests(unittest.TestCase):
    def test_live_edits_and_hard_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rules.toml'
            path.write_text('min_year=1995\nexcluded_countries=["Country X"]\ninstructions="More mysteries"\n')
            rules = load_requirements(path)
            self.assertFalse(rules.allows({'year': 1994, 'countries': ['Country Y']}))
            self.assertFalse(rules.allows({'year': 2000, 'countries': ['Country X', 'Country Y']}))
            self.assertFalse(rules.allows({'year': 2000}))
            self.assertTrue(rules.allows({'year': 1995, 'countries': ['Country Y']}))
            path.write_text('min_year=2001\n')
            updated = load_requirements(path)
            self.assertNotEqual(rules.digest, updated.digest)
            self.assertFalse(updated.allows({'year': 2000}))

    def test_invalid_or_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rules.toml'
            for text in ('min_year="bad"', 'unknown=1', 'excluded_countries=[2]'):
                path.write_text(text)
                rules = load_requirements(path)
                self.assertTrue(rules.error)
                self.assertFalse(rules.allows({'year': 2020}))
            path.unlink()
            self.assertTrue(load_requirements(path).error)
        self.assertFalse(load_requirements().error)
