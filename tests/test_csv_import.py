"""Task 2 CSV import contracts (spec §18.4)."""

import unittest

from core.csv_import import CsvError, parse_suite_csv

HEADER = "input,expected,scorer,pattern\n"


class CsvImportTests(unittest.TestCase):
    def errors_for(self, text):
        with self.assertRaises(CsvError) as caught:
            parse_suite_csv(text)
        return caught.exception.errors

    def assert_row_error(self, errors, row):
        self.assertTrue(
            any(error.startswith(f"row {row}:") for error in errors), errors
        )

    def test_valid_rows_any_column_order(self):
        expected = [
            {"input": "capital?", "expected": "Paris", "scorer": "exact", "pattern": ""},
            {"input": "year?", "expected": "", "scorer": "regex", "pattern": "[0-9]{4}"},
        ]
        texts = (
            HEADER + "capital?,Paris,exact,\nyear?,,regex,[0-9]{4}\n",
            "pattern,scorer,input,expected\n"
            ",exact,capital?,Paris\n[0-9]{4},regex,year?,\n",
            "expected,input,pattern,scorer\n"
            "Paris,capital?,,exact\n,year?,[0-9]{4},regex\n",
        )
        for text in texts:
            with self.subTest(header=text.splitlines()[0]):
                self.assertEqual(parse_suite_csv(text), expected)

    def test_unknown_column_rejected(self):
        errors = self.errors_for(
            "input,expected,scorer,pattern,extra\nquestion,answer,exact,,value\n"
        )
        self.assertTrue(
            any(e.startswith("row 1:") and "extra" in e for e in errors), errors
        )

    def test_optional_columns_default_to_blank(self):
        self.assertEqual(
            parse_suite_csv("input,expected,scorer\nq,a,contains\n"),
            [{"input": "q", "expected": "a", "scorer": "contains", "pattern": ""}],
        )
        self.assertEqual(
            parse_suite_csv("input,scorer,pattern\nq,regex,[0-9]\n"),
            [{"input": "q", "expected": "", "scorer": "regex", "pattern": "[0-9]"}],
        )

    def test_missing_header_rejected(self):
        # Only input and scorer are required; an empty file has no header at all.
        for text in ("", "input,expected\nq,a\n", "scorer,expected\nexact,a\n"):
            with self.subTest(text=text):
                errors = self.errors_for(text)
                self.assertTrue(errors)
                self.assertTrue(all(e.startswith("row 1:") for e in errors), errors)

    def test_duplicate_column_rejected(self):
        # DictReader would silently keep the last value, losing the first column.
        errors = self.errors_for("input,input,scorer\nfirst,second,exact\n")
        self.assertTrue(
            any(e.startswith("row 1:") and "input" in e for e in errors), errors
        )

    def test_oversized_header_cell_is_a_csv_error(self):
        # Larger than the csv module's own field limit, but under our 1 MB file limit.
        self.assert_row_error(self.errors_for("input," + "h" * 200_000 + ",scorer\n"), 1)

    def test_bad_regex_reports_row(self):
        errors = self.errors_for(HEADER + "one,one,exact,\ntwo,two,exact,\nthree,,regex,(\n")
        self.assert_row_error(errors, 4)

    def test_limits_rejected(self):
        # 999 rows of exactly 1,000 bytes + a short last row land on the byte limit.
        full_row = "a" * 990 + ",x,exact,\n"
        body_over = HEADER + full_row * 999 + "a" * 961 + ",x,exact,\n"
        # Swapping "aa" for "é" keeps 1,000,000 characters but makes 1,000,001 bytes,
        # so a character count instead of a byte count would let this through.
        utf8_over = body_over.replace("aa", "é", 1)
        self.assertEqual(len(body_over.encode("utf-8")), 1_000_001)
        self.assertEqual(len(utf8_over), 1_000_000)
        self.assertEqual(len(utf8_over.encode("utf-8")), 1_000_001)

        # label: (row the error is reported on, text). File-wide limits report row 1.
        cases = {
            "1,001 rows": (1, HEADER + "a,x,exact,\n" * 1_001),
            "1,000,001 ASCII bytes": (1, body_over),
            "1,000,001 UTF-8 bytes": (1, utf8_over),
            "8,001 input characters": (2, HEADER + "a" * 8_001 + ",x,exact,\n"),
            "8,001 expected characters": (2, HEADER + "a," + "x" * 8_001 + ",contains,\n"),
            "8,001 pattern characters": (2, HEADER + "a,,regex," + "a" * 8_001 + "\n"),
        }
        for label, (row, text) in cases.items():
            with self.subTest(limit=label):
                self.assert_row_error(self.errors_for(text), row)

    def test_limits_accepted_at_boundary(self):
        full_row = "a" * 990 + ",x,exact,\n"
        body_at_limit = HEADER + full_row * 999 + "a" * 960 + ",x,exact,\n"
        self.assertEqual(len(body_at_limit.encode("utf-8")), 1_000_000)
        self.assertEqual(len(parse_suite_csv(body_at_limit)), 1_000)

        # 8,000 two-byte characters: the cell limit counts characters, not bytes.
        cell = "é" * 8_000
        self.assertEqual(parse_suite_csv(HEADER + cell + ",x,exact,\n")[0]["input"], cell)

    def test_zero_data_rows_rejected(self):
        self.assert_row_error(self.errors_for(HEADER), 1)

    def test_unknown_scorer_rejected(self):
        errors = self.errors_for(HEADER + "one,answer,bogus,\ntwo,answer,ci:bogus,\n")
        self.assert_row_error(errors, 2)
        self.assert_row_error(errors, 3)

    def test_regex_requires_pattern(self):
        for scorer in ("regex", "ci:regex"):
            with self.subTest(scorer=scorer):
                self.assert_row_error(self.errors_for(HEADER + f"q,,{scorer},\n"), 2)

    def test_contains_requires_expected(self):
        # Every scorer except regex needs `expected`; for not_contains it is the
        # forbidden text, and a blank one would match everything.
        scorers = ("exact", "contains", "not_contains")
        for scorer in scorers + tuple("ci:" + s for s in scorers):
            with self.subTest(scorer=scorer):
                self.assert_row_error(self.errors_for(HEADER + f"q,,{scorer},\n"), 2)

    def test_excel_export(self):
        text = '﻿input,expected,scorer,pattern\r\n"a, b","line1\nline2",contains,\r\n'
        self.assertEqual(
            parse_suite_csv(text),
            [{"input": "a, b", "expected": "line1\nline2", "scorer": "contains", "pattern": ""}],
        )
