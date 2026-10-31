"""Task 6 contracts: DynamoDB write rules, queue messages and sample suites.

Local only, no AWS: the pure builders are called directly and the rest runs on the fakes.
"""

import base64
import unittest

from core.jobqueue import case_message, expand_message
from core.models import COUNTERS, derive_status
from core.samples import SAMPLE_SUITES, ensure_sample_suites
from core.store import (
    IdempotencyConflict,
    StoreError,
    classify_cancellation,
    decode_cursor,
    encode_cursor,
    from_dynamo,
    result_transaction,
    to_dynamo,
)
from tests.fakes import InMemoryStore

OWNER = "alice"


def make_run(**overrides):
    """A queued two-case run with zeroed counters and a deadline at t=1000."""
    run = {
        "runId": "r_1",
        "requestHash": "hash-a",
        "total": 2,
        "expanded": 2,
        "deadline": 1000.0,
        **{counter: 0 for counter in COUNTERS},
    }
    return {**run, **overrides}


class PureBuilderTests(unittest.TestCase):
    def test_result_transaction_conditional(self):
        items = result_transaction(
            "table", "alice", "r_1", "0042", {"verdict": True}, {"done": 1, "passed": 1, "tokensIn": 12}, 500.0
        )
        put, update = items[0]["Put"], items[1]["Update"]

        # Item 0: a retried message can't write (and so can't count) the result twice.
        self.assertEqual(put["ConditionExpression"], "attribute_not_exists(PK)")
        self.assertEqual(put["Item"]["PK"], {"S": "U#alice#RUN#r_1"})
        self.assertEqual(put["Item"]["SK"], {"S": "RES#0042"})

        # Item 1: the run's counters, refused once the deadline has passed (M1).
        self.assertEqual(update["Key"], {"PK": {"S": "U#alice"}, "SK": {"S": "RUN#r_1"}})
        self.assertTrue(update["UpdateExpression"].startswith("ADD "))
        self.assertIn("done", update["UpdateExpression"])
        self.assertIn("tokensIn", update["UpdateExpression"])
        self.assertEqual(update["ConditionExpression"], "deadline >= :now")

    def test_result_item_carries_case_id(self):
        # InMemoryStore stores caseId on each result, so the real item must too; otherwise
        # every local test passes and results read back from AWS have no case ID.
        items = result_transaction("table", "alice", "r_1", "0042", {"verdict": True}, {"done": 1}, 500.0)
        self.assertEqual(items[0]["Put"]["Item"]["caseId"], {"S": "0042"})

    def test_classify_cancellation(self):
        failed, passed = {"Code": "ConditionalCheckFailed"}, {"Code": "None"}
        self.assertEqual(classify_cancellation([failed, passed]), "duplicate")
        self.assertEqual(classify_cancellation([passed, failed]), "expired")
        # Anything else is a real fault: raise so the message is retried, never dropped (M7).
        with self.assertRaises(StoreError):
            classify_cancellation([{"Code": "ThrottlingError"}, passed])

    def test_floats_become_decimal(self):
        # boto3's serializer refuses Python floats (M5). Decimal(str(x)) keeps "0.7";
        # Decimal(0.7) would carry float noise (0.6999...) and be refused as inexact.
        item = to_dynamo({"temperature": 0.7, "topP": [1.0]})
        self.assertEqual(item, {"M": {"temperature": {"N": "0.7"}, "topP": {"L": [{"N": "1.0"}]}}})
        self.assertEqual(from_dynamo(item), {"temperature": 0.7, "topP": [1.0]})

    def test_cursor_roundtrip(self):
        key = {"PK": {"S": "U#alice"}, "SK": {"S": "CASE#0007"}}
        self.assertEqual(decode_cursor(encode_cursor(key)), key)
        self.assertIsNone(decode_cursor(None))  # no ?cursor= means the first page

    def test_cursor_must_be_an_object(self):
        # A client can send any base64 text. Valid JSON that isn't an object (a list, a
        # string, a number) must be a ValueError, which the API turns into a 400, and
        # not an AttributeError further down, which would be a 500.
        for raw in (b"[1]", b'"text"', b"5", b"not json"):
            cursor = base64.urlsafe_b64encode(raw).decode("ascii")
            with self.assertRaises(ValueError, msg=raw):
                decode_cursor(cursor)


