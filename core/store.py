"""DynamoDB rules for PromptGate: conditional writes, value conversion and cursors.

The builders and helpers are pure. `DynamoStore` at the end takes a boto3 client as an
argument and never creates one, so importing this module needs no AWS. The `Store`
protocol is what `DynamoStore` and the test-only `InMemoryStore` both implement.
"""

import base64
import json
import time
from decimal import Decimal
from typing import Protocol

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

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

BATCH_WRITE_MAX = 25  # DynamoDB's limit for one batch_write_item call
MAX_ATTEMPTS = 5

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


class IdempotencyConflict(Exception):
    """The same run ID was posted again with a different request body (M3)."""


class StoreError(Exception):
    """A storage fault the caller should retry, never drop silently (M7)."""


class Store(Protocol):
    def put_config_version(self, owner: str, cfg_id: str, cfg: dict) -> int: ...
    def get_config_version(self, owner: str, cfg_id: str, n: int) -> dict | None: ...
    def list_configs(self, owner: str) -> list[dict]: ...
    def put_suite_version(self, owner: str, suite_id: str, name: str, cases: list[dict]) -> int: ...
    def get_suite_version(self, owner: str, suite_id: str, n: int) -> dict | None: ...
    def list_suites(self, owner: str) -> list[dict]: ...
    def query_cases(
        self, owner: str, suite_id: str, n: int, cursor: str | None, limit: int
    ) -> tuple[list[dict], str | None]: ...
    def get_case(self, owner: str, suite_id: str, n: int, case_id: str) -> dict | None: ...
    def create_run(self, owner: str, run: dict) -> tuple[dict, bool]: ...
    def get_run(self, owner: str, run_id: str) -> dict | None: ...
    def list_runs(self, owner: str) -> list[dict]: ...
    def record_expansion(self, owner: str, run_id: str, expanded: int, cursor: str | None) -> None: ...
    def write_result(
        self, owner: str, run_id: str, case_id: str, result: dict, counters: dict, now: float
    ) -> str: ...
    def query_results(
        self, owner: str, run_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict], str | None]: ...
    def set_baseline(self, owner: str, run: dict) -> None: ...
    def get_baseline(self, owner: str, cfg_id: str, suite_id: str, suite_v: int) -> str | None: ...
    def put_fresh_pointer(
        self, owner: str, cfg_id: str, cfg_v: int, suite_id: str, suite_v: int, run_id: str, created: float
    ) -> None: ...
    def get_fresh_pointer(
        self, owner: str, cfg_id: str, cfg_v: int, suite_id: str, suite_v: int
    ) -> dict | None: ...


def _decimals(value):
    if isinstance(value, float):
        # str() first: Decimal(0.7) would keep float noise (0.6999...) and be refused.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _decimals(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimals(item) for item in value]
    return value


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def to_dynamo(value):
    # boto3's serializer rejects Python floats (M5), e.g. temperature 0.7.
    return _serializer.serialize(_decimals(value))


def from_dynamo(value):
    return _plain(_deserializer.deserialize(value))


def _without_keys(item):
    # A stored item to a plain dict, minus PK and SK, as the InMemoryStore returns it.
    return {name: value for name, value in from_dynamo({"M": item}).items() if name not in ("PK", "SK")}


