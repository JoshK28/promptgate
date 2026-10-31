"""Local end-to-end check (Task 16, gate G1): the real API, worker and CLI code in one
process, over the in-memory fakes from tests/fakes.py.

Local only, no AWS. Nothing here builds a boto3 client: the store, queue, cache and
model are fakes, the CLI's sign-in is a stub, and the API accepts only the local
`local-<name>` tokens defined below. The scenario uses a fake clock; --serve uses
the real clock for the browser check. Run: .\\.venv\\Scripts\\python scripts\\local_e2e.py
"""

import argparse
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import re
import sys
import threading
import time
from unittest.mock import patch

# Run as a file, Python puts scripts/ on the path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
import httpx  # noqa: E402

from api.app import AppDeps, create_app  # noqa: E402
from api.auth import AuthError  # noqa: E402
from cli import main as cli_main  # noqa: E402
from cli.session import Session  # noqa: E402
from core.csv_import import parse_suite_csv  # noqa: E402
from core.gate import gate_exit_code  # noqa: E402
from core.samples import PUBLIC_OWNER, SAMPLE_SUITES, ensure_sample_suites  # noqa: E402
from core.settings import ModelInfo, Settings  # noqa: E402
from tests.fakes import InMemoryCache, InMemoryClock, InMemoryQueue, InMemoryStore  # noqa: E402
from worker.bedrock import ModelReply  # noqa: E402
from worker.loop import Deps, process_message  # noqa: E402

MODEL_ID = "nvidia.nemotron-super-3-120b"  # the first allowed model in plan §2
LOCAL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_RECEIVES = 10_000  # drain() stops here instead of looping forever on a stuck message


def local_settings():
    # The same fields load_settings fills from the environment and Parameter Store,
    # with placeholder IDs: no table, queue, Memcached or user pool is ever contacted.
    return Settings(
        table_name="local-table",
        queue_url="local-queue",
        memcached_endpoint="127.0.0.1:11211",
        user_pool_id="local-pool",
        client_id="local-client",
        region="ap-southeast-2",
        allowed_models=[ModelInfo(MODEL_ID, True)],
        worker_concurrency=1,
        run_deadline_s=1800,
        cache_ttl_s=604800,
    )


def local_verifier(token):
    """Accepts `local-<name>` and returns {"sub": name}. Built only in this script, never in
    api/main.py, so the deployed API can't accept a fake token."""
    name = token.removeprefix("local-")
    # `public` would make the caller the owner of the read-only sample suites.
    if not token.startswith("local-") or not LOCAL_NAME.fullmatch(name) or name == PUBLIC_OWNER:
        raise AuthError("Not a local token.")
    return {"sub": name}


class CapitalsModel:
    """Stands in for Bedrock: answers each pub_capitals question with its expected city,
    found by the case input inside the rendered prompt."""

    def __init__(self):
        _, path = SAMPLE_SUITES["pub_capitals"]
        cases = parse_suite_csv(path.read_text(encoding="utf-8"))
        self.answers = {case["input"]: case["expected"] for case in cases}
        self.calls = 0

    def converse(self, request):
        self.calls += 1
        prompt = request["messages"][0]["content"][0]["text"]
        text = next((city for question, city in self.answers.items() if question in prompt), "I don't know.")
        return ModelReply(text=text, stop_reason="end_turn", tokens_in=len(prompt.split()), tokens_out=1, retries=0)


class FailingModel:
    """A model call that always fails, like a Bedrock fault that outlasts boto3's retries."""

    def __init__(self):
        self.calls = 0

    def converse(self, request):
        self.calls += 1
        raise RuntimeError("simulated model failure")