class InMemoryStoreTests(unittest.TestCase):
    def test_fake_write_result_idempotent(self):
        store = InMemoryStore()
        store.create_run(OWNER, make_run())
        counters = {"done": 1, "passed": 1}

        self.assertEqual(store.write_result(OWNER, "r_1", "0000", {"verdict": True}, counters, 10.0), "written")
        self.assertEqual(store.write_result(OWNER, "r_1", "0000", {"verdict": True}, counters, 11.0), "duplicate")

        run = store.get_run(OWNER, "r_1")
        self.assertEqual((run["done"], run["passed"]), (1, 1))  # counted once

    def test_fake_write_after_deadline_expired(self):
        # RF6: a result arriving after the deadline must not turn an INCOMPLETE run COMPLETE.
        store = InMemoryStore()
        store.create_run(OWNER, make_run(total=1))

        outcome = store.write_result(OWNER, "r_1", "0000", {"verdict": True}, {"done": 1, "passed": 1}, 1001.0)

        self.assertEqual(outcome, "expired")
        run = store.get_run(OWNER, "r_1")
        self.assertEqual(run["done"], 0)
        self.assertEqual(derive_status(run, 1001.0), "INCOMPLETE")

    def test_create_run_conflict(self):
        store = InMemoryStore()
        store.create_run(OWNER, make_run())
        with self.assertRaises(IdempotencyConflict):
            store.create_run(OWNER, make_run(requestHash="hash-b"))

    def test_create_run_idempotent(self):
        store = InMemoryStore()
        first, created_first = store.create_run(OWNER, make_run())
        second, created_second = store.create_run(OWNER, make_run())

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(second, first)

    def test_suite_cases_written_before_version(self):
        # M2: the SUITE# item is what makes a suite visible, so it must be written last.
        cases = [{"input": f"q{i}", "expected": "a", "scorer": "contains", "pattern": ""} for i in range(5)]
        store = InMemoryStore(fail_after_case_writes=3)

        with self.assertRaises(StoreError):
            store.put_suite_version(OWNER, "s1", "Suite", cases)

        self.assertEqual(store.case_writes, 3)  # the cases really were written first
        self.assertIsNone(store.get_suite_version(OWNER, "s1", 1))
        self.assertEqual(store.list_suites(OWNER), [])

    def test_fake_rejects_cursor_from_another_run(self):
        # The real _query refuses a cursor taken from a different partition, so the fake
        # must too, or an API test for "tampered cursor -> 400" would fail locally.
        store = InMemoryStore()
        store.create_run(OWNER, make_run())
        for case_id in ("0001", "0002"):
            store.write_result(OWNER, "r_1", case_id, {"verdict": True}, {"done": 1}, 10.0)
        _, cursor = store.query_results(OWNER, "r_1", None, 1)
        self.assertIsNotNone(cursor)

        store.query_results(OWNER, "r_1", cursor, 1)  # its own run's cursor still works
        with self.assertRaises(ValueError):
            store.query_results(OWNER, "r_2", cursor, 1)

    def test_fake_owner_isolation(self):
        store = InMemoryStore()
        store.create_run("alice", make_run())
        self.assertIsNotNone(store.get_run("alice", "r_1"))
        self.assertIsNone(store.get_run("bob", "r_1"))


class MessageAndSampleTests(unittest.TestCase):
    def test_case_message_shape(self):
        self.assertEqual(
            case_message("alice", "r_1", "0042"),
            {"type": "case", "owner": "alice", "runId": "r_1", "caseId": "0042"},
        )
        self.assertEqual(expand_message("alice", "r_1"), {"type": "expand", "owner": "alice", "runId": "r_1"})

    def test_samples_parse_and_seed_once(self):
        store = InMemoryStore()
        self.assertEqual(set(SAMPLE_SUITES), {"pub_capitals", "pub_format"})

        self.assertEqual(ensure_sample_suites(store), 2)  # imports and parses both CSVs
        self.assertEqual(ensure_sample_suites(store), 0)  # version 1 exists, so nothing is added

        for suite_id in SAMPLE_SUITES:
            self.assertGreater(store.get_suite_version("public", suite_id, 1)["caseCount"], 0)
