"""Thin SQS helper used to hand work to the asynchronous workers."""
import json
import os

_sqs = None


def _client():
    global _sqs
    if _sqs is None:
        import boto3

        _sqs = boto3.client("sqs")
    return _sqs


def send_messages(queue_url, messages):
    """Send dict messages in batches of 10 (the SQS batch maximum)."""
    if not messages:
        return 0
    if os.environ.get("QUEUE_MODE") == "log":  # local development: print instead of sending
        for m in messages:
            print(f"[queue {queue_url}] {json.dumps(m, default=str)}")
        return len(messages)
    for start in range(0, len(messages), 10):
        chunk = messages[start:start + 10]
        result = _client().send_message_batch(
            QueueUrl=queue_url,
            Entries=[{"Id": str(i), "MessageBody": json.dumps(m, default=str)} for i, m in enumerate(chunk)],
        )
        if result.get("Failed"):
            raise RuntimeError(f"SQS rejected {len(result['Failed'])} message(s): {result['Failed']}")
    return len(messages)