class LocalStack:
    """One process holding what ECS runs as two services: the API (as a TestClient) and the
    worker (as drain(), calling the same process_message the worker loop calls)."""

    def __init__(self, clock=None):
        self.clock = InMemoryClock(1_000_000.0) if clock is None else clock
        self.store = InMemoryStore(clock=self.clock)
        self.queue = InMemoryQueue()
        self.cache = InMemoryCache()
        self.settings = local_settings()
        ensure_sample_suites(self.store)  # what api/main.py does at start-up (DV11)
        self.app = create_app(AppDeps(
            store=self.store, queue=self.queue, settings=self.settings,
            verify=local_verifier, clock=self.clock, auth_mode="local",
        ))
        self.client = TestClient(self.app)
        # The same fields worker/main.py fills, with fakes in place of the boto3 clients.
        self.deps = Deps(
            store=self.store, queue=self.queue, cache=self.cache, model=CapitalsModel(),
            settings=self.settings, clock=self.clock,
        )

    def use_model(self, model):
        self.deps = replace(self.deps, model=model)  # Deps is frozen

    def drain(self):
        """Process queued messages until none is left. A message the handler leaves for a
        retry comes straight back with its receive count up by one (InMemoryQueue)."""
        for _ in range(MAX_RECEIVES):
            messages = self.queue.receive(wait_s=0)
            if not messages:
                return
            for msg in messages:
                process_message(msg, self.deps)
        raise RuntimeError(f"queue still not empty after {MAX_RECEIVES} receives")

    def api(self, method, path, user="alice", **kwargs):
        return self.client.request(method, path, headers={"Authorization": f"Bearer local-{user}"}, **kwargs)

    def _forward(self, request):
        # Drain first: `cli run` polls until the run finishes, and in one process nothing
        # else runs the worker, so without this the poll would never end.
        self.drain()
        headers = {name: request.headers[name] for name in ("authorization", "content-type") if name in request.headers}
        response = self.client.request(
            request.method, request.url.raw_path.decode("ascii"), content=request.content, headers=headers,
        )
        return httpx.Response(
            response.status_code, content=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json")},
        )

    def cli(self, *argv):
        """Runs cli.main.main(argv) against this process: the real Session over an in-process
        transport, with a sign-in stub in place of Cognito. Returns (exit code, stdout)."""

        def stub_login(**kwargs):
            return {"AuthenticationResult": {"IdToken": "local-alice", "RefreshToken": "local"}}

        def session(base_url, client_id, region):
            return Session(
                base_url, client_id, region,
                transport=httpx.MockTransport(self._forward), login_fn=stub_login,
            )

        env = {
            "PROMPTGATE_URL": "http://promptgate.local",
            "PROMPTGATE_CLIENT_ID": "local-client",
            # Set, so Session keeps tokens in memory and writes no token file.
            "PROMPTGATE_USERNAME": "alice",
            "PROMPTGATE_PASSWORD": "local-only",
        }
        output = io.StringIO()
        with patch.dict(os.environ, env), patch.object(cli_main, "Session", session), \
                patch.object(cli_main.time, "sleep", lambda seconds: None), redirect_stdout(output):
            code = cli_main.main(list(argv))
        print(output.getvalue(), end="")  # into the log, with the API and worker lines
        return code, output.getvalue()


def check(condition, message):
    # Not `assert`: python -O would skip it.
    if not condition:
        raise AssertionError(message)


def cli_run_id(output):
    match = re.search(r"^Run (r_\w+):", output, re.MULTILINE)
    check(match is not None, f"no run ID in the CLI output: {output!r}")
    return match.group(1)


