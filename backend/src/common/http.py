"""HTTP helpers for API Gateway HTTP API (payload format 2.0)."""
import base64
import datetime
import decimal
import json
import uuid


class HttpError(Exception):
    """Raised by handlers to return a clean JSON error to the browser."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _json_default(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"Cannot serialise {type(value).__name__}")


def to_json(body):
    return json.dumps(body, default=_json_default)


def respond(status, body=None):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": to_json(body) if body is not None else "",
    }


def parse_body(event, max_bytes=262_144):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw) > max_bytes:
        raise HttpError(413, "Request body too large")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HttpError(400, "Body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise HttpError(400, "Body must be a JSON object")
    return data
