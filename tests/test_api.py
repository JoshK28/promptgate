"""Task 12 route contracts on in-memory services. Local only, no AWS."""

import unittest

from fastapi.testclient import TestClient

from api.app import AppDeps, create_app
from api.auth import AuthError
from core.samples import ensure_sample_suites
from core.settings import ModelInfo, Settings
from tests.fakes import InMemoryClock, InMemoryQueue, InMemoryStore


ALICE = {"Authorization": "Bearer tok-alice"}
BOB = {"Authorization": "Bearer tok-bob"}
MODEL_ID = "nvidia.nemotron-super-3-120b"
CSV = (
    '\ufeffinput,expected,scorer,pattern\r\n'
    '"France, please",Paris,exact,\r\n'
    '"Japan\ncapital",Tokyo,exact,\r\n'
)


def settings():
    return Settings(
        table_name="local-table",
        queue_url="local-queue",
        memcached_endpoint="127.0.0.1:11211",
        user_pool_id="ap-southeast-2_test",
        client_id="test-client",
        region="ap-southeast-2",
        allowed_models=[ModelInfo(MODEL_ID, True)],
        worker_concurrency=1,
        run_deadline_s=1800,
        cache_ttl_s=604800,
    )


def verify(token):
    if token == "tok-alice":
        return {"sub": "alice"}
    if token == "tok-bob":
        return {"sub": "bob"}
    raise AuthError("Invalid test token.")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.clock = InMemoryClock(1_000_000)
        self.store = InMemoryStore(clock=self.clock)
        self.queue = InMemoryQueue()
        self.settings = settings()
        ensure_sample_suites(self.store)
        self.client = self.enterContext(TestClient(create_app(AppDeps(
            store=self.store,
            queue=self.queue,
            settings=self.settings,
            verify=verify,
            clock=self.clock,
        ))))

    def create_config(self, **overrides):
        body = {
            "name": "Capital answers",
            "modelId": MODEL_ID,
            "system": "Answer in one word.",
            "template": "Q: {{input}}",
        } | overrides
        response = self.client.post("/api/configs", headers=ALICE, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["version"], 1)
        return response.json()

    def create_suite(self):
        response = self.client.post(
            "/api/suites", headers=ALICE | {"Content-Type": "text/csv"}, content=CSV,
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["version"], 1)
        return response.json()

    def run_body(self, *, key="first-run", mode="fresh", config=None):
        config = config or self.create_config()
        suite = self.create_suite()
        return {
            "configId": config["configId"],
            "configVersion": config["version"],
            "suiteId": suite["suiteId"],
            "suiteVersion": suite["version"],
            "mode": mode,
            "idempotencyKey": key,
        }

    def start_run(self, body):
        response = self.client.post("/api/runs", headers=ALICE, json=body)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(set(response.json()), {"runId"})
        return response.json()["runId"]

    def complete_run(self, run_id, *, passed=2, errored=0):
        # Simulate the worker through the Store interface, without model calls.
        self.store.record_expansion("alice", run_id, 2, None)
        for index in range(1, 3):
            is_error = index > 2 - errored
            is_pass = index <= passed and not is_error
            result = {"passed": is_pass, "reason": "test result", "source": "fresh"}
            counters = {
                "done": int(not is_error), "passed": int(is_pass),
                "errored": int(is_error), "freshCalls": 1,
                "cacheHits": 0, "tokensIn": 10, "tokensOut": 1,
            }
            outcome = self.store.write_result(
                "alice", run_id, f"{index:04d}", result, counters, self.clock(),
            )
            self.assertEqual(outcome, "written")

    def test_health_no_auth(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_config_json_public(self):
        response = self.client.get("/config.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "region": "ap-southeast-2", "clientId": "test-client", "authMode": "cognito",
        })
        local_app = create_app(AppDeps(
            store=self.store, queue=self.queue, settings=self.settings,
            verify=verify, clock=self.clock, auth_mode="local",
        ))
        with TestClient(local_app) as client:
            response = client.get("/config.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "region": "ap-southeast-2", "clientId": "test-client", "authMode": "local",
        })

    def test_api_requires_token_401(self):
        for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": "Basic invalid"}):
            with self.subTest(headers=headers):
                response = self.client.get("/api/runs", headers=headers)
                self.assertEqual(response.status_code, 401)

    def test_cross_owner_read_404(self):
        body = self.run_body()
        run_id = self.start_run(body)
        own = self.client.get(f"/api/runs/{run_id}", headers=ALICE)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json()["status"], "QUEUED")
        self.assertEqual(own.json()["passRate"], 0)
        for path in (
            f"/api/runs/{run_id}",
            f"/api/runs/{run_id}/results",
            f"/api/configs/{body['configId']}/versions/1",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=BOB, params={"owner": "alice"})
                self.assertEqual(response.status_code, 404)

    def test_cross_owner_write_404(self):
        cfg_id = self.create_config()["configId"]
        original = self.store.get_config_version("alice", cfg_id, 1)
        response = self.client.post(f"/api/configs/{cfg_id}/versions", headers=BOB, json={
            "name": "Bob's overwrite", "modelId": MODEL_ID, "template": "Changed: {{input}}",
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.get_config_version("alice", cfg_id, 1), original)
        self.assertIsNone(self.store.get_config_version("alice", cfg_id, 2))
        self.assertEqual(self.store.list_configs("bob"), [])

    def test_missing_referenced_version_404(self):
        body = self.run_body() | {"configVersion": 99}
        response = self.client.post("/api/runs", headers=ALICE, json=body)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.list_runs("alice"), [])
        self.assertEqual(self.queue.sent, [])

    def test_two_page_pagination(self):
        run_id = self.start_run(self.run_body())
        self.complete_run(run_id)
        path = f"/api/runs/{run_id}/results"
        first = self.client.get(path, headers=ALICE, params={"limit": 1})
        self.assertEqual(first.status_code, 200)
        self.assertEqual([item["caseId"] for item in first.json()["items"]], ["0001"])
        cursor = first.json()["cursor"]
        self.assertIsInstance(cursor, str)
        self.assertTrue(cursor)
        second = self.client.get(path, headers=ALICE, params={"limit": 1, "cursor": cursor})
        self.assertEqual(second.status_code, 200)
        self.assertEqual([item["caseId"] for item in second.json()["items"]], ["0002"])
        self.assertIsNone(second.json()["cursor"])

    def test_public_suite_readable_not_writable(self):
        for headers in (ALICE, BOB):
            with self.subTest(headers=headers):
                listed = self.client.get("/api/suites", headers=headers)
                self.assertEqual(listed.status_code, 200)
                self.assertIn("pub_capitals", [suite["suiteId"] for suite in listed.json()])
                read = self.client.get("/api/suites/pub_capitals/versions/1/cases", headers=headers)
                self.assertEqual(read.status_code, 200)
                self.assertGreater(len(read.json()["items"]), 0)
                write = self.client.post(
                    "/api/suites/pub_capitals/versions",
                    headers=headers | {"Content-Type": "text/csv"}, content=CSV,
                )
                self.assertEqual(write.status_code, 404)
        self.assertIsNone(self.store.get_suite_version("public", "pub_capitals", 2))

    def test_post_runs_202_one_write_one_message(self):
        config = self.create_config(additionalModelRequestFields={"pg_force_error": True})
        body = self.run_body(config=config)
        snapshot = self.store.get_config_version("alice", body["configId"], 1)
        # A newer config version must not replace the requested version's snapshot.
        newer = self.client.post(
            f"/api/configs/{body['configId']}/versions", headers=ALICE,
            json={"name": "Changed", "modelId": MODEL_ID, "template": "New: {{input}}"},
        )
        self.assertEqual(newer.status_code, 201, newer.text)
        self.assertEqual(newer.json()["version"], 2)
        run_id = self.start_run(body)
        self.assertEqual(len(self.store.list_runs("alice")), 1)
        self.assertEqual(self.queue.sent, [{"type": "expand", "owner": "alice", "runId": run_id}])
        run = self.store.get_run("alice", run_id)
        self.assertEqual(run["config"], snapshot)
        self.assertEqual(run["config"]["additionalModelRequestFields"], {"pg_force_error": True})
        self.assertEqual(run["config"]["inferenceConfig"], {
            "maxTokens": 512, "temperature": 0, "topP": 1, "stopSequences": [],
        })
        self.assertEqual(run["effectiveMode"], "fresh")
        self.assertEqual(run["cfgId"], body["configId"])
        self.assertEqual(run["suiteId"], body["suiteId"])
        self.assertEqual(run["suiteVersion"], 1)
        self.assertEqual(run["scorerVersions"], {
            "exact": "exact-v1", "contains": "contains-v1",
            "not_contains": "not_contains-v1", "regex": "regex-v1",
        })
        self.assertEqual(run["deadline"], 1_001_800)
        self.assertEqual(run["total"], 2)
        for name in ("expanded", "done", "passed", "errored", "freshCalls", "cacheHits", "tokensIn", "tokensOut"):
            self.assertEqual(run[name], 0, name)
        self.assertIsNone(run["baselineRunId"])
        self.assertIsNone(run["baselineRate"])
        self.assertRegex(run["requestHash"], r"^[0-9a-f]{64}$")
        # RF1: Excel BOM/CRLF, quoted comma and embedded newline survive the API.
        self.assertEqual(self.store.get_case("alice", body["suiteId"], 1, "0001")["input"], "France, please")
        self.assertEqual(self.store.get_case("alice", body["suiteId"], 1, "0002")["input"], "Japan\ncapital")

    def test_same_idempotency_key_same_run(self):
        body = self.run_body(mode="auto")
        first = self.start_run(body)
        second = self.start_run(body)
        self.assertEqual(second, first)
        self.assertEqual(len(self.store.list_runs("alice")), 1)
        # The first request creates a fresh pointer; a retry retains its original mode.
        self.assertEqual(self.store.get_run("alice", first)["effectiveMode"], "fresh")

    def test_same_key_different_body_409(self):
        body = self.run_body()
        self.start_run(body)
        response = self.client.post("/api/runs", headers=ALICE, json=body | {"mode": "replay"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.store.list_runs("alice")), 1)
        self.assertEqual(len(self.queue.sent), 1)

    def test_queue_send_failure_503_then_retry_202(self):
        body = self.run_body()
        self.queue.reject_next = 1
        failed = self.client.post("/api/runs", headers=ALICE, json=body)
        self.assertEqual(failed.status_code, 503)
        run_id = failed.json()["runId"]
        self.assertEqual(failed.json(), {"runId": run_id, "retry": True})
        self.assertEqual(len(self.store.list_runs("alice")), 1)
        self.assertEqual(self.queue.sent, [])
        retried_id = self.start_run(body)
        self.assertEqual(retried_id, run_id)
        self.assertEqual(len(self.store.list_runs("alice")), 1)
        self.assertEqual(self.queue.sent, [{"type": "expand", "owner": "alice", "runId": run_id}])

    def test_pointer_write_failure_503_then_retry_202(self):
        # B5 regression: a failed fresh-pointer write gave an uncaught 500 with the run saved
        # but never queued, and clients only retry the same key after a 503.
        body = self.run_body()
        put_pointer = self.store.put_fresh_pointer
        calls = []

        def fail_once(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("DynamoDB throttled")
            put_pointer(*args)

        self.store.put_fresh_pointer = fail_once
        failed = self.client.post("/api/runs", headers=ALICE, json=body)
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(self.queue.sent, [])
        run_id = self.start_run(body)
        self.assertEqual(run_id, failed.json()["runId"])
        self.assertEqual(self.queue.sent, [{"type": "expand", "owner": "alice", "runId": run_id}])
        pointer = self.store.get_fresh_pointer("alice", body["configId"], body["configVersion"],
                                               body["suiteId"], body["suiteVersion"])
        self.assertEqual(pointer["runId"], run_id)

    def test_baseline_fixed_at_creation(self):
        body = self.run_body()
        baseline_a = self.start_run(body)
        self.complete_run(baseline_a, passed=1)
        marked = self.client.put(f"/api/runs/{baseline_a}/baseline", headers=ALICE)
        self.assertEqual(marked.status_code, 200, marked.text)
        target = self.start_run(body | {"idempotencyKey": "target"})
        baseline_b = self.start_run(body | {"idempotencyKey": "new-baseline"})
        self.complete_run(baseline_b)
        marked = self.client.put(f"/api/runs/{baseline_b}/baseline", headers=ALICE)
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(self.store.get_baseline("alice", body["configId"], body["suiteId"], 1), baseline_b)
        target_read = self.client.get(f"/api/runs/{target}", headers=ALICE)
        self.assertEqual(target_read.status_code, 200)
        self.assertEqual(target_read.json()["baselineRunId"], baseline_a)
        self.assertEqual(target_read.json()["baselineRate"], 0.5)

    def test_auto_mode_resolution(self):
        body = self.run_body(mode="auto")
        first = self.start_run(body)
        self.assertEqual(self.store.get_run("alice", first)["effectiveMode"], "fresh")
        second = self.start_run(body | {"idempotencyKey": "within-ttl"})
        self.assertEqual(self.store.get_run("alice", second)["effectiveMode"], "replay")
        self.clock.advance(604801)
        third = self.start_run(body | {"idempotencyKey": "past-ttl"})
        self.assertEqual(self.store.get_run("alice", third)["effectiveMode"], "fresh")

    def test_baseline_rules(self):
        body = self.run_body(mode="replay")
        replay = self.start_run(body)
        self.complete_run(replay)
        response = self.client.put(f"/api/runs/{replay}/baseline", headers=ALICE)
        self.assertEqual(response.status_code, 409)
        fresh_body = body | {"mode": "fresh", "idempotencyKey": "fresh"}
        fresh = self.start_run(fresh_body)
        response = self.client.put(f"/api/runs/{fresh}/baseline", headers=ALICE)
        self.assertEqual(response.status_code, 409)
        self.complete_run(fresh)
        response = self.client.put(f"/api/runs/{fresh}/baseline", headers=ALICE)
        self.assertEqual(response.status_code, 200, response.text)
        errored = self.start_run(fresh_body | {"idempotencyKey": "errored"})
        self.complete_run(errored, passed=1, errored=1)
        response = self.client.put(f"/api/runs/{errored}/baseline", headers=ALICE)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.get_baseline("alice", body["configId"], body["suiteId"], 1), fresh)

    def test_disallowed_model_400(self):
        response = self.client.post("/api/configs", headers=ALICE, json={
            "name": "Disallowed", "modelId": "not-allowed", "template": "Q: {{input}}",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_configs("alice"), [])

    def test_template_without_placeholder_400(self):
        response = self.client.post("/api/configs", headers=ALICE, json={
            "name": "Invalid template", "modelId": MODEL_ID, "template": "Q: input",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list_configs("alice"), [])

    def test_bad_inference_config_400(self):
        bad_values = [
            {"maxToken": 512}, {"maxTokens": 0}, {"maxTokens": 100000},
            {"maxTokens": 1.5}, {"maxTokens": True},
            {"temperature": -0.01}, {"temperature": 1.01},
            {"topP": -0.01}, {"topP": 1.01},
            {"stopSequences": ["a", "b", "c", "d", "e"]},
            {"stopSequences": [123]},
        ]
        for inference in bad_values:
            with self.subTest(inference=inference):
                response = self.client.post("/api/configs", headers=ALICE, json={
                    "name": "Invalid inference", "modelId": MODEL_ID,
                    "template": "Q: {{input}}", "inferenceConfig": inference,
                })
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.list_configs("alice"), [])

    def test_suite_version_keeps_name_by_default(self):
        created = self.client.post(
            "/api/suites", headers=ALICE | {"Content-Type": "text/csv"},
            params={"name": "Capital cities"}, content=CSV,
        )
        self.assertEqual(created.status_code, 201, created.text)
        suite_id = created.json()["suiteId"]
        updated = self.client.post(
            f"/api/suites/{suite_id}/versions",
            headers=ALICE | {"Content-Type": "text/csv"}, content=CSV,
        )
        self.assertEqual(updated.status_code, 201, updated.text)
        self.assertEqual(self.store.get_suite_version("alice", suite_id, 2)["name"], "Capital cities")

    def test_run_reads_hide_internal_fields(self):
        run_id = self.start_run(self.run_body())
        for path in (f"/api/runs/{run_id}", "/api/runs"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=ALICE)
                self.assertEqual(response.status_code, 200)
                run = response.json() if isinstance(response.json(), dict) else response.json()[0]
                self.assertNotIn("requestHash", run)
                self.assertNotIn("expandCursor", run)
                self.assertEqual(run["status"], "QUEUED")
        self.assertIn("requestHash", self.store.get_run("alice", run_id))
        self.assertIn("expandCursor", self.store.get_run("alice", run_id))

    def test_csv_errors_400_with_rows(self):
        response = self.client.post(
            "/api/suites", headers=ALICE | {"Content-Type": "text/csv"},
            content="\ufeffinput,expected,scorer,pattern\r\na,,unknown,\r\nb,,regex,[\r\n",
        )
        self.assertEqual(response.status_code, 400)
        errors = response.json()["errors"]
        self.assertEqual(len(errors), 2)
        self.assertTrue(any(error.startswith("row 2:") for error in errors))
        self.assertTrue(any(error.startswith("row 3:") for error in errors))
        self.assertEqual(self.store.list_suites("alice"), [])


if __name__ == "__main__":
    unittest.main()
