"""Submission processor: S3 ObjectCreated -> SQS -> this Lambda.

1. Checks the uploaded object really is a PDF/ZIP (magic bytes, not just the name).
2. Records the submission once (s3_key is UNIQUE, so SQS redeliveries are harmless).
3. Queues a notification to the tutor and an email receipt to the student.
"""
import json
import logging
import os
import re
from urllib.parse import unquote_plus

from common import db, emails
from common.queue import send_messages

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

KEY_RE = re.compile(r"^submissions/(?P<assignment_id>[0-9a-fA-F-]{36})/(?P<student_sub>[^/]+)/(?P<object>[^/]+)$")
MAGIC_BYTES = {"application/pdf": (b"%PDF",), "application/zip": (b"PK\x03\x04",)}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))

_s3 = None


def _s3_client():
    global _s3
    if _s3 is None:
        import boto3

        _s3 = boto3.client("s3")
    return _s3


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            if body.get("Event") == "s3:TestEvent":  # sent once when the notification is configured
                continue
            for s3_record in body.get("Records", []):
                process(s3_record["s3"]["bucket"]["name"], unquote_plus(s3_record["s3"]["object"]["key"]))
        except Exception:  # noqa: BLE001
            log.exception("Failed to process message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def check_file(content_type, first_bytes, size):
    """Return a rejection reason, or None if the file is acceptable."""
    if content_type not in MAGIC_BYTES:
        return "only PDF or ZIP files are accepted"
    if not first_bytes.startswith(MAGIC_BYTES[content_type]):
        return "the file content does not match its type (corrupted or renamed file)"
    if size > MAX_UPLOAD_BYTES:
        return "the file is larger than 50 MB"
    return None


def process(bucket, key):
    match = KEY_RE.match(key)
    if not match:
        log.warning("Ignoring object with unexpected key: %s", key)
        return
    s3 = _s3_client()
    head = s3.head_object(Bucket=bucket, Key=key)
    size = head["ContentLength"]
    content_type = head.get("ContentType", "")
    file_name = head.get("Metadata", {}).get("original-name") or match["object"]
    first_bytes = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-7")["Body"].read()
    reason = check_file(content_type, first_bytes, size)

    db.query(
        """INSERT INTO submission (assignment_id, student_sub, student_email, s3_key, s3_version_id, file_name,
                                   size_bytes, content_type, status, reject_reason, late)
           SELECT a.id, :sub, (SELECT email FROM app_user WHERE sub = :sub), :key, :version, :file_name,
                  :size, :content_type, :status, :reason, now() > a.due_at
           FROM assignment a WHERE a.id = CAST(:assignment_id AS uuid)
           ON CONFLICT (s3_key) DO NOTHING""",
        sub=match["student_sub"], key=key, version=head.get("VersionId"), file_name=file_name, size=size,
        content_type=content_type, status="REJECTED" if reason else "RECEIVED", reason=reason,
        assignment_id=match["assignment_id"],
    )
    sub = db.one(
        """SELECT s.id, s.student_email, s.file_name, s.size_bytes, s.status, s.reject_reason, s.late,
                  s.received_at, s.s3_version_id, s.notified, a.title, a.tutor_email
           FROM submission s JOIN assignment a ON a.id = s.assignment_id WHERE s.s3_key = :key""",
        key=key,
    )
    if sub is None:
        log.warning("Assignment %s not found for %s", match["assignment_id"], key)
        return
    if sub["notified"]:
        log.info("Duplicate event for %s, already processed", key)
        return

    messages = []
    if sub["status"] == "REJECTED":
        if sub["student_email"]:
            messages.append(emails.submission_rejected(sub["student_email"], sub["title"], sub["file_name"],
                                                       sub["reject_reason"]))
    else:
        messages.append(emails.tutor_new_submission(sub["tutor_email"], sub["student_email"], sub["title"],
                                                    sub["file_name"], sub["size_bytes"], sub["late"]))
        if sub["student_email"]:
            messages.append(emails.submission_receipt(sub["student_email"], sub["title"], sub["file_name"],
                                                      sub["size_bytes"], sub["received_at"], sub["s3_version_id"],
                                                      sub["late"]))
    send_messages(os.environ["NOTIFY_QUEUE_URL"], messages)
    # Mark as notified only after the messages are queued: a crash before this line
    # means SQS redelivers and we try again (at-least-once, never lost).
    db.query("UPDATE submission SET notified = true WHERE id = CAST(:id AS uuid)", id=str(sub["id"]))
    log.info("submission %s %s (%s bytes) for assignment %s", key, sub["status"], size, match["assignment_id"])