def scenario(stack, say):
    # 1. Samples seeded at start-up; alice creates a config.
    suites = {suite["suiteId"]: suite for suite in stack.api("GET", "/api/suites").json()}
    check("pub_capitals" in suites, "pub_capitals is not seeded")
    total = suites["pub_capitals"]["caseCount"]
    response = stack.api("POST", "/api/configs", json={
        "name": "Capitals, one word", "modelId": MODEL_ID, "template": "{{input}}",
    })
    check(response.status_code == 201, f"create config gave HTTP {response.status_code}")
    cfg_id = response.json()["configId"]
    say(f"1. pub_capitals seeded ({total} cases); alice created config {cfg_id} v1")

    def start(mode, key):
        response = stack.api("POST", "/api/runs", json={
            "configId": cfg_id, "configVersion": 1, "suiteId": "pub_capitals", "suiteVersion": 1,
            "mode": mode, "idempotencyKey": key,
        })
        check(response.status_code == 202, f"POST /api/runs ({mode}) gave HTTP {response.status_code}")
        return response.json()["runId"]

    def run(run_id):
        return stack.api("GET", f"/api/runs/{run_id}").json()

    # 2. A fresh run, then the worker drains the queue.
    fresh_id = start("fresh", "e2e-fresh")
    stack.drain()
    say(f"2. fresh run {fresh_id} started (202) and the queue drained")

    # 3. Every case called the model.
    fresh = run(fresh_id)
    check(fresh["status"] == "COMPLETE", f"fresh run is {fresh['status']}")
    check(fresh["freshCalls"] == total, f"freshCalls {fresh['freshCalls']} != total {total}")
    say(f"3. fresh run COMPLETE: freshCalls={fresh['freshCalls']} == total, passed={fresh['passed']}")

    # 4. A replay run is answered from the cache, with no model calls.
    calls_before = stack.deps.model.calls
    replay_id = start("replay", "e2e-replay")
    stack.drain()
    replay = run(replay_id)
    check(replay["status"] == "COMPLETE", f"replay run is {replay['status']}")
    check(replay["cacheHits"] == total, f"cacheHits {replay['cacheHits']} != total {total}")
    check(stack.deps.model.calls == calls_before, "the replay run called the model")
    say(f"4. replay run {replay_id} COMPLETE: cacheHits={replay['cacheHits']} == total, no model calls")

    # 5. The fresh run becomes the baseline.
    response = stack.api("PUT", f"/api/runs/{fresh_id}/baseline")
    check(response.status_code == 200, f"PUT baseline gave HTTP {response.status_code}")
    say(f"5. baseline set to {fresh_id}")

    # 6. The CLI gate on a new run passes.
    code, output = stack.cli("run", "--config", f"{cfg_id}@1", "--suite", "pub_capitals@1", "--min-pass", "0.9")
    gated = run(cli_run_id(output))
    check(code == 0, f"CLI gate exit {code}, expected 0")
    check(gated["baselineRunId"] == fresh_id, "the CLI run did not pick up the baseline")
    say(f"6. cli run {gated['runId']} ({gated['effectiveMode']}): exit 0, baselineRate={gated['baselineRate']}")

    # 7. A model that always fails: each case is retried up to its 4th receive, then
    # recorded as ERROR, so the run still completes and the gate can't decide (RF4).
    failing = FailingModel()
    stack.use_model(failing)
    code, output = stack.cli(
        "run", "--config", f"{cfg_id}@1", "--suite", "pub_capitals@1", "--min-pass", "0.9", "--mode", "fresh",
    )
    failed = run(cli_run_id(output))
    results = stack.api("GET", f"/api/runs/{failed['runId']}/results").json()["items"]
    check(failed["status"] == "COMPLETE", f"failing run is {failed['status']}")
    check(failed["errored"] == total, f"errored {failed['errored']} != total {total}")
    check(all(r["verdict"] == "ERROR" and r["attempts"] == 4 for r in results), "results are not ERROR after 4 receives")
    check(failing.calls == 4 * total, f"model called {failing.calls} times, expected {4 * total}")
    check(code == 2, f"CLI gate exit {code}, expected 2")
    say(f"7. failing model, fresh run {failed['runId']}: COMPLETE with {failed['errored']} ERROR after 4 receives each; exit 2")

    # 8. A run whose deadline passes before the worker reaches it stays INCOMPLETE (M1).
    stack.use_model(CapitalsModel())
    late_id = start("fresh", "e2e-late")
    stack.clock.advance(stack.settings.run_deadline_s + 1)
    stack.drain()
    late = run(late_id)
    code, reason = gate_exit_code(
        status=late["status"], total=late["total"], passed=late["passed"], errored=late["errored"],
        min_pass=0.9, baseline_rate=late["baselineRate"],
    )
    check(late["status"] == "INCOMPLETE", f"late run is {late['status']}")
    check(late["done"] == 0, f"late run has {late['done']} results written after its deadline")
    check(code == 2, f"gate exit {code}, expected 2")
    say(f"8. late run {late_id}: deadline passed, drained, still INCOMPLETE; gate exit 2 ({reason})")

    say("E2E OK")


def serve():
    """Serve the local dashboard and process its queued work on a background thread."""
    import uvicorn  # already pinned; no AWS startup code from api/main.py

    stack = LocalStack(clock=time.time)
    lock = threading.Lock()
    stop = threading.Event()

    async def locked_app(scope, receive, send):
        if scope["type"] != "http":
            await stack.app(scope, receive, send)
            return
        # A blocking acquire would freeze the event loop while another request
        # holds the lock across an await. Cancellation while waiting owns no lock.
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            await stack.app(scope, receive, send)
        finally:
            lock.release()

    def work():
        while not stop.is_set():
            # The fake queue's receive also reads/mutates shared state. It returns
            # at most one message, so this lock covers one process_message only.
            with lock:
                messages = stack.queue.receive(wait_s=0)
                for msg in messages:
                    process_message(msg, stack.deps)
            if not messages:
                stop.wait(0.05)

    worker = threading.Thread(target=work, name="local-worker")
    worker.start()
    try:
        print("Local only, no AWS. Sign in with a local name; data resets on restart.", flush=True)
        uvicorn.run(locked_app, host="127.0.0.1", port=3000)
    finally:
        stop.set()
        worker.join()
        stack.client.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local end-to-end check; no AWS.")
    parser.add_argument("--serve", action="store_true", help="serve the local dashboard on 127.0.0.1:3000")
    args = parser.parse_args(argv)
    if args.serve:
        return serve()

    real_stdout = sys.stdout

    def say(line):
        print(line, file=real_stdout, flush=True)

    # API, worker and CLI log lines go to this buffer, so the output is the nine step
    # lines; if a step fails, the buffer goes to stderr so nothing is hidden.
    log = io.StringIO()
    try:
        with redirect_stdout(log):
            scenario(LocalStack(), say)
    except Exception:
        sys.stderr.write(log.getvalue())
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
