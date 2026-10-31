"""Run records: counter names, derived status and run IDs. Pure: no I/O, no AWS."""

import hashlib

COUNTERS = ("done", "passed", "errored", "freshCalls", "cacheHits", "tokensIn", "tokensOut")


def derive_status(run: dict, now: float) -> str:
    # Computed on every read from the counters, so it can't go stale. The order matters:
    # COMPLETE is checked first, which is safe because a result written after the
    # deadline is refused (M1), so a late case can't turn an INCOMPLETE run COMPLETE.
    finished = run["done"] + run["errored"]
    if finished == run["total"]:
        return "COMPLETE"
    if now > run["deadline"]:
        return "INCOMPLETE"
    if run["expanded"] < run["total"] and finished == 0:
        return "QUEUED"
    return "RUNNING"


def new_run_id(owner, idempotency_key):
    # Derived, not random: a retried POST /runs with the same key gets the same run (M3).
    # The NUL separator stops ("ab", "c") and ("a", "bc") hashing the same text.
    digest = hashlib.sha256(f"{owner}\0{idempotency_key}".encode("utf-8")).hexdigest()
    return "r_" + digest[:16]
