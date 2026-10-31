"""Task 15 synthetic load suite contract. Local only, no AWS."""

import unittest

from core.csv_import import parse_suite_csv
from loadgen.generate import generate


class LoadgenTests(unittest.TestCase):
    def test_generated_csv_parses_and_unique(self):
        rows = parse_suite_csv(generate(50, 1))

        self.assertEqual(len(rows), 50)
        self.assertEqual(len({row["input"] for row in rows}), 50)
