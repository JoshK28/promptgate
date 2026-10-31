"""The Bedrock Converse call, wrapped so the rest of the worker sees only model.converse(request)."""

from dataclasses import dataclass


@dataclass
class ModelReply:
    text: str
    stop_reason: str
    tokens_in: int
    tokens_out: int
    retries: int  # attempts boto3 made after the first, from ResponseMetadata.RetryAttempts


def build_converse_request(snapshot: dict, rendered: str) -> dict:
    # Optional fields are left out, not set to empty values: the cache key hashes this
    # request, so one config must always produce byte-identical JSON.
    request = {
        "modelId": snapshot["modelId"],
        "messages": [{"role": "user", "content": [{"text": rendered}]}],
        "inferenceConfig": dict(snapshot["inferenceConfig"]),
    }
    if snapshot.get("system"):
        request["system"] = [{"text": snapshot["system"]}]
    if snapshot.get("additionalModelRequestFields"):
        request["additionalModelRequestFields"] = snapshot["additionalModelRequestFields"]
    return request


class BedrockModel:
    """Takes the client; creates none, so tests pass a stub and no AWS call is made."""

    def __init__(self, client):
        self.client = client

    def converse(self, request: dict) -> ModelReply:
        response = self.client.converse(**request)
        # A reply is a list of content blocks and may hold none (a model that stops at
        # max_tokens), so join the text blocks and accept an empty result (RF2).
        blocks = response["output"]["message"]["content"]
        text = "".join(block["text"] for block in blocks if "text" in block)
        return ModelReply(
            text=text,
            stop_reason=response["stopReason"],
            tokens_in=response["usage"]["inputTokens"],
            tokens_out=response["usage"]["outputTokens"],
            retries=response["ResponseMetadata"].get("RetryAttempts", 0),
        )


def make_bedrock_client(region: str):
    # Imported here so importing this module never needs AWS (the tests rely on that).
    import boto3
    from botocore.config import Config

    # M6: a reply slower than 60 s counts as failed, and 3 attempts with backoff take at
    # most about 190 s, under the queue's 300 s visibility timeout, so a slow call can't
    # make the message reappear while this worker is still on it. Adaptive mode also
    # slows the request rate when Bedrock throttles.
    config = Config(
        read_timeout=60,
        connect_timeout=5,
        retries={"mode": "adaptive", "max_attempts": 3},
    )
    return boto3.client("bedrock-runtime", region_name=region, config=config)
