"""Production API entry point; the importable app stays health-only and AWS-free."""

from fastapi import FastAPI


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    # AWS: production startup reads parameters and seeds DynamoDB. Josh runs it.
    import os
    import time

    import boto3
    import uvicorn

    from api.app import AppDeps, create_app
    from api.auth import JwksKeys, cognito_issuer, verify_id_token
    from core.jobqueue import SqsQueue
    from core.samples import ensure_sample_suites
    from core.settings import load_settings, ssm_getter
    from core.store import DynamoStore

    region = os.environ["AWS_REGION"]
    ssm = boto3.client("ssm", region_name=region)
    settings = load_settings(os.environ, ssm_getter(ssm))
    store = DynamoStore(
        boto3.client("dynamodb", region_name=settings.region),
        settings.table_name,
    )
    queue = SqsQueue(
        boto3.client("sqs", region_name=settings.region), settings.queue_url,
    )
    issuer = cognito_issuer(settings.region, settings.user_pool_id)
    keys = JwksKeys(issuer)

    def verify(token):
        return verify_id_token(
            token, key_for=keys.key_for, issuer=issuer, client_id=settings.client_id,
        )

    ensure_sample_suites(store)
    production_app = create_app(AppDeps(
        store=store, queue=queue, settings=settings, verify=verify, clock=time.time,
    ))
    uvicorn.run(production_app, host="0.0.0.0", port=3000)


if __name__ == "__main__":
    main()
