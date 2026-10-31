"""Start-up settings: IDs from the environment, tunables from Parameter Store (8.32)."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    tools: bool


@dataclass(frozen=True)
class Settings:
    table_name: str
    queue_url: str
    memcached_endpoint: str
    user_pool_id: str
    client_id: str
    region: str
    allowed_models: list[ModelInfo]
    worker_concurrency: int
    run_deadline_s: int
    cache_ttl_s: int


def load_settings(env: Mapping[str, str], get_parameter: Callable[[str], str]) -> Settings:
    # The environment is read first, so a missing variable fails before any
    # Parameter Store call. `env[...]` raises a KeyError that names the variable.
    prefix = env["PARAM_PREFIX"]

    def parameter(name):
        return get_parameter(f"{prefix}/{name}")

    return Settings(
        table_name=env["TABLE_NAME"],
        queue_url=env["QUEUE_URL"],
        memcached_endpoint=env["MEMCACHED_ENDPOINT"],
        user_pool_id=env["COGNITO_USER_POOL_ID"],
        client_id=env["COGNITO_CLIENT_ID"],
        region=env["AWS_REGION"],
        allowed_models=[
            ModelInfo(m["id"], m["tools"]) for m in json.loads(parameter("allowed_models"))
        ],
        worker_concurrency=int(parameter("worker_concurrency")),
        run_deadline_s=int(parameter("run_deadline_seconds")),
        cache_ttl_s=int(parameter("cache_ttl_seconds")),
    )


def ssm_getter(ssm_client) -> Callable[[str], str]:
    # One name per call, as in practical 8.32. The client is passed in, so importing
    # this module never needs boto3 or AWS.
    def get_parameter(name):
        return ssm_client.get_parameter(Name=name)["Parameter"]["Value"]

    return get_parameter
