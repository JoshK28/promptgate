"""Tests for the Bedrock wrapper and the fail-open cache (Task 7).

Local only, no AWS: the Converse client and Memcached are small stubs, so no boto3
client or network connection is created here.
"""

import unittest

from worker.bedrock import BedrockModel, build_converse_request
from worker.cache import FailOpenCache

SNAPSHOT = {
    "modelId": "nvidia.nemotron-super-3-120b",
    "system": "Answer in one word.",
    "template": "Q: {{input}}",
    "inferenceConfig": {"maxTokens": 256, "temperature": 0.7, "topP": 0.9, "stopSequences": ["END"]},
}


class StubClient:
    """Stands in for boto3's bedrock-runtime client; returns the response it was given."""

    def __init__(self, response):
        self.response = response
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        return self.response


def converse_response(content, stop_reason="end_turn", tokens=(12, 3), retries=0):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1], "totalTokens": sum(tokens)},
        "ResponseMetadata": {"RetryAttempts": retries},
    }


class BrokenCache:
    """A Memcached that is down: every call raises."""

    def get(self, key):
        raise ConnectionError("memcached is down")

    def set(self, key, value, ttl):
        raise ConnectionError("memcached is down")


class BuildRequestTests(unittest.TestCase):
    def test_request_shape(self):
        request = build_converse_request(SNAPSHOT, "Q: What is 2+2?")

        self.assertEqual(request["modelId"], "nvidia.nemotron-super-3-120b")
        self.assertEqual(request["messages"], [{"role": "user", "content": [{"text": "Q: What is 2+2?"}]}])
        self.assertEqual(request["inferenceConfig"]["maxTokens"], 256)
        self.assertEqual(request["inferenceConfig"]["temperature"], 0.7)
        self.assertEqual(request["inferenceConfig"]["topP"], 0.9)
        self.assertEqual(request["inferenceConfig"]["stopSequences"], ["END"])


class ModelReplyTests(unittest.TestCase):
    def test_reply_parsing(self):
        client = StubClient(converse_response([{"text": "Four"}, {"text": "."}], tokens=(12, 3), retries=1))

        reply = BedrockModel(client).converse(build_converse_request(SNAPSHOT, "Q: 2+2?"))

        self.assertEqual(reply.text, "Four.")
        self.assertEqual(reply.stop_reason, "end_turn")
        self.assertEqual((reply.tokens_in, reply.tokens_out), (12, 3))
        self.assertEqual(reply.retries, 1)

    def test_empty_content_reply(self):
        # RF2: a model that stops at max_tokens can return no content blocks at all. That is
        # an empty output to score, not a crash.
        client = StubClient(converse_response([], stop_reason="max_tokens", tokens=(12, 0)))

        reply = BedrockModel(client).converse(build_converse_request(SNAPSHOT, "Q: 2+2?"))

        self.assertEqual(reply.text, "")
        self.assertEqual(reply.stop_reason, "max_tokens")


class FailOpenCacheTests(unittest.TestCase):
    def test_fail_open_get_returns_none_and_reports(self):
        errors = []
        cache = FailOpenCache(BrokenCache(), errors.append)

        self.assertIsNone(cache.get("pg:abc"))  # a miss, so the case still runs the model

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConnectionError)

    def test_fail_open_set_swallows(self):
        errors = []
        cache = FailOpenCache(BrokenCache(), errors.append)

        cache.set("pg:abc", b"reply", 604800)  # must not raise

        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
