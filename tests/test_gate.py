"""Task 4 contracts: the exit code the CLI returns to CI (spec §8)."""

import unittest

from core.gate import gate_exit_code, pass_rate


def gate(**overrides):
    """A finished, clean 10/10 run against min_pass 0.9, with any field overridden."""
    fields = dict(status="COMPLETE", total=10, passed=10, errored=0, min_pass=0.9)
    return gate_exit_code(**{**fields, **overrides})


class GateTests(unittest.TestCase):
    def test_pass_rate(self):
        self.assertEqual(pass_rate(8, 10), 0.8)
        self.assertEqual(pass_rate(0, 0), 0.0)  # no cases: no division by zero

    def test_complete_and_above_threshold_exits_0(self):
        code, _ = gate()
        self.assertEqual(code, 0)

    def test_below_min_pass_exits_1(self):
        code, _ = gate(passed=8)
        self.assertEqual(code, 1)

    def test_below_baseline_exits_1_unless_within_tolerance(self):
        # min_pass is low so only the baseline decides. At tolerance 0.1 the rate sits
        # exactly on the limit (0.9), which must pass.
        code, _ = gate(passed=9, min_pass=0.5, baseline_rate=1.0, tolerance=0.0)
        self.assertEqual(code, 1)
        code, _ = gate(passed=9, min_pass=0.5, baseline_rate=1.0, tolerance=0.1)
        self.assertEqual(code, 0)

    def test_errored_case_exits_2(self):
        code, _ = gate(passed=9, errored=1)
        self.assertEqual(code, 2)
        # A service problem outranks a low pass rate: we can't say the prompt regressed.
        code, _ = gate(passed=4, errored=1)
        self.assertEqual(code, 2)

    def test_incomplete_exits_2(self):
        code, _ = gate(status="INCOMPLETE", passed=3)
        self.assertEqual(code, 2)
        code, _ = gate(status="INCOMPLETE")  # even when every finished case passed
        self.assertEqual(code, 2)

    def test_no_baseline_is_stated(self):
        _, reason = gate()
        self.assertIn("no baseline", reason)
