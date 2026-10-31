"""Task 2 scorer contracts (spec §18.4)."""

import unittest

from core.scorers import SCORER_VERSIONS, score


def passed(scorer, output, expected="", pattern=""):
    return score(scorer, output, expected, pattern).passed


class ScorerTests(unittest.TestCase):
    def test_exact_trims_whitespace(self):
        self.assertTrue(passed("exact", " Paris \n", "Paris"))
        self.assertTrue(passed("exact", "Paris", " Paris \n"))

    def test_exact_case_rules(self):
        self.assertFalse(passed("exact", "Paris", "paris"))
        self.assertTrue(passed("ci:exact", "Paris", "paris"))

    def test_contains_case_rules(self):
        self.assertTrue(passed("contains", "The capital is Paris.", "Paris"))
        self.assertFalse(passed("contains", "The capital is Paris.", "paris"))
        self.assertTrue(passed("ci:contains", "The capital is Paris.", "paris"))

    def test_not_contains(self):
        self.assertFalse(passed("not_contains", "Try the Berlin office.", "Berlin"))
        self.assertTrue(passed("not_contains", "Try the Paris office.", "Berlin"))
        self.assertTrue(passed("not_contains", "Try the Berlin office.", "berlin"))
        self.assertFalse(passed("ci:not_contains", "Try the Berlin office.", "berlin"))

    def test_regex_uses_search_semantics(self):
        self.assertTrue(passed("regex", "in 1999 we", pattern=r"\d{4}"))
        self.assertFalse(passed("regex", "no year here", pattern=r"\d{4}"))
        self.assertFalse(passed("regex", "in Lima we", pattern="LIMA"))
        self.assertTrue(passed("ci:regex", "in Lima we", pattern="LIMA"))

    def test_failing_verdicts_have_reasons(self):
        cases = (
            ("exact", "Rome", "Paris", ""),
            ("contains", "Rome", "Paris", ""),
            ("not_contains", "Paris", "Paris", ""),
            ("regex", "no year here", "", r"\d{4}"),
        )
        for scorer, output, expected, pattern in cases:
            with self.subTest(scorer=scorer):
                verdict = score(scorer, output, expected, pattern)
                self.assertFalse(verdict.passed)
                self.assertTrue(verdict.reason.strip())

    def test_scorer_versions(self):
        self.assertEqual(
            SCORER_VERSIONS,
            {
                "exact": "exact-v1",
                "contains": "contains-v1",
                "not_contains": "not_contains-v1",
                "regex": "regex-v1",
            },
        )