def result_transaction(table, owner, run_id, case_id, result, counters, now) -> list[dict]:
    # Two items must change together or not at all, so this is one TransactWriteItems
    # call, not 5.33's single conditional put (DV8).
    add = ", ".join(f"{name} :{name}" for name in counters)
    values = {f":{name}": amount for name, amount in counters.items()} | {":now": now}
    return [
        {
            "Put": {
                "TableName": table,
                "Item": to_dynamo(
                    {"PK": result_pk(owner, run_id), "SK": result_sk(case_id)} | result
                )["M"],
                # A message delivered twice can't write, and so can't count, a second time.
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        },
        {
            "Update": {
                "TableName": table,
                "Key": to_dynamo({"PK": user_pk(owner), "SK": run_sk(run_id)})["M"],
                "UpdateExpression": f"ADD {add}",
                # A result after the deadline is refused (M1): the run was already failed
                # by CI as INCOMPLETE, and a late case must not make it COMPLETE.
                "ConditionExpression": "deadline >= :now",
                "ExpressionAttributeValues": to_dynamo(values)["M"],
            }
        },
    ]


def classify_cancellation(reasons: list[dict]) -> str:
    # DynamoDB lists one reason per transaction item, in order; "None" means that item
    # was fine. A duplicate wins when both conditions fail (item 0 is checked first).
    codes = [reason.get("Code") for reason in reasons]
    if codes[:1] == ["ConditionalCheckFailed"]:
        return "duplicate"
    if codes[:2] == ["None", "ConditionalCheckFailed"]:
        return "expired"
    raise StoreError(f"transaction cancelled for another reason: {codes}")


def encode_cursor(last_evaluated_key: dict) -> str:
    raw = json.dumps(last_evaluated_key, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str | None) -> dict | None:
    # A tampered cursor raises ValueError; the API turns that into a 400.
    if not cursor:
        return None
    return json.loads(base64.urlsafe_b64decode(cursor))


class DynamoStore:
    """The real Store, over a boto3 low-level DynamoDB client (5.34). Checked live at G2."""

    def __init__(self, client, table, clock=time.time):
        self.client = client
        self.table = table
        self.clock = clock

    # -- helpers --------------------------------------------------------------

    def _put(self, pk, sk, attributes, condition=None):
        kwargs = {"TableName": self.table, "Item": to_dynamo({"PK": pk, "SK": sk} | attributes)["M"]}
        if condition:
            kwargs["ConditionExpression"] = condition
        self.client.put_item(**kwargs)

    def _get(self, pk, sk):
        key = to_dynamo({"PK": pk, "SK": sk})["M"]
        # Consistent reads throughout: status is derived from counters that workers
        # have just written, and the API reads back what it has just created.
        item = self.client.get_item(TableName=self.table, Key=key, ConsistentRead=True).get("Item")
        return _without_keys(item) if item else None

    def _query_args(self, pk, prefix):
        return {
            "TableName": self.table,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": to_dynamo({":pk": pk, ":prefix": prefix})["M"],
            "ConsistentRead": True,
        }

    def _query(self, pk, prefix, cursor, limit):
        args = self._query_args(pk, prefix) | {"Limit": limit}
        start = decode_cursor(cursor)
        if start:
            # A cursor is client-supplied; it must stay inside this partition.
            if start.get("PK") != {"S": pk}:
                raise ValueError("cursor does not belong to this query")
            args["ExclusiveStartKey"] = start
        response = self.client.query(**args)
        last = response.get("LastEvaluatedKey")
        # DynamoDB can return a key even when the next page is empty; the caller then
        # gets one empty page, which is harmless.
        return [_without_keys(item) for item in response["Items"]], encode_cursor(last) if last else None

    def _query_all(self, pk, prefix):
        # One Query returns at most 1 MB, so follow LastEvaluatedKey to the end.
        items, args = [], self._query_args(pk, prefix)
        while True:
            response = self.client.query(**args)
            items += [_without_keys(item) for item in response["Items"]]
            if "LastEvaluatedKey" not in response:
                return items
            args["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _latest_version(self, pk, prefix):
        args = self._query_args(pk, prefix) | {"ScanIndexForward": False, "Limit": 1}
        items = self.client.query(**args)["Items"]
        return int(items[0]["SK"]["S"][len(prefix):]) if items else 0

    # -- configs and suites ---------------------------------------------------

    def put_config_version(self, owner, cfg_id, cfg):
        pk, prefix = user_pk(owner), f"CFG#{cfg_id}#V#"
        for _ in range(MAX_ATTEMPTS):
            n = self._latest_version(pk, prefix) + 1
            try:
                self._put(
                    pk, config_sk(cfg_id, n),
                    cfg | {"cfgId": cfg_id, "version": n, "created": self.clock()},
                    "attribute_not_exists(PK)",
                )
                return n
            except self.client.exceptions.ConditionalCheckFailedException:
                continue  # another request took this version number; read again
        raise StoreError(f"could not allocate a version for config {cfg_id}")

    def get_config_version(self, owner, cfg_id, n):
        return self._get(user_pk(owner), config_sk(cfg_id, n))

    def list_configs(self, owner):
        return self._query_all(user_pk(owner), "CFG#")

    def put_suite_version(self, owner, suite_id, name, cases):
        pk, prefix = user_pk(owner), f"SUITE#{suite_id}#V#"
        for _ in range(MAX_ATTEMPTS):
            n = self._latest_version(pk, prefix) + 1
            self._write_cases(case_pk(owner, suite_id, n), cases)
            try:
                # Last (M2): the suite only becomes visible once every case is stored.
                # Case items left behind by a failed import are a stated limitation.
                self._put(
                    pk, suite_sk(suite_id, n),
                    {"suiteId": suite_id, "version": n, "name": name,
                     "caseCount": len(cases), "created": self.clock()},
                    "attribute_not_exists(PK)",
                )
                return n
            except self.client.exceptions.ConditionalCheckFailedException:
                continue
        raise StoreError(f"could not allocate a version for suite {suite_id}")

    def _write_cases(self, pk, cases):
        requests = [
            {"PutRequest": {"Item": to_dynamo(
                {"PK": pk, "SK": case_sk(index)} | case | {"caseId": f"{index:04d}"}
            )["M"]}}
            for index, case in enumerate(cases, start=1)
        ]
        for start in range(0, len(requests), BATCH_WRITE_MAX):
            pending = {self.table: requests[start:start + BATCH_WRITE_MAX]}
            for attempt in range(MAX_ATTEMPTS):
                pending = self.client.batch_write_item(RequestItems=pending).get("UnprocessedItems")
                if not pending:
                    break
                time.sleep(0.05 * 2**attempt)  # throttled items are returned, not raised
            else:
                raise StoreError("DynamoDB left case items unprocessed after retries")

    def get_suite_version(self, owner, suite_id, n):
        return self._get(user_pk(owner), suite_sk(suite_id, n))

    def list_suites(self, owner):
        return self._query_all(user_pk(owner), "SUITE#")

    def query_cases(self, owner, suite_id, n, cursor, limit):
        return self._query(case_pk(owner, suite_id, n), "CASE#", cursor, limit)

    def get_case(self, owner, suite_id, n, case_id):
        return self._get(case_pk(owner, suite_id, n), case_sk(int(case_id)))

    # -- runs and results -----------------------------------------------------

    def create_run(self, owner, run):
        pk, sk = user_pk(owner), run_sk(run["runId"])
        try:
            self._put(pk, sk, run, "attribute_not_exists(PK)")
            return run, True
        except self.client.exceptions.ConditionalCheckFailedException:
            existing = self._get(pk, sk)
            if existing is None:
                raise StoreError(f"run {run['runId']} vanished after a failed create") from None
            if existing["requestHash"] != run["requestHash"]:
                raise IdempotencyConflict(run["runId"]) from None  # M3
            return existing, False

    def get_run(self, owner, run_id):
        return self._get(user_pk(owner), run_sk(run_id))

    def list_runs(self, owner):
        return self._query_all(user_pk(owner), "RUN#")

    def record_expansion(self, owner, run_id, expanded, cursor):
        try:
            self.client.update_item(
                TableName=self.table,
                Key=to_dynamo({"PK": user_pk(owner), "SK": run_sk(run_id)})["M"],
                # Placeholders for the names, in case one is a DynamoDB reserved word.
                UpdateExpression="SET #expanded = :expanded, #cursor = :cursor",
                ConditionExpression="attribute_exists(PK)",  # never create a run by accident
                ExpressionAttributeNames={"#expanded": "expanded", "#cursor": "expandCursor"},
                ExpressionAttributeValues=to_dynamo({":expanded": expanded, ":cursor": cursor})["M"],
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            raise StoreError(f"no run {run_id}") from None

    def write_result(self, owner, run_id, case_id, result, counters, now):
        items = result_transaction(self.table, owner, run_id, case_id, result, counters, now)
        try:
            self.client.transact_write_items(TransactItems=items)
        except self.client.exceptions.TransactionCanceledException as exc:
            return classify_cancellation(exc.response.get("CancellationReasons", []))
        return "written"

    def query_results(self, owner, run_id, cursor, limit):
        return self._query(result_pk(owner, run_id), "RES#", cursor, limit)

    # -- baselines and fresh pointers -----------------------------------------

    def set_baseline(self, owner, run):
        sk = baseline_sk(run["cfgId"], run["suiteId"], run["suiteVersion"])
        self._put(user_pk(owner), sk, {"runId": run["runId"]})

    def get_baseline(self, owner, cfg_id, suite_id, suite_v):
        item = self._get(user_pk(owner), baseline_sk(cfg_id, suite_id, suite_v))
        return item["runId"] if item else None

    def put_fresh_pointer(self, owner, cfg_id, cfg_v, suite_id, suite_v, run_id, created):
        sk = fresh_sk(cfg_id, cfg_v, suite_id, suite_v)
        self._put(user_pk(owner), sk, {"runId": run_id, "created": created})

    def get_fresh_pointer(self, owner, cfg_id, cfg_v, suite_id, suite_v):
        return self._get(user_pk(owner), fresh_sk(cfg_id, cfg_v, suite_id, suite_v))
