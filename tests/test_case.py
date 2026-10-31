"""Tests for the case handler (Task 8).

Local only, no AWS: the store, queue messages, cache, model and clock are the in-memory
fakes, so every outcome ("delete" or "retry") can be checked without a network.
"""

import json
import unittest

from core.jobqueue import Message, case_message
from core.models import COUNTERS
from core.scorers import SCORER_VERSIONS
from core.settings import ModelInfo, Settings
from core.store import StoreError
from tests.fakes import InMemoryCache, InMemoryClock, InMemoryModel, InMemoryStore
from worker.bedrock import ModelReply
from worker.cache import FailOpenCache
from worker.case import handle_case

OWNER = "alice"
DEADLINE = 1000.0
CASE_ID = "0001"

CONFIG = {
    "cfgId": "c_1",
    "version": 1,
    "name": "Capitals",
    "modelId": "nvidia.nemotron-super-3-120b",
    "system": "Answer briefly.",
    "template": "Q: {{input}}",
    "inferenceConfig": {"maxTokens": 256, "temperature": 0, "topP": 1, "stopSequences": []},
}
CASE = {"input": "What is 2+2?", "expected": "4", "scorer": "contains", "pattern": ""}
SETTINGS = Settings(
    table_name="t", queue_url="q", memcached_endpoint="m:11211", user_pool_id="p", client_id="c",
    region="ap-southeast-2", allowed_models=[ModelInfo("nvidia.nemotron-super-3-120b", True)],
    worker_concurrency=1, run_deadline_s=1800, cache_ttl_s=604800,
)


def make_run(run_id="r_1", mode="fresh"):
    return {
        "runId": run_id, "requestHash": "hash-" + run_id,
        "cfgId": "c_1", "suiteId": "s_1", "suiteVersion": 1,
        "config": CONFIG, "scorerVersions": dict(SCORER_VERSIONS), "effectiveMode": mode,
        "total": 1, "expanded": 1, "deadline": DEADLINE,
        **{counter: 0 for counter in COUNTERS},
    }


def make_message(run_id="r_1", receive_count=1):
    return Message(case_message(OWNER, run_id, CASE_ID), "receipt-1", receive_count)


def make_store(*run_ids, cls=InMemoryStore):
    store = cls()
    store.put_suite_version(OWNER, "s_1", "Suite", [CASE])  # becomes case "0001", version 1
    for run_id in run_ids:
        store.create_run(OWNER, make_run(run_id, "replay" if run_id == "r_2" else "fresh"))
    return store


def reply(text="The answer is 4"):
    return ModelReply(text=text, stop_reason="end_turn", tokens_in=10, tokens_out=5, retries=0)


class Recorder:
    """Collects what handle_case emits, as (dimensions, metrics) pairs."""

    def __init__(self):
        self.calls = []

    def __call__(self, dimensions, metrics):
        self.calls.append((dimensions, metrics))

    @property
    def names(self):
        return {name for _, metrics in self.calls for name in metrics}


class BrokenCache:
    def get(self, key):
        raise ConnectionError("memcached is down")

    def set(self, key, value, ttl):
        raise ConnectionError("memcached is down")


class FailingWriteStore(InMemoryStore):
    def write_result(self, *args, **kwargs):
        raise StoreError("throttled")


