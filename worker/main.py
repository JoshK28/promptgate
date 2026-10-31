"""The worker process ECS runs: `python -m worker.main`.

Nothing here runs at import. main() is the only place real AWS clients are built, so
tests and `import worker.main` never need AWS.
"""

import os
import signal
import threading
import time

from core import metrics
from core.jobqueue import SqsQueue
from core.settings import load_settings, ssm_getter
from core.store import DynamoStore
from worker.bedrock import BedrockModel, make_bedrock_client
from worker.cache import FailOpenCache, MemcachedCache
from worker.case import DIMENSIONS
from worker.loop import Deps, worker_thread

HEARTBEAT_S = 60


def cache_error_handler(emit):
    def on_error(exc):
        print(f"cache fault, treated as a miss: {type(exc).__name__}: {exc}", flush=True)
        emit(DIMENSIONS, {"CacheErrors": (1, "Count")})

    return on_error


def heartbeat(stop, emit=metrics.emit, interval_s=HEARTBEAT_S):
    # WorkerAlive = 1 a minute from each worker, so the dashboard's per-minute Sum is
    # roughly the number of running workers. stop.wait doubles as the sleep.
    emit(DIMENSIONS, {"WorkerAlive": (1, "Count")})
    while not stop.wait(interval_s):
        emit(DIMENSIONS, {"WorkerAlive": (1, "Count")})


def main():
    import boto3  # here and not at the top, so importing this module builds no client

    region = os.environ["AWS_REGION"]
    settings = load_settings(os.environ, ssm_getter(boto3.client("ssm", region_name=region)))

    # boto3 clients are thread-safe, so the threads share these three...
    store = DynamoStore(boto3.client("dynamodb", region_name=region), settings.table_name)
    queue = SqsQueue(boto3.client("sqs", region_name=region), settings.queue_url)
    model = BedrockModel(make_bedrock_client(region))

    stop = threading.Event()
    # ECS sends SIGTERM on scale-in. The Dockerfile's exec-form CMD makes Python process 1,
    # so the signal arrives here. SIGINT is the same for Ctrl-C when run locally.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda signum, frame: stop.set())

    threads = []
    for index in range(settings.worker_concurrency):
        # ...but pymemcache's basic client is not, so every thread builds its own.
        cache = FailOpenCache(MemcachedCache(settings.memcached_endpoint), cache_error_handler(metrics.emit))
        deps = Deps(store=store, queue=queue, cache=cache, model=model, settings=settings, clock=time.time)
        thread = threading.Thread(target=worker_thread, args=(deps, stop), name=f"worker-{index}")
        thread.start()
        threads.append(thread)
    threading.Thread(target=heartbeat, args=(stop,), name="heartbeat", daemon=True).start()
    print(f"worker started with {len(threads)} thread(s)", flush=True)

    # Short waits so the signal handler gets to run promptly in the main thread.
    while not stop.wait(1):
        pass
    print("stop requested: finishing the cases in hand", flush=True)
    for thread in threads:
        thread.join()  # each finishes its current message; ECS kills the task at its stop timeout
    print("worker stopped", flush=True)


if __name__ == "__main__":
    main()
