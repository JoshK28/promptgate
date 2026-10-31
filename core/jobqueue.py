"""Job queue messages, the queue interface and the SQS adapter (spec §7)."""

import json
from typing import NamedTuple, Protocol

SEND_BATCH_MAX = 10  # SQS's limit for one send_message_batch call


class Message(NamedTuple):
    body: dict
    receipt: str
    receive_count: int  # SQS ApproximateReceiveCount; 4 is the last attempt


class JobQueue(Protocol):
    def send(self, body: dict) -> None: ...
    def send_batch(self, bodies: list[dict]) -> list[dict]: ...  # returns the bodies that failed
    def receive(self, wait_s: int = 20) -> list[Message]: ...
    def delete(self, receipt: str) -> None: ...


def expand_message(owner, run_id):
    # The owner travels in the message so a worker builds its keys without a lookup.
    return {"type": "expand", "owner": owner, "runId": run_id}


def case_message(owner, run_id, case_id):
    return {"type": "case", "owner": owner, "runId": run_id, "caseId": case_id}


class SqsQueue:
    """The real JobQueue, over a boto3 SQS client (5.31). Takes the client; creates none."""

    def __init__(self, client, url):
        self.client = client
        self.url = url

    def send(self, body):
        self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(body))

    def send_batch(self, bodies):
        failed = []
        for start in range(0, len(bodies), SEND_BATCH_MAX):
            group = bodies[start:start + SEND_BATCH_MAX]
            entries = [{"Id": str(i), "MessageBody": json.dumps(body)} for i, body in enumerate(group)]
            response = self.client.send_message_batch(QueueUrl=self.url, Entries=entries)
            # A batch call can succeed overall while some entries fail; hand those back.
            failed += [group[int(entry["Id"])] for entry in response.get("Failed", [])]
        return failed

    def receive(self, wait_s=20):
        # Long polling: the call waits up to wait_s for a message instead of returning at once.
        response = self.client.receive_message(
            QueueUrl=self.url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_s,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        return [
            Message(
                json.loads(message["Body"]),
                message["ReceiptHandle"],
                int(message["Attributes"]["ApproximateReceiveCount"]),
            )
            for message in response.get("Messages", [])
        ]

    def delete(self, receipt):
        self.client.delete_message(QueueUrl=self.url, ReceiptHandle=receipt)
