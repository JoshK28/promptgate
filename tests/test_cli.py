"""Task 14 CLI contracts. Local only, no AWS or real HTTP connections."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

import httpx


AWS_FACTORIES = (
    "boto3.client",
    "boto3.resource",
    "boto3.session.Session.client",
    "boto3.session.Session.resource",
)

# Importing CLI modules must not create AWS clients either.
with ExitStack() as import_guards:
    for factory in AWS_FACTORIES:
        import_guards.enter_context(
            patch(factory, side_effect=AssertionError("Local only, no AWS."))
        )
    from cli import main as cli_main
    from cli.session import Session


ID_TOKEN = "fake-id-token-not-a-secret"
ACCESS_TOKEN = "fake-access-token-not-a-secret"
REFRESH_TOKEN = "fake-refresh-token-not-a-secret"
NEW_ID_TOKEN = "fake-refreshed-id-token-not-a-secret"
RUN_ARGS = [
    "run", "--config", "cfg_one@1", "--suite", "suite_one@1",
    "--min-pass", "0.9",
]


def run_record(run_id="r_one", **overrides):
    return {
        "runId": run_id,
        "cfgId": "cfg_one",
        "configVersion": 1,
        "suiteId": "suite_one",
        "suiteVersion": 1,
        "status": "COMPLETE",
        "total": 10,
        "done": 10,
        "passed": 10,
        "errored": 0,
        "passRate": 1.0,
        "baselineRunId": None,
        "baselineRate": None,
        **overrides,
    }


class CliTests(unittest.TestCase):
    def setUp(self):
        for factory in AWS_FACTORIES:
            self.enterContext(
                patch(factory, side_effect=AssertionError("Local only, no AWS."))
            )
        self.token_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.token_path = self.token_dir / "tokens.json"
        self.enterContext(patch.dict(os.environ, {
            "PROMPTGATE_URL": "https://promptgate.test",
            "PROMPTGATE_CLIENT_ID": "test-public-client",
            "PROMPTGATE_REGION": "ap-southeast-2",
            "PROMPTGATE_USERNAME": "alice@example.test",
            "PROMPTGATE_PASSWORD": "fake-test-password",
        }, clear=True))
        self.script = []
        self.requests = []
        self.auth_calls = []
        self.transport = httpx.MockTransport(self.respond)
        self.enterContext(patch.object(cli_main, "Session", self.make_session))
        self.enterContext(patch("time.sleep", return_value=None))

    def make_session(self, *args, **kwargs):
        # Keep Session's real login/refresh/persistence behavior under test.
        kwargs.update(
            token_path=self.token_path,
            transport=self.transport,
            login_fn=self.authenticate,
        )
        return Session(*args, **kwargs)

    def authenticate(self, **request):
        """Stand in for initiate_auth, including its refresh response shape."""
        self.auth_calls.append(request)
        self.assertEqual(request["ClientId"], "test-public-client")
        if request["AuthFlow"] == "USER_PASSWORD_AUTH":
            self.assertEqual(request["AuthParameters"], {
                "USERNAME": "alice@example.test", "PASSWORD": "fake-test-password",
            })
            tokens = {
                "IdToken": ID_TOKEN,
                "AccessToken": ACCESS_TOKEN,
                "RefreshToken": REFRESH_TOKEN,
            }
        else:
            self.assertEqual(request["AuthFlow"], "REFRESH_TOKEN_AUTH")
            self.assertEqual(request["AuthParameters"], {"REFRESH_TOKEN": REFRESH_TOKEN})
            # Cognito does not return another refresh token in this flow.
            tokens = {"IdToken": NEW_ID_TOKEN, "AccessToken": ACCESS_TOKEN}
        return {"AuthenticationResult": {
            **tokens, "ExpiresIn": 3600, "TokenType": "Bearer",
        }}

    def respond(self, request):
        self.requests.append(request)
        self.assertTrue(self.script, f"Unexpected request: {request.method} {request.url.path}")
        method, path, status, body = self.script.pop(0)
        self.assertEqual(request.method, method)
        self.assertEqual(request.url.raw_path.decode(), path)
        self.assertIn(request.headers.get("Authorization"), (
            f"Bearer {ID_TOKEN}", f"Bearer {NEW_ID_TOKEN}",
        ))
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)

    def queue_run(self, **overrides):
        self.script.extend([
            ("POST", "/api/runs", 202, {"runId": "r_one"}),
            ("GET", "/api/runs/r_one", 200, run_record(**overrides)),
        ])

    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main.main(argv)
        self.assertEqual(self.script, [], "The command did not finish its scripted requests.")
        return code, stdout.getvalue(), stderr.getvalue()

    def test_run_exit_0(self):
        self.queue_run()

        code, stdout, _ = self.invoke(RUN_ARGS)

        self.assertEqual(code, 0)
        self.assertIn("no baseline", stdout.lower())
        body = json.loads(self.requests[0].content)
        self.assertEqual(set(body), {
            "configId", "configVersion", "suiteId", "suiteVersion", "mode", "idempotencyKey",
        })
        self.assertEqual({key: value for key, value in body.items() if key != "idempotencyKey"}, {
            "configId": "cfg_one", "configVersion": 1,
            "suiteId": "suite_one", "suiteVersion": 1, "mode": "auto",
        })
        self.assertEqual(UUID(body["idempotencyKey"]).version, 4)

    def test_run_exit_1(self):
        self.queue_run(passed=8, passRate=0.8)

        code, _, _ = self.invoke(RUN_ARGS)

        self.assertEqual(code, 1)

    def test_run_exit_2_incomplete(self):
        self.queue_run(status="INCOMPLETE", done=3, passed=3, passRate=0.3)

        code, _, _ = self.invoke(RUN_ARGS)

        self.assertEqual(code, 2)

    def test_network_error_exit_2(self):
        self.script = [("POST", "/api/runs", None, httpx.ConnectError("Connection refused"))]

        code, _, _ = self.invoke(RUN_ARGS)

        self.assertEqual(code, 2)

    def test_refresh_on_401_once(self):
        for second_status, expected_code in ((200, 0), (401, 2)):
            with self.subTest(second_status=second_status):
                self.requests.clear()
                self.auth_calls.clear()
                self.script = [
                    ("POST", "/api/runs", 202, {"runId": "r_one"}),
                    ("GET", "/api/runs/r_one", 401, {"detail": "Unauthorized"}),
                    ("GET", "/api/runs/r_one", second_status,
                     run_record() if second_status == 200 else {"detail": "Unauthorized"}),
                ]

                code, _, _ = self.invoke(RUN_ARGS)

                self.assertEqual(code, expected_code)
                self.assertEqual([call["AuthFlow"] for call in self.auth_calls], [
                    "USER_PASSWORD_AUTH", "REFRESH_TOKEN_AUTH",
                ])
                self.assertEqual([request.headers["Authorization"] for request in self.requests], [
                    f"Bearer {ID_TOKEN}", f"Bearer {ID_TOKEN}", f"Bearer {NEW_ID_TOKEN}",
                ])

    def test_compare_refuses_different_suite_version(self):
        self.script = [
            ("GET", "/api/runs/r_a", 200, run_record("r_a", suiteVersion=1)),
            ("GET", "/api/runs/r_b", 200, run_record("r_b", suiteVersion=2)),
        ]

        code, stdout, stderr = self.invoke(["compare", "r_a", "r_b"])

        self.assertEqual(code, 2)
        self.assertIn("suite", (stdout + stderr).lower())

    def test_compare_prints_changed_only(self):
        self.script = [
            ("GET", "/api/runs/r_a", 200, run_record("r_a")),
            ("GET", "/api/runs/r_b", 200, run_record("r_b")),
            ("GET", "/api/runs/r_a/results", 200, {
                "items": [{"caseId": "0001", "passed": True}], "cursor": "page_a",
            }),
            ("GET", "/api/runs/r_a/results?cursor=page_a", 200, {
                "items": [{"caseId": "0002", "passed": True}], "cursor": None,
            }),
            ("GET", "/api/runs/r_b/results", 200, {
                "items": [{"caseId": "0002", "passed": False}], "cursor": "page_b",
            }),
            ("GET", "/api/runs/r_b/results?cursor=page_b", 200, {
                "items": [{"caseId": "0001", "passed": True}], "cursor": None,
            }),
        ]

        code, stdout, _ = self.invoke(["compare", "r_a", "r_b"])

        self.assertEqual(code, 0)
        rows = [line for line in stdout.splitlines() if line.strip().startswith("000")]
        self.assertEqual(len(rows), 1)
        self.assertEqual([cell.strip() for cell in rows[0].split("|")], ["0002", "PASS", "FAIL"])
        self.assertNotIn("0001", stdout)

    def test_tokens_never_printed(self):
        for network_error in (False, True):
            with self.subTest(network_error=network_error):
                self.queue_run()
                if network_error:
                    # Even an exception containing token values must be safe to report.
                    self.script[-1] = (
                        "GET", "/api/runs/r_one", None,
                        httpx.ConnectError(f"Failed with {ID_TOKEN} {ACCESS_TOKEN} {REFRESH_TOKEN}"),
                    )

                code, stdout, stderr = self.invoke(RUN_ARGS)

                self.assertEqual(code, 2 if network_error else 0)
                for token in (ID_TOKEN, ACCESS_TOKEN, REFRESH_TOKEN, NEW_ID_TOKEN):
                    self.assertNotIn(token, stdout + stderr)

    def test_post_retry_same_key_on_503(self):
        self.script = [
            ("POST", "/api/runs", 503, {"runId": "r_one", "retry": True}),
            ("POST", "/api/runs", 202, {"runId": "r_one"}),
            ("GET", "/api/runs/r_one", 200, run_record()),
        ]

        code, _, _ = self.invoke(RUN_ARGS)

        self.assertEqual(code, 0)
        bodies = [json.loads(request.content) for request in self.requests if request.method == "POST"]
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(UUID(bodies[0]["idempotencyKey"]).version, 4)

    def test_ci_env_login_writes_no_token_file(self):
        self.queue_run()

        code, _, _ = self.invoke(RUN_ARGS)

        self.assertEqual(code, 0)
        self.assertEqual([call["AuthFlow"] for call in self.auth_calls], ["USER_PASSWORD_AUTH"])
        self.assertFalse(self.token_path.exists())
        self.assertEqual(list(self.token_dir.iterdir()), [])

    def test_prompt_file_unchanged_reuses_version(self):
        template = "Answer {{input}} briefly.\n"
        prompt_file = self.token_dir / "prompt.txt"
        prompt_file.write_text(template, encoding="utf-8")
        self.script = [
            ("GET", "/api/configs/cfg_one/versions/1", 200, {
                "cfgId": "cfg_one", "version": 1, "created": 0,
                "name": "Test config", "modelId": "nvidia.nemotron-super-3-120b",
                "system": "", "template": template,
                "inferenceConfig": {
                    "maxTokens": 512, "temperature": 0, "topP": 1, "stopSequences": [],
                },
                "additionalModelRequestFields": None,
            }),
        ]
        self.queue_run()

        code, _, _ = self.invoke(RUN_ARGS + ["--prompt-file", str(prompt_file)])

        self.assertEqual(code, 0)
        posts = [request for request in self.requests if request.method == "POST"]
        self.assertEqual([request.url.path for request in posts], ["/api/runs"])
        body = json.loads(posts[0].content)
        self.assertEqual(body["configVersion"], 1)
        self.assertEqual(body["mode"], "auto")


if __name__ == "__main__":
    unittest.main()
