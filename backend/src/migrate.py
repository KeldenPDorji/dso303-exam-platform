"""Database migration Lambda, invoked by the CI/CD pipeline after every deploy.

Applies schema.sql (idempotent) and, when asked, seeds a demo exam and assignment
so both walkthroughs can be demonstrated straight after a fresh deployment.
Payload: {"seed": true, "tutorEmail": "tutor@example.edu"}
"""
import json
import logging
import os
import pathlib
from datetime import datetime, timedelta, timezone

from common import db

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SCHEMA = (pathlib.Path(__file__).parent / "schema.sql").read_text()

DEMO_QUESTIONS = [
    {"kind": "MCQ", "prompt": "Which service keeps 50 MB uploads off the API by issuing presigned URLs?",
     "options": ["Amazon EBS", "Amazon S3", "Amazon SQS", "AWS WAF"], "correct": 1, "marks": 1},
    {"kind": "MCQ", "prompt": "What does RDS Multi-AZ give this platform?",
     "options": ["Read scaling", "A synchronous standby in a second AZ with automatic failover",
                 "Cross-region replication", "Cheaper storage"], "correct": 1, "marks": 1},
    {"kind": "MCQ", "prompt": "Why are exam answers autosaved with a client sequence number?",
     "options": ["To compress them", "So a delayed retry can never overwrite newer answers",
                 "To encrypt them", "It is required by Cognito"], "correct": 1, "marks": 1},
    {"kind": "MCQ", "prompt": "Which component absorbs the burst of 3,000 submissions at the deadline?",
     "options": ["Amazon SQS", "Route 53", "NAT gateway", "Cognito"], "correct": 0, "marks": 1},
    {"kind": "SHORT", "prompt": "In two or three sentences, explain how this platform survives the loss of one Availability Zone.",
     "marks": 4},
]


def _statements(sql):
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def handler(event, context):
    event = event or {}
    for statement in _statements(SCHEMA):
        db.query(statement)
    seeded = seed(event.get("tutorEmail")) if event.get("seed") else []
    tables = db.query("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1")
    result = {"ok": True, "tables": [t["table_name"] for t in tables], "seeded": seeded}
    log.info(json.dumps(result))
    return result


def seed(tutor_email=None):
    created = []
    now = datetime.now(timezone.utc)
    if not db.one("SELECT id FROM exam LIMIT 1"):
        exam_id = db.one(
            """INSERT INTO exam (title, duration_minutes, opens_at, closes_at, created_by)
               VALUES (:title, 20, CAST(:opens AS timestamptz), CAST(:closes AS timestamptz), 'seed') RETURNING id""",
            title="DSO303 Practice Exam (demo)", opens=(now - timedelta(hours=1)).isoformat(),
            closes=(now + timedelta(days=30)).isoformat(),
        )["id"]
        for position, q in enumerate(DEMO_QUESTIONS, start=1):
            db.query(
                """INSERT INTO question (exam_id, position, kind, prompt, options, correct_option, marks)
                   VALUES (CAST(:exam_id AS uuid), :position, :kind, :prompt, CAST(:options AS jsonb), :correct,
                           CAST(:marks AS numeric))""",
                exam_id=str(exam_id), position=position, kind=q["kind"], prompt=q["prompt"],
                options=json.dumps(q["options"]) if "options" in q else None,
                correct=q.get("correct"), marks=str(q["marks"]),
            )
        created.append("exam")
    if tutor_email and not db.one("SELECT id FROM assignment LIMIT 1"):
        db.query(
            """INSERT INTO assignment (title, due_at, tutor_sub, tutor_email)
               VALUES ('Assignment 1: Cloud Design Report', CAST(:due AS timestamptz), 'seed', :email)""",
            due=(now + timedelta(days=14)).isoformat(), email=tutor_email,
        )
        created.append("assignment")
    return created
