"""Notifier: notifications queue -> Amazon SES.

Runs outside the VPC on purpose: it touches no private resource, so it has no
dependency on NAT gateways and keeps working even if one AZ's NAT is lost.
The event source mapping caps its concurrency so we never exceed SES's send rate;
SQS holds the backlog (e.g. 3,000 "results released" emails) until it drains.
"""
import json
import logging
import os

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_ses = None


def _client():
    global _ses
    if _ses is None:
        import boto3

        _ses = boto3.client("sesv2")
    return _ses


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            msg = json.loads(record["body"])
            _client().send_email(
                FromEmailAddress=os.environ["SES_FROM"],
                Destination={"ToAddresses": [msg["to"]]},
                Content={"Simple": {
                    "Subject": {"Data": msg["subject"], "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": msg["body"], "Charset": "UTF-8"}},
                }},
            )
            log.info("email sent subject=%r", msg["subject"])
        except Exception:  # noqa: BLE001 - retried by SQS, then dead-lettered + alarm
            log.exception("Failed to send message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
