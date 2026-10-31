"""Task 5 contracts: start-up settings and CloudWatch embedded-metric lines."""

import contextlib
import io
import json
import unittest

from core.metrics import emf_line, emit
from core.settings import ModelInfo, load_settings, ssm_getter

ENV = {
    "TABLE_NAME": "example-a3-promptgate",
    "QUEUE_URL": "https://sqs.ap-southeast-2.amazonaws.com/123456789012/example-a3-jobs",
    "MEMCACHED_ENDPOINT": "cache.example.cfg.apse2.cache.amazonaws.com:11211",
    "COGNITO_USER_POOL_ID": "ap-southeast-2_example",
    "COGNITO_CLIENT_ID": "exampleclientid",
    "AWS_REGION": "ap-southeast-2",
    "PARAM_PREFIX": "/example/a3",
}

PARAMETERS = {
    "/example/a3/allowed_models": '[{"id": "model-a", "tools": true}, {"id": "model-b", "tools": false}]',
    "/example/a3/worker_concurrency": "1",
    "/example/a3/run_deadline_seconds": "1800",
    "/example/a3/cache_ttl_seconds": "604800",
}


class SettingsTests(unittest.TestCase):
    def test_load_settings_reads_four_parameters(self):
        names = []

        def get_parameter(name):
            names.append(name)
            return PARAMETERS[name]

        load_settings(ENV, get_parameter)
        self.assertCountEqual(names, PARAMETERS)

    def test_load_settings_parses_values(self):
        settings = load_settings(ENV, PARAMETERS.__getitem__)
        self.assertEqual(settings.table_name, ENV["TABLE_NAME"])
        self.assertEqual(settings.queue_url, ENV["QUEUE_URL"])
        self.assertEqual(settings.memcached_endpoint, ENV["MEMCACHED_ENDPOINT"])
        self.assertEqual(settings.user_pool_id, ENV["COGNITO_USER_POOL_ID"])
        self.assertEqual(settings.client_id, ENV["COGNITO_CLIENT_ID"])
        self.assertEqual(settings.region, "ap-southeast-2")
        self.assertEqual(
            settings.allowed_models, [ModelInfo("model-a", True), ModelInfo("model-b", False)]
        )
        # Parameter Store holds strings; the numbers must come back as int.
        self.assertEqual(settings.worker_concurrency, 1)
        self.assertEqual(settings.run_deadline_s, 1800)
        self.assertEqual(settings.cache_ttl_s, 604800)

    def test_missing_env_raises_with_name(self):
        for name in ENV:
            with self.subTest(missing=name):
                env = {key: value for key, value in ENV.items() if key != name}
                with self.assertRaises(KeyError) as caught:
                    load_settings(env, PARAMETERS.__getitem__)
                self.assertIn(name, str(caught.exception))

    def test_ssm_getter_reads_one_name(self):
        class FakeSsm:
            def get_parameter(self, **kwargs):
                self.kwargs = kwargs
                return {"Parameter": {"Value": "1800"}}

        ssm = FakeSsm()
        self.assertEqual(ssm_getter(ssm)("/example/a3/run_deadline_seconds"), "1800")
        self.assertEqual(ssm.kwargs, {"Name": "/example/a3/run_deadline_seconds"})


class MetricsTests(unittest.TestCase):
    def test_emf_shape(self):
        line = emf_line(
            "PromptGate",
            {"Service": "worker"},
            {"CaseLatencyMs": (123.5, "Milliseconds"), "Cases": (1, "Count")},
            1_700_000_000_000,
        )
        self.assertNotIn("\n", line)  # CloudWatch reads one JSON object per log line
        doc = json.loads(line)

        self.assertEqual(doc["_aws"]["Timestamp"], 1_700_000_000_000)
        directive = doc["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(directive["Namespace"], "PromptGate")
        self.assertEqual(directive["Dimensions"], [["Service"]])
        self.assertEqual(
            directive["Metrics"],
            [
                {"Name": "CaseLatencyMs", "Unit": "Milliseconds"},
                {"Name": "Cases", "Unit": "Count"},
            ],
        )
        # The dimension and metric values are top-level keys beside "_aws".
        self.assertEqual(doc["Service"], "worker")
        self.assertEqual(doc["CaseLatencyMs"], 123.5)
        self.assertEqual(doc["Cases"], 1)

    def test_emit_prints_one_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            emit({"Service": "worker"}, {"Cases": (1, "Count")})
        printed = out.getvalue()
        self.assertEqual(printed.count("\n"), 1)  # one line, ending in a newline
        self.assertEqual(json.loads(printed)["_aws"]["CloudWatchMetrics"][0]["Namespace"], "PromptGate")
