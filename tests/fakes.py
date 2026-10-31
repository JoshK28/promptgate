"""In-memory stand-ins for DynamoDB, SQS, Memcached, Bedrock and the clock.

Every later task is tested on these, so the rules that matter match the real services:
duplicate results, writes after the deadline and a suite that appears only once all of
its cases are stored. Local only, no AWS.
"""

import copy
import time

from core.jobqueue import Message
from core.keys import (
    baseline_sk,
    case_pk,
    case_sk,
    config_sk,
    fresh_sk,
    result_pk,
    result_sk,
    run_sk,
    suite_sk,
    user_pk,
)
from core.store import IdempotencyConflict, StoreError, decode_cursor, encode_cursor


class InMemoryStore:
    def __init__(self, fail_after_case_writes=None, clock=time.time):
        self.items = {}  # (PK, SK) -> plain dict, the way the real table is keyed
        self.fail_after_case_writes = fail_after_case_writes  # the M2 test
        self.case_writes = 0
        self.clock = clock

    # -- helpers --------------------------------------------------------------

    def _get(self, pk, sk):
        return copy.deepcopy(self.items.get((pk, sk)))  # a copy, as a real read would be

    def _next_version(self, pk, prefix):
        versions = [int(sk[len(prefix):]) for (p, sk) in self.items if p == pk and sk.startswith(prefix)]
        return max(versions, default=0) + 1

    def _query(self, pk, prefix, cursor=None, limit=None):
        # Same shape as a DynamoDB Query: sorted by SK, resumed from a LastEvaluatedKey.
        sks = sorted(sk for (p, sk) in self.items if p == pk and sk.startswith(prefix))
        start = decode_cursor(cursor)
        if start:
            # The same check as DynamoStore._query: a cursor must stay inside this partition.
            if start.get("PK") != {"S": pk}:
                raise ValueError("cursor does not belong to this query")
            sks = [sk for sk in sks if sk > start["SK"]["S"]]
        page = sks if limit is None else sks[:limit]
        more = limit is not None and len(sks) > limit
        next_cursor = encode_cursor({"PK": {"S": pk}, "SK": {"S": page[-1]}}) if more else None
        return [self._get(pk, sk) for sk in page], next_cursor

    # -- configs and suites ---------------------------------------------------

    def put_config_version(self, owner, cfg_id, cfg):
        n = self._next_version(user_pk(owner), f"CFG#{cfg_id}#V#")
        self.items[(user_pk(owner), config_sk(cfg_id, n))] = copy.deepcopy(
            cfg | {"cfgId": cfg_id, "version": n, "created": self.clock()}
        )
        return n

    def get_config_version(self, owner, cfg_id, n):
        return self._get(user_pk(owner), config_sk(cfg_id, n))

    def list_configs(self, owner):
        return self._query(user_pk(owner), "CFG#")[0]

    def put_suite_version(self, owner, suite_id, name, cases):
        n = self._next_version(user_pk(owner), f"SUITE#{suite_id}#V#")
        for index, case in enumerate(cases, start=1):
            if self.fail_after_case_writes is not None and self.case_writes >= self.fail_after_case_writes:
                raise StoreError("simulated failure while writing cases")
            self.items[(case_pk(owner, suite_id, n), case_sk(index))] = case | {"caseId": f"{index:04d}"}
            self.case_writes += 1
        # Last (M2): the suite only becomes visible once every case is stored.
        self.items[(user_pk(owner), suite_sk(suite_id, n))] = {
            "suiteId": suite_id, "version": n, "name": name,
            "caseCount": len(cases), "created": self.clock(),
        }
        return n

    def get_suite_version(self, owner, suite_id, n):
        return self._get(user_pk(owner), suite_sk(suite_id, n))

    def list_suites(self, owner):
        return self._query(user_pk(owner), "SUITE#")[0]

    def query_cases(self, owner, suite_id, n, cursor, limit):
        return self._query(case_pk(owner, suite_id, n), "CASE#", cursor, limit)

    def get_case(self, owner, suite_id, n, case_id):
        return self._get(case_pk(owner, suite_id, n), case_sk(int(case_id)))

    # -- runs and results -----------------------------------------------------

    def create_run(self, owner, run):
        key = (user_pk(owner), run_sk(run["runId"]))
        existing = self.items.get(key)
        if existing is None:
            self.items[key] = copy.deepcopy(run)
            return copy.deepcopy(run), True
        if existing["requestHash"] != run["requestHash"]:
            raise IdempotencyConflict(run["runId"])
        return copy.deepcopy(existing), False

    def get_run(self, owner, run_id):
        return self._get(user_pk(owner), run_sk(run_id))

    def list_runs(self, owner):
        return self._query(user_pk(owner), "RUN#")[0]

    def record_expansion(self, owner, run_id, expanded, cursor):
        run = self.items.get((user_pk(owner), run_sk(run_id)))
        if run is None:
            raise StoreError(f"no run {run_id}")
        run["expanded"], run["expandCursor"] = expanded, cursor

    def write_result(self, owner, run_id, case_id, result, counters, now):
        # The same two rules as the real transaction, and in the same order: a
        # duplicate is reported first, then a write after the deadline (M1).
        key = (result_pk(owner, run_id), result_sk(case_id))
        if key in self.items:
            return "duplicate"
        run = self.items.get((user_pk(owner), run_sk(run_id)))
        if run is None or now > run["deadline"]:
            return "expired"
        self.items[key] = copy.deepcopy(result | {"caseId": case_id})
        for name, amount in counters.items():
            run[name] = run.get(name, 0) + amount
        return "written"

    def get_result(self, owner, run_id, case_id):
        return self._get(result_pk(owner, run_id), result_sk(case_id))

    def query_results(self, owner, run_id, cursor, limit):
        return self._query(result_pk(owner, run_id), "RES#", cursor, limit)

    # -- baselines and fresh pointers -----------------------------------------

    def set_baseline(self, owner, run):
        self.items[(user_pk(owner), baseline_sk(run["cfgId"], run["suiteId"], run["suiteVersion"]))] = {
            "runId": run["runId"]
        }

    def get_baseline(self, owner, cfg_id, suite_id, suite_v):
        item = self._get(user_pk(owner), baseline_sk(cfg_id, suite_id, suite_v))
        return item["runId"] if item else None

    def put_fresh_pointer(self, owner, cfg_id, cfg_v, suite_id, suite_v, run_id, created):
        self.items[(user_pk(owner), fresh_sk(cfg_id, cfg_v, suite_id, suite_v))] = {
            "runId": run_id, "created": created
        }

    def get_fresh_pointer(self, owner, cfg_id, cfg_v, suite_id, suite_v):
        return self._get(user_pk(owner), fresh_sk(cfg_id, cfg_v, suite_id, suite_v))


