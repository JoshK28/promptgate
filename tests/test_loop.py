"""Tests for the worker loop (Task 10).

Local only, no AWS: the handlers run for real against the in-memory store, queue, cache and
model. Threads are used only where the test is about threads, and they wait on Events
that the test sets, never on sleeps.
"""

import threading
import unittest

from core.jobqueue import case_message
from tests.fakes import InMemoryCache, InMemoryClock, InMemoryModel, InMemoryQueue, InMemoryStore
from tests.test_case import CASE_ID, OWNER, SETTINGS, Recorder, make_store, reply
from worker.loop import Deps, process_message, worker_thread

WAIT_S = 5  # longest a test waits for a thread; only reached when the code under test is broken


class RecordingQueue(InMemoryQueue):
    def __init__(self):
        super().__init__()
        self.deleted = []
        self.deleted_event = threading.Event()

    def delete(self, receipt):
        self.deleted.append(receipt)
        self.deleted_event.set()
        super().delete(receipt)


class FlakyReceiveQueue(RecordingQueue):
    """The first receive fails, as an SQS outage would; later ones work."""

    def __init__(self):
        super().__init__()
        self.receive_calls = 0

    def receive(self, wait_s=20):
        self.receive_calls += 1
        if self.receive_calls == 1:
            raise RuntimeError("SQS unreachable")
        return super().receive(wait_s)


class BlockingModel(InMemoryModel):
    """converse() waits until the test releases it, so a case can be held mid-way."""

    def __init__(self, model_reply):
        super().__init__(reply=model_reply)
        self.started = threading.Event()
        self.release = threading.Event()

    def converse(self, request):
        self.started.set()
        if not self.release.wait(WAIT_S):
            raise RuntimeError("the test never released the model")
        return super().converse(request)


class BrokenReadStore(InMemoryStore):
    def get_run(self, owner, run_id):
        raise RuntimeError("DynamoDB throttled")


def make_deps(store, *, queue=None, model=None):
    return Deps(
        store=store,
        queue=queue if queue is not None else RecordingQueue(),
        cache=InMemoryCache(),
        model=model if model is not None else InMemoryModel(reply=reply()),
        settings=SETTINGS,
        clock=InMemoryClock(10.0),
        emit=Recorder(),
    )


def start_thread(deps, stop, **kwargs):
    thread = threading.Thread(target=worker_thread, args=(deps, stop), kwargs=kwargs, daemon=True)
    thread.start()
    return thread


class ProcessMessageTests(unittest.TestCase):
    def test_retry_leaves_message(self):
        deps = make_deps(make_store("r_1"), model=InMemoryModel(error=RuntimeError("model unavailable")))
        deps.queue.send(case_message(OWNER, "r_1", CASE_ID))

        process_message(deps.queue.receive()[0], deps)  # receive 1 of 4: the handler answers "retry"

        self.assertEqual(deps.queue.deleted, [])  # left on the queue; SQS redelivers it later

    def test_finished_message_deleted(self):
        deps = make_deps(make_store("r_1"))
        deps.queue.send(case_message(OWNER, "r_1", CASE_ID))
        message = deps.queue.receive()[0]

        process_message(message, deps)

        self.assertEqual(deps.queue.deleted, [message.receipt])
        self.assertEqual(deps.store.get_result(OWNER, "r_1", CASE_ID)["verdict"], "PASS")

    def test_unknown_type_deleted(self):
        deps = make_deps(make_store("r_1"))
        deps.queue.send({"type": "mystery", "owner": OWNER})
        message = deps.queue.receive()[0]

        process_message(message, deps)

        self.assertEqual(deps.queue.deleted, [message.receipt])
        self.assertIn("PoisonMessages", deps.emit.names)  # RF4: counted, never retried forever

    def test_malformed_body_deleted(self):
        # RF4: a message missing a field can never succeed, so it is dropped and counted,
        # not retried until it lands in the dead-letter queue and trips its alarm.
        bodies = [
            {"type": "case", "runId": "r_1", "caseId": CASE_ID},  # no owner
            {"type": "case", "owner": OWNER, "runId": "r_1"},  # no caseId
            {"type": "expand", "owner": OWNER},  # no runId
            ["not", "an", "object"],  # valid JSON, but not a message
        ]
        for body in bodies:
            with self.subTest(body=body):
                deps = make_deps(make_store("r_1"))
                deps.queue.send(body)
                message = deps.queue.receive()[0]

                process_message(message, deps)

                self.assertEqual(deps.queue.deleted, [message.receipt])
                self.assertIn("PoisonMessages", deps.emit.names)

    def test_handler_exception_leaves_message(self):
        # The case handler doesn't catch store faults (Task 8 carry-forward), so the loop must.
        deps = make_deps(make_store("r_1", cls=BrokenReadStore))
        deps.queue.send(case_message(OWNER, "r_1", CASE_ID))

        process_message(deps.queue.receive()[0], deps)  # must not raise

        self.assertEqual(deps.queue.deleted, [])


class WorkerThreadTests(unittest.TestCase):
    def test_stop_event_finishes_current_then_exits(self):
        model = BlockingModel(reply())
        deps = make_deps(make_store("r_1"), model=model)
        deps.queue.send(case_message(OWNER, "r_1", CASE_ID))
        deps.queue.send(case_message(OWNER, "r_1", CASE_ID))  # a second message, waiting behind
        stop = threading.Event()

        thread = start_thread(deps, stop)
        self.assertTrue(model.started.wait(WAIT_S))  # the thread is inside the first case
        stop.set()  # scale-in arrives mid-case
        self.assertTrue(thread.is_alive())  # it has not abandoned the case

        model.release.set()
        thread.join(1)

        self.assertFalse(thread.is_alive())  # exited within 1 s of the case finishing
        self.assertEqual(len(deps.queue.deleted), 1)  # the case in hand was finished and deleted...
        self.assertEqual(len(deps.queue.sent), 2)  # ...and the second was left for another worker
        self.assertEqual(model.calls, 1)

    def test_receive_error_does_not_kill_thread(self):
        queue = FlakyReceiveQueue()
        deps = make_deps(make_store("r_1"), queue=queue)
        queue.send(case_message(OWNER, "r_1", CASE_ID))
        stop = threading.Event()

        thread = start_thread(deps, stop, error_pause_s=0.01)
        finished = queue.deleted_event.wait(WAIT_S)  # the case is processed after the failed receive
        stop.set()
        thread.join(1)

        self.assertTrue(finished)
        self.assertGreaterEqual(queue.receive_calls, 2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