class CaseHandlerTests(unittest.TestCase):
    def run_case(self, store, *, cache=None, model=None, clock=None, message=None, emit=None):
        self.cache = cache if cache is not None else InMemoryCache()
        self.model = model if model is not None else InMemoryModel(reply=reply())
        self.emit = emit if emit is not None else Recorder()
        return handle_case(
            message or make_message(), store=store, cache=self.cache, model=self.model,
            settings=SETTINGS, clock=clock or InMemoryClock(10.0), emit=self.emit,
        )

    def test_fresh_calls_model_and_sets_cache(self):
        store = make_store("r_1")

        outcome = self.run_case(store)

        self.assertEqual(outcome, "delete")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(len(self.cache.data), 1)
        key, value = next(iter(self.cache.data.items()))
        self.assertTrue(key.startswith("pg:"))
        self.assertEqual(json.loads(value)["text"], "The answer is 4")
        self.assertEqual(self.cache.ttls[key], SETTINGS.cache_ttl_s)
        result = store.get_result(OWNER, "r_1", CASE_ID)
        self.assertEqual((result["verdict"], result["source"], result["truncated"]), ("PASS", "fresh", False))
        run = store.get_run(OWNER, "r_1")
        self.assertEqual((run["done"], run["passed"], run["freshCalls"], run["cacheHits"]), (1, 1, 1, 0))

    def test_replay_hit_skips_model(self):
        store = make_store("r_1", "r_2")
        self.run_case(store)  # a fresh run fills the cache
        calls_after_fresh = self.model.calls

        outcome = self.run_case(store, cache=self.cache, model=self.model, message=make_message("r_2"))

        self.assertEqual(outcome, "delete")
        self.assertEqual(self.model.calls, calls_after_fresh)  # no new model call
        result = store.get_result(OWNER, "r_2", CASE_ID)
        self.assertEqual((result["verdict"], result["source"]), ("PASS", "cache"))
        run = store.get_run(OWNER, "r_2")
        self.assertEqual((run["cacheHits"], run["freshCalls"]), (1, 0))

    def test_duplicate_delivery_counts_once(self):
        store = make_store("r_1")

        first = self.run_case(store)
        second = self.run_case(store, cache=self.cache, model=self.model)  # the same message again

        self.assertEqual((first, second), ("delete", "delete"))
        self.assertEqual(store.get_run(OWNER, "r_1")["done"], 1)

    def test_last_attempt_writes_error(self):
        store = make_store("r_1")
        model = InMemoryModel(error=RuntimeError("model unavailable"))

        outcome = self.run_case(store, model=model, message=make_message(receive_count=4))

        self.assertEqual(outcome, "delete")
        result = store.get_result(OWNER, "r_1", CASE_ID)
        self.assertEqual(result["verdict"], "ERROR")
        self.assertIn("RuntimeError", result["reason"])
        run = store.get_run(OWNER, "r_1")
        self.assertEqual((run["errored"], run["done"]), (1, 0))

    def test_earlier_attempt_retries(self):
        store = make_store("r_1")
        model = InMemoryModel(error=RuntimeError("model unavailable"))

        outcome = self.run_case(store, model=model, message=make_message(receive_count=2))

        self.assertEqual(outcome, "retry")
        self.assertIsNone(store.get_result(OWNER, "r_1", CASE_ID))
        self.assertEqual(store.get_run(OWNER, "r_1")["errored"], 0)

    def test_cache_down_still_completes(self):
        store = make_store("r_1")
        emit = Recorder()
        # The worker wraps each thread's cache like this (Task 10); here the wrapper reports
        # to the same recorder the handler emits to.
        cache = FailOpenCache(
            BrokenCache(), lambda exc: emit({"Service": "worker"}, {"CacheErrors": (1, "Count")})
        )

        outcome = self.run_case(store, cache=cache, emit=emit)

        self.assertEqual(outcome, "delete")
        self.assertEqual(store.get_result(OWNER, "r_1", CASE_ID)["verdict"], "PASS")
        self.assertIn("CacheErrors", emit.names)

    def test_output_truncated_at_32kb(self):
        store = make_store("r_1")
        model = InMemoryModel(reply=reply("a" * 40_000))

        self.run_case(store, model=model)

        result = store.get_result(OWNER, "r_1", CASE_ID)
        self.assertEqual(len(result["output"].encode("utf-8")), 32_768)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["verdict"], "FAIL")  # scored on the full output: "4" is not in it

    def test_missing_run_deleted(self):
        # RF4: a message whose run is gone can never succeed, so it is dropped and counted.
        store = make_store()  # no run

        outcome = self.run_case(store)

        self.assertEqual(outcome, "delete")
        self.assertEqual(self.model.calls, 0)
        self.assertIn("PoisonMessages", self.emit.names)

    def test_past_deadline_skips_model(self):
        store = make_store("r_1")

        outcome = self.run_case(store, clock=InMemoryClock(DEADLINE + 1))

        self.assertEqual(outcome, "delete")
        self.assertEqual(self.model.calls, 0)  # no tokens spent on a run that can't complete
        self.assertIn("LateCases", self.emit.names)
        run = store.get_run(OWNER, "r_1")
        self.assertEqual((run["done"], run["errored"]), (0, 0))

    def test_store_error_retries(self):
        store = make_store("r_1", cls=FailingWriteStore)

        outcome = self.run_case(store)

        self.assertEqual(outcome, "retry")  # the message stays on the queue

    def test_existing_result_skips_model(self):
        store = make_store("r_1")
        store.write_result(OWNER, "r_1", CASE_ID, {"verdict": "PASS"}, {"done": 1, "passed": 1}, 5.0)

        outcome = self.run_case(store)

        self.assertEqual(outcome, "delete")
        self.assertEqual(self.model.calls, 0)


if __name__ == "__main__":
    unittest.main()