class InMemoryQueue:
    def __init__(self):
        self.sent = []  # every body accepted, in order
        self.reject_next = 0  # how many of the next bodies to refuse, once
        self._pending = {}  # receipt -> [body, receive_count]; removed only by delete()
        self._counter = 0

    def _accept(self, body):
        self.sent.append(body)
        self._counter += 1
        self._pending[f"receipt-{self._counter}"] = [body, 0]

    def send(self, body):
        if self.reject_next:
            self.reject_next -= 1
            raise RuntimeError("simulated send failure")
        self._accept(body)

    def send_batch(self, bodies):
        failed = []
        for body in bodies:
            if self.reject_next:
                self.reject_next -= 1
                failed.append(body)
            else:
                self._accept(body)
        return failed

    def receive(self, wait_s=20):
        # A message that is received but not deleted comes back on the next receive,
        # as if its visibility timeout had run out, with its receive count up by one.
        for receipt, entry in self._pending.items():
            entry[1] += 1
            return [Message(copy.deepcopy(entry[0]), receipt, entry[1])]
        return []

    def delete(self, receipt):
        self._pending.pop(receipt, None)


class InMemoryCache:
    def __init__(self):
        self.data = {}  # key -> bytes
        self.ttls = {}  # key -> ttl as set

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl):
        self.data[key] = value
        self.ttls[key] = ttl


class InMemoryModel:
    def __init__(self, reply=None, error=None):
        self.reply = reply  # what converse() returns; Task 7 supplies a ModelReply
        self.error = error  # an exception to raise instead
        self.calls = 0
        self.requests = []

    def converse(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


class InMemoryClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def set(self, now):
        self.now = now

    def advance(self, seconds):
        self.now += seconds
