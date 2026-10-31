"""Turning one expand message into one case message per test case (spec §S5).

The API only queues a single expand message, so POST /runs stays fast whatever the suite
size; this handler does the fan-out, a page of cases at a time.
"""

from core.access import resolve_owner
from core.jobqueue import SEND_BATCH_MAX, case_message

RESEND_ATTEMPTS = 3  # extra tries for entries SQS reports as failed


def _send(queue, bodies):
    pending = queue.send_batch(bodies)
    for _ in range(RESEND_ATTEMPTS):
        if not pending:
            return
        pending = queue.send_batch(pending)
    if pending:
        # Raising leaves the expand message on the queue, so it is redelivered and this
        # page is sent again from the saved cursor.
        raise RuntimeError(f"could not send {len(pending)} case messages after {RESEND_ATTEMPTS} resends")


def handle_expand(msg, *, store, queue, page_size=100):
    owner, run_id = msg.body["owner"], msg.body["runId"]

    run = store.get_run(owner, run_id)
    if run is None or run["expanded"] >= run["total"]:
        return "delete"  # a missing run can never succeed; a finished expansion has nothing left

    # A pub_ suite's cases live under "public"; the run, its messages and results stay under owner.
    suite_owner = resolve_owner(owner, run["suiteId"], write=False)
    expanded = run["expanded"]
    cursor = run.get("expandCursor")  # the API doesn't write it, so a new run has none
    while True:
        page, next_cursor = store.query_cases(suite_owner, run["suiteId"], run["suiteVersion"], cursor, page_size)
        bodies = [case_message(owner, run_id, case["caseId"]) for case in page]
        for start in range(0, len(bodies), SEND_BATCH_MAX):
            _send(queue, bodies[start:start + SEND_BATCH_MAX])
        expanded += len(page)
        # Saved per page: a worker that dies later resumes here instead of starting again.
        # A crash between the send and this save resends the page, which is safe because
        # a duplicate case message is counted once by the result transaction (Task 8).
        store.record_expansion(owner, run_id, expanded, next_cursor)
        if next_cursor is None:
            return "delete"
        cursor = next_cursor
