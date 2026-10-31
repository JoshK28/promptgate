"""Handling one case message: ask the cache or the model, score, write the result (spec §S8).

handle_case answers "delete" (finished or hopeless, remove the message) or "retry" (leave
it; SQS redelivers it once the 300 s visibility timeout runs out).
"""

import json
import time

from core import metrics
from core.render import render_prompt
from core.cache_keys import model_cache_key
from core.scorers import score
from core.store import StoreError
from worker.bedrock import build_converse_request

MAX_OUTPUT_BYTES = 32_768  # what is stored per result; scoring uses the full output
MAX_REASON_CHARS = 500
DIMENSIONS = {"Service": "worker"}


def _emit(emit, **values):
    # values are name=(amount, unit), the shape emit() takes.
    emit(DIMENSIONS, values)


def _truncate(text):
    data = text.encode("utf-8")
    if len(data) <= MAX_OUTPUT_BYTES:
        return text, False
    # errors="ignore" drops a multi-byte character cut in half at the boundary.
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


def _from_cache(raw):
    try:
        entry = json.loads(raw)
        return entry["text"]
    except (ValueError, KeyError, TypeError):
        return None  # an unreadable entry is treated as a miss, and the fresh reply overwrites it


def evaluate_case(run, case, *, owner, cache, model, ttl):
    """Returns (result item, counter deltas) for one case. Raises if the model call fails."""
    # The run's own config snapshot, not the latest config, so a run is reproducible (N6).
    config = run["config"]
    request = build_converse_request(config, render_prompt(config["template"], case["input"]))
    key = model_cache_key(
        owner=owner,
        model_id=request["modelId"],
        system=request.get("system"),
        messages=request["messages"],
        inference_config=request["inferenceConfig"],
        additional_fields=request.get("additionalModelRequestFields"),
    )

    started = time.perf_counter()
    text, source = None, "fresh"
    if run["effectiveMode"] == "replay":
        raw = cache.get(key)
        if raw is not None:
            text = _from_cache(raw)
            source = "cache" if text is not None else "fresh"
    tokens_in = tokens_out = attempts = 0
    if text is None:
        reply = model.converse(request)
        text, tokens_in, tokens_out, attempts = reply.text, reply.tokens_in, reply.tokens_out, reply.retries + 1
        # Fresh mode skips the get above but still sets, so the next replay run is fast.
        entry = {"text": text, "stopReason": reply.stop_reason, "tokensIn": tokens_in, "tokensOut": tokens_out}
        cache.set(key, json.dumps(entry).encode("utf-8"), ttl)

    verdict = score(case["scorer"], text, case["expected"], case["pattern"])
    output, truncated = _truncate(text)
    result = {
        "output": output,
        "truncated": truncated,
        "verdict": "PASS" if verdict.passed else "FAIL",
        "reason": verdict.reason,
        "latencyMs": int((time.perf_counter() - started) * 1000),
        "tokensIn": tokens_in,  # tokens spent by this case: 0 when the reply came from the cache
        "tokensOut": tokens_out,
        "source": source,
        "attempts": attempts,
        "scorerVersion": run["scorerVersions"][case["scorer"].removeprefix("ci:")],
    }
    counters = {
        "done": 1,
        "passed": 1 if verdict.passed else 0,
        "freshCalls" if source == "fresh" else "cacheHits": 1,
        "tokensIn": tokens_in,
        "tokensOut": tokens_out,
    }
    return result, counters


def _error_code(exc):
    # botocore's ClientError carries the AWS error code in .response; read it this way so
    # this module doesn't import botocore.
    response = getattr(exc, "response", None)
    return response.get("Error", {}).get("Code") if isinstance(response, dict) else None


def _error_result(run, case, exc, receive_count):
    reason = f"{type(exc).__name__}: {exc}"[:MAX_REASON_CHARS]
    result = {
        "output": "", "truncated": False, "verdict": "ERROR", "reason": reason,
        "latencyMs": 0, "tokensIn": 0, "tokensOut": 0, "source": "none",
        "attempts": receive_count,  # SQS receives here; a normal result counts Bedrock attempts
        "scorerVersion": run["scorerVersions"].get(case["scorer"].removeprefix("ci:"), ""),
    }
    return result, {"errored": 1}


def handle_case(msg, *, store, cache, model, settings, clock, max_receives=4, emit=metrics.emit):
    owner, run_id, case_id = msg.body["owner"], msg.body["runId"], msg.body["caseId"]

    run = store.get_run(owner, run_id)
    if run is None:
        _emit(emit, PoisonMessages=(1, "Count"))  # RF4: a message for a missing run can never succeed
        return "delete"
    if clock() > run["deadline"]:
        # No tokens are spent on a run that can no longer complete (M1).
        _emit(emit, LateCases=(1, "Count"))
        return "delete"
    if store.get_result(owner, run_id, case_id) is not None:
        # A redelivery of finished work. Two copies running at the same instant can still
        # both call the model; the result transaction counts only one of them.
        return "delete"
    case = store.get_case(owner, run["suiteId"], run["suiteVersion"], case_id)
    if case is None:
        _emit(emit, PoisonMessages=(1, "Count"))
        return "delete"

    try:
        result, counters = evaluate_case(
            run, case, owner=owner, cache=cache, model=model, ttl=settings.cache_ttl_s
        )
    except Exception as exc:  # Exception, not a bare except: Ctrl-C and SIGTERM handling stay intact
        # Logged on every failed receive, so a bad config shows in the container log at once,
        # not only as the ERROR result written on the last receive (about 15 min later).
        print(f"case {run_id}/{case_id} receive {msg.receive_count} failed: {type(exc).__name__}: {exc}", flush=True)
        _emit(emit, CaseErrors=(1, "Count"))
        if _error_code(exc) == "ThrottlingException":
            # Throttles that boto3's own retries absorbed never reach here; this counts the
            # ones that outlasted all 3 attempts. Phase 6 uses it to size worker concurrency (M11).
            _emit(emit, Throttles=(1, "Count"))
        if msg.receive_count < max_receives:
            return "retry"
        # Last attempt: record the failure so the run can still finish, and the DLQ is left
        # for messages that cannot even be processed.
        result, counters = _error_result(run, case, exc, msg.receive_count)

    try:
        outcome = store.write_result(owner, run_id, case_id, result, counters, clock())
    except StoreError:
        return "retry"  # throttling or a fault, not a duplicate: try again later (M7)
    if outcome == "expired":
        _emit(emit, LateCases=(1, "Count"))
    elif outcome == "written" and result["verdict"] != "ERROR":
        _emit(
            emit,
            CaseLatencyMs=(result["latencyMs"], "Milliseconds"),
            TokensIn=(counters["tokensIn"], "Count"),
            TokensOut=(counters["tokensOut"], "Count"),
            **({"FreshCalls": (1, "Count")} if result["source"] == "fresh" else {"CacheHits": (1, "Count")}),
        )
    return "delete"  # "written", "duplicate" and "expired" all finish the message
