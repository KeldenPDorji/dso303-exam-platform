"""Marking worker.

* SQS trigger (marking queue): auto-marks the MCQ part of a submitted attempt.
* EventBridge schedule ({"sweep": true}, every 2 minutes): auto-submits attempts
  whose deadline has passed (student closed the tab or lost power) and re-queues
  any submitted attempt that was never marked. This makes marking self-healing.
"""
import json
import logging
import os

from common import db
from common.marking import score_attempt
from common.queue import send_messages

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event, context):
    if isinstance(event, dict) and event.get("sweep"):
        return sweep()
    failures = []
    for record in event.get("Records", []):
        try:
            mark_attempt(json.loads(record["body"])["attemptId"])
        except Exception:  # noqa: BLE001 - report this message only; SQS retries it, then DLQ
            log.exception("Marking failed for message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def mark_attempt(attempt_id):
    attempt = db.one("SELECT exam_id, answers, status FROM attempt WHERE id = CAST(:id AS uuid)", id=attempt_id)
    if attempt is None:
        log.warning("Attempt %s not found, skipping", attempt_id)
        return
    if attempt["status"] == "IN_PROGRESS":
        log.warning("Attempt %s is still in progress, skipping", attempt_id)
        return
    questions = db.query(
        "SELECT id, kind, correct_option, marks FROM question WHERE exam_id = CAST(:exam_id AS uuid)",
        exam_id=str(attempt["exam_id"]),
    )
    score, total = score_attempt(questions, attempt["answers"])
    db.query(
        """UPDATE attempt SET mcq_score = CAST(:score AS numeric), mcq_total = CAST(:total AS numeric),
                              status = 'MARKED', marked_at = now()
           WHERE id = CAST(:id AS uuid)""",
        score=str(score), total=str(total), id=attempt_id,
    )
    log.info("attempt %s marked: %s/%s", attempt_id, score, total)


def sweep():
    expired = db.query(
        """UPDATE attempt SET status = 'SUBMITTED', submitted_at = now(), auto_submitted = true
           WHERE status = 'IN_PROGRESS' AND now() > deadline + interval '60 seconds'
           RETURNING id"""
    )
    stuck = db.query(
        "SELECT id FROM attempt WHERE status = 'SUBMITTED' AND submitted_at < now() - interval '10 minutes'"
    )
    ids = [str(r["id"]) for r in expired + stuck]
    send_messages(os.environ["MARKING_QUEUE_URL"], [{"attemptId": i} for i in ids])
    log.info("sweep: auto-submitted=%d requeued=%d", len(expired), len(stuck))
    return {"autoSubmitted": len(expired), "requeued": len(stuck)}
