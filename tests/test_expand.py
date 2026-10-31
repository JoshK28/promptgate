"""Tests for the expand handler (Task 9).

Local only, no AWS: the store and the queue are the in-memory fakes, so a worker crash
halfway through an expansion can be simulated by making one call raise.
"""

import unittest
from types import SimpleNamespace

from core.jobqueue import Message, expand_message
from core.models import COUNTERS
from core.samples import ensure_sample_suites
from core.scorers import SCORER_VERSIONS
from tests.fakes import InMemoryCache, InMemoryModel, InMemoryQueue, InMemoryStore
from worker.bedrock import ModelReply
from worker.case import handle_case
from worker.expand import handle_expand

OWNER = "alice"
CASE_COUNT = 25


def make_run(run_id="r_1", total=CASE_COUNT):
    return {
        "runId": run_id, "requestHash": "hash-" + run_id,
        "cfgId": "c_1", "suiteId": "s_1", "suiteVersion": 1,
        "config": {}, "scorerVersions": dict(SCORER_VERSIONS), "effectiveMode": "fresh",
        "total": total, "expanded": 0, "deadline": 1000.0,
        **{counter: 0 for counter in COUNTERS},
    }


def make_store(cls=InMemoryStore, run=None):
    store = cls()
    cases = [{"input": f"q{i}", "expected": "x", "scorer": "contains", "pattern": ""} for i in range(CASE_COUNT)]
    store.put_suite_version(OWNER, "s_1", "Suite", cases)  # cases "0001" to "0025", version 1
    store.create_run(OWNER, run or make_run())
    return store


def make_message(run_id="r_1"):
    return Message(expand_message(OWNER, run_id), "receipt-1", 1)


class BatchQueue(InMemoryQueue):
    """Records the size of every send_batch call."""

    def __init__(self):
        super().__init__()
        self.batches = []

    def send_batch(self, bodies):
        self.batches.append(len(bodies))
        return super().send_batch(bodies)


class CrashingStore(InMemoryStore):
    """Raises on the nth record_expansion call, after that page's messages were already sent."""

    def __init__(self, crash_on):
        super().__init__()
        self.crash_on = crash_on
        self.record_calls = 0

    def record_expansion(self, *args, **kwargs):
        self.record_calls += 1
        if self.record_calls == self.crash_on:
            raise RuntimeError("worker died before saving the cursor")
        super().record_expansion(*args, **kwargs)


def sent_case_ids(queue):
    return {body["caseId"] for body in queue.sent}


class ExpandHandlerTests(unittest.TestCase):
    def test_expands_all_cases_in_batches_of_10(self):
        store, queue = make_store(), BatchQueue()

        outcome = handle_expand(make_message(), store=store, queue=queue)

        self.assertEqual(outcome, "delete")
        self.assertEqual(queue.batches, [10, 10, 5])  # 25 cases, SQS's limit of 10 per call
        self.assertEqual(len(queue.sent), CASE_COUNT)
        self.assertEqual(
            queue.sent[0], {"type": "case", "owner": OWNER, "runId": "r_1", "caseId": "0001"}
        )
        self.assertEqual(sent_case_ids(queue), {f"{i:04d}" for i in range(1, CASE_COUNT + 1)})
        self.assertEqual(store.get_run(OWNER, "r_1")["expanded"], CASE_COUNT)

    def test_resumes_from_cursor(self):
        store, queue = make_store(cls=lambda: CrashingStore(crash_on=2)), BatchQueue()

        with self.assertRaises(RuntimeError):
            handle_expand(make_message(), store=store, queue=queue, page_size=10)
        # Page 1 was saved; page 2 was sent but its cursor was not.
        self.assertEqual(store.get_run(OWNER, "r_1")["expanded"], 10)

        outcome = handle_expand(make_message(), store=store, queue=queue, page_size=10)

        self.assertEqual(outcome, "delete")
        self.assertGreaterEqual(len(queue.sent), CASE_COUNT)  # page 2 went out twice
        self.assertEqual(len(sent_case_ids(queue)), CASE_COUNT)  # but every case was sent
        self.assertEqual(store.get_run(OWNER, "r_1")["expanded"], CASE_COUNT)

    def test_failed_entries_retried(self):
        store, queue = make_store(), BatchQueue()
        queue.reject_next = 3  # the first 3 entries of the first batch come back as failed

        outcome = handle_expand(make_message(), store=store, queue=queue)

        self.assertEqual(outcome, "delete")
        self.assertEqual(len(queue.sent), CASE_COUNT)  # the 3 were resent, none twice
        self.assertEqual(len(sent_case_ids(queue)), CASE_COUNT)

    def test_persistent_send_failure_raises(self):
        store, queue = make_store(), BatchQueue()
        queue.reject_next = 1000  # SQS keeps refusing

        with self.assertRaises(RuntimeError):
            handle_expand(make_message(), store=store, queue=queue)

        # Nothing was recorded for the failed page, so the redelivered message starts it again.
        self.assertEqual(store.get_run(OWNER, "r_1")["expanded"], 0)

    def test_already_expanded_noop(self):
        run = make_run() | {"expanded": CASE_COUNT}
        store, queue = make_store(run=run), BatchQueue()

        outcome = handle_expand(make_message(), store=store, queue=queue)

        self.assertEqual(outcome, "delete")
        self.assertEqual(queue.sent, [])

    def test_public_suite_run_expands_and_scores(self):
        # Regression (Task 16 local E2E): a pub_ suite's cases are stored under "public",
        # while the run, its messages and its results stay under the requester.
        store, queue = InMemoryStore(), InMemoryQueue()
        ensure_sample_suites(store)
        config = {
            "modelId": "nvidia.nemotron-super-3-120b", "system": "Answer briefly.", "template": "{{input}}",
            "inferenceConfig": {"maxTokens": 256, "temperature": 0, "topP": 1, "stopSequences": []},
        }
        store.create_run(OWNER, make_run(total=10) | {"suiteId": "pub_capitals", "config": config})

        self.assertEqual(handle_expand(make_message(), store=store, queue=queue), "delete")
        self.assertEqual(len(queue.sent), 10)
        self.assertEqual(queue.sent[0]["owner"], OWNER)

        model = InMemoryModel(ModelReply(text="Canberra", stop_reason="end_turn", tokens_in=10, tokens_out=2, retries=0))
        outcome = handle_case(
            Message(queue.sent[0], "receipt-2", 1), store=store, cache=InMemoryCache(), model=model,
            settings=SimpleNamespace(cache_ttl_s=60), clock=lambda: 0.0, emit=lambda dims, values: None,
        )

        self.assertEqual(outcome, "delete")
        self.assertEqual(store.get_result(OWNER, "r_1", "0001")["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
