"""Task 3 contracts: prompt rendering, cache keys, run status and run IDs."""

import unittest

from core.cache_keys import model_cache_key
from core.models import derive_status, new_run_id
from core.render import render_prompt, validate_template

KEY_FIELDS = dict(
    owner="alice",
    model_id="model-a",
    system=[{"text": "Be brief."}],
    messages=[{"role": "user", "content": [{"text": "Capital of France?"}]}],
    inference_config={"maxTokens": 512, "temperature": 0, "topP": 1, "stopSequences": []},
    additional_fields={"top_k": 5},
)


def run(**overrides):
    """A run record as stored: 10 cases, all expanded, nothing finished, deadline 1000."""
    return {"total": 10, "expanded": 10, "done": 0, "errored": 0, "deadline": 1000, **overrides}


class RenderTests(unittest.TestCase):
    def test_template_requires_placeholder(self):
        with self.assertRaises(ValueError):
            validate_template("Answer the question.")
        validate_template("Q: {{input}}")  # a valid template does not raise

    def test_render_single_pass(self):
        # A case's own text is never treated as a placeholder (RF5).
        self.assertEqual(render_prompt("Q: {{input}}", "{{input}} x"), "Q: {{input}} x")


class CacheKeyTests(unittest.TestCase):
    def test_cache_key_changes_with_every_field(self):
        base = model_cache_key(**KEY_FIELDS)
        config = KEY_FIELDS["inference_config"]
        changes = {
            "owner": {"owner": "bob"},
            "model_id": {"model_id": "model-b"},
            "system": {"system": [{"text": "Be formal."}]},
            "messages": {"messages": [{"role": "user", "content": [{"text": "Capital of Spain?"}]}]},
            "maxTokens": {"inference_config": {**config, "maxTokens": 256}},
            "temperature": {"inference_config": {**config, "temperature": 0.5}},
            "topP": {"inference_config": {**config, "topP": 0.9}},
            "stopSequences": {"inference_config": {**config, "stopSequences": ["END"]}},
            "additional_fields": {"additional_fields": {"top_k": 6}},
        }
        for label, change in changes.items():
            with self.subTest(field=label):
                self.assertNotEqual(model_cache_key(**{**KEY_FIELDS, **change}), base)

    def test_cache_key_order_independent(self):
        reordered = {
            **KEY_FIELDS,
            "inference_config": dict(reversed(KEY_FIELDS["inference_config"].items())),
            "messages": [{"content": [{"text": "Capital of France?"}], "role": "user"}],
        }
        self.assertEqual(model_cache_key(**reordered), model_cache_key(**KEY_FIELDS))

    def test_cache_key_length(self):
        key = model_cache_key(**KEY_FIELDS)
        self.assertEqual(len(key), 67)  # "pg:" + 64 hex characters
        self.assertTrue(key.startswith("pg:"))


class StatusTests(unittest.TestCase):
    def test_status_complete(self):
        self.assertEqual(derive_status(run(done=8, errored=2), now=500), "COMPLETE")
        # Complete wins even after the deadline; late writes are refused elsewhere (M1).
        self.assertEqual(derive_status(run(done=8, errored=2), now=2000), "COMPLETE")

    def test_status_incomplete_after_deadline(self):
        self.assertEqual(derive_status(run(done=3), now=1001), "INCOMPLETE")
        self.assertEqual(derive_status(run(expanded=0), now=1001), "INCOMPLETE")  # beats QUEUED
        self.assertEqual(derive_status(run(done=3), now=1000), "RUNNING")  # not yet past it

    def test_status_queued(self):
        self.assertEqual(derive_status(run(expanded=4), now=500), "QUEUED")
        self.assertEqual(derive_status(run(expanded=0), now=500), "QUEUED")

    def test_status_running(self):
        self.assertEqual(derive_status(run(done=3), now=500), "RUNNING")
        self.assertEqual(derive_status(run(), now=500), "RUNNING")  # all expanded, none done
        self.assertEqual(derive_status(run(expanded=4, errored=1), now=500), "RUNNING")


class RunIdTests(unittest.TestCase):
    def test_run_id_deterministic_per_owner(self):
        run_id = new_run_id("alice", "key-1")
        self.assertEqual(run_id, new_run_id("alice", "key-1"))  # a retry gets the same run
        self.assertNotEqual(run_id, new_run_id("bob", "key-1"))
        self.assertNotEqual(run_id, new_run_id("alice", "key-2"))
        self.assertTrue(run_id.startswith("r_"))
        self.assertEqual(len(run_id), 18)  # "r_" + 16 hex characters

    def test_run_id_separator(self):
        # The NUL between owner and key stops ("ab", "c") colliding with ("a", "bc").
        self.assertNotEqual(new_run_id("ab", "c"), new_run_id("a", "bc"))
