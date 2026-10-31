"""Memcached response cache (11.36), and a wrapper that treats cache faults as misses."""

from collections.abc import Callable

from pymemcache.client.base import Client

MAX_VALUE_BYTES = 1_000_000  # Memcached's default item limit is 1 MB


class MemcachedCache:
    """One per worker thread: pymemcache's basic client isn't thread-safe."""

    def __init__(self, endpoint: str):
        # "host:port", the ElastiCache configuration endpoint (MEMCACHED_ENDPOINT).
        host, port = endpoint.rsplit(":", 1)
        # The same arguments as the 11.36 practical. default_noreply=False makes set()
        # wait for the server's answer, so a failed set raises instead of vanishing.
        self.client = Client((host, int(port)), connect_timeout=5, timeout=5, default_noreply=False)

    def get(self, key: str) -> bytes | None:
        return self.client.get(key)

    def set(self, key: str, value: bytes, ttl: int) -> None:
        if len(value) > MAX_VALUE_BYTES:
            return  # too big to store; the case just won't be cached
        self.client.set(key, value, expire=ttl)


class FailOpenCache:
    """The cache only saves time and money, so when it is down a case still runs the
    model: a failed get is a miss and a failed set is skipped. Each fault is reported
    to on_error, which the worker uses to emit the CacheErrors metric."""

    def __init__(self, inner, on_error: Callable[[Exception], None]):
        self.inner = inner
        self.on_error = on_error

    def get(self, key: str) -> bytes | None:
        try:
            return self.inner.get(key)
        except Exception as exc:  # Exception, not a bare except: Ctrl-C still stops the worker
            self.on_error(exc)
            return None

    def set(self, key: str, value: bytes, ttl: int) -> None:
        try:
            self.inner.set(key, value, ttl)
        except Exception as exc:
            self.on_error(exc)
