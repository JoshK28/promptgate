"""The worker's receive loop: pull one message at a time, hand it to its handler, and
stop between messages when ECS scales the task in (spec §S8)."""

from dataclasses import dataclass
from typing import Any

from core import metrics
from worker.case import DIMENSIONS, handle_case
from worker.expand import handle_expand

QUEUE_WAIT_S = 20  # long polling (5.31): an idle worker makes about 3 requests a minute
REQUIRED_FIELDS = {  # what each message type's handler reads from the body
    "expand": ("owner", "runId"),
    "case": ("owner", "runId", "caseId"),
}


@dataclass(frozen=True)
class Deps:
    """Everything a handler needs. Each worker thread gets its own copy because the
    Memcached client is not thread-safe, so `cache` is the one field that differs."""

    store: Any
    queue: Any
    cache: Any
    model: Any
    settings: Any
    clock: Any
    emit: Any = metrics.emit


def process_message(msg, deps):
    """Runs the handler for one message, then deletes the message unless the handler
    asked for a retry. A retried message is simply left: SQS shows it again once the
    300 s visibility timeout runs out."""
    body = msg.body if isinstance(msg.body, dict) else {}
    kind = body.get("type")
    required = REQUIRED_FIELDS.get(kind)
    try:
        if required is None or not all(field in body for field in required):
            # An unknown type, or a body missing a field: no retry can fix it, and retrying
            # would only end in the dead-letter queue and trip its alarm (RF4).
            deps.emit(DIMENSIONS, {"PoisonMessages": (1, "Count")})
            outcome = "delete"
        elif kind == "expand":
            outcome = handle_expand(msg, store=deps.store, queue=deps.queue)
        elif kind == "case":
            outcome = handle_case(
                msg, store=deps.store, cache=deps.cache, model=deps.model,
                settings=deps.settings, clock=deps.clock, emit=deps.emit,
            )
    except Exception as exc:  # Exception, not a bare except: SIGTERM and Ctrl-C stay intact
        # The handlers don't catch store faults (such as DynamoDB throttling), so a fault
        # reaches here. Leaving the message is the safe answer: it is retried, and a message
        # that keeps failing ends in the dead-letter queue.
        print(f"{kind} message failed, left for redelivery: {type(exc).__name__}: {exc}", flush=True)
        return
    if outcome == "delete":
        deps.queue.delete(msg.receipt)


def worker_thread(deps, stop, error_pause_s=5):
    """Loops until `stop` is set. A message already started is always finished; once `stop`
    is set no new one is started."""
    while not stop.is_set():
        try:
            messages = deps.queue.receive(wait_s=QUEUE_WAIT_S)
        except Exception as exc:
            # One failed receive must not end the thread: the container would stay up
            # but never process anything again. stop.wait is a pause a stop can cut short.
            print(f"receive failed: {type(exc).__name__}: {exc}", flush=True)
            stop.wait(error_pause_s)
            continue
        for msg in messages:
            if stop.is_set():
                break  # received but not started: it becomes visible again after the timeout
            process_message(msg, deps)
