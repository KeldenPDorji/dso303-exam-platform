"""Exam platform REST API: one Lambda behind API Gateway (HTTP API).

API Gateway validates the Cognito JWT and applies throttling before this code
runs. Routing happens here, so the whole API is one function with one
least-privilege IAM role.
"""
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from common import db, emails
from common.auth import ROLES, principal_from_event
from common.http import HttpError, parse_body, respond
from common.queue import send_messages
from common.validation import (
    parse_time,
    validate_answers,
    validate_client_seq,
    validate_email,
    validate_exam,
    validate_schedule,
    validate_upload,
)

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
GRACE = "interval '60 seconds'"  # network grace after the deadline for the final autosave/submit
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
UPLOAD_URL_TTL = 900
ROUTES = []

_s3 = None
_cognito = None


def _s3_client():
    """Regional, SigV4 S3 client so presigned URLs work with SSE-KMS and new buckets."""
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config

        region = os.environ.get("AWS_REGION", "ap-southeast-2")
        _s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def _cognito_client():
    global _cognito
    if _cognito is None:
        import boto3

        _cognito = boto3.client("cognito-idp")
    return _cognito


def route(method, path, *roles):
    pattern = re.compile("^" + re.sub(r"\{(\w+)\}", lambda m: f"(?P<{m.group(1)}>{UUID_RE})", path) + "$")

    def register(fn):
        ROUTES.append((method, pattern, roles, fn))
        return fn

    return register


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _verify_origin(event):
    """Only accept traffic that came through CloudFront (and therefore through WAF)."""
    expected = os.environ.get("ORIGIN_VERIFY_SECRET")
    if not expected:
        return
    given = (event.get("headers") or {}).get("x-origin-verify", "")
    if not hmac.compare_digest(given, expected):
        raise HttpError(403, "Direct API access is not allowed")


def handler(event, context):
    try:
        _verify_origin(event)
        method = event["requestContext"]["http"]["method"]
        path = event.get("rawPath", "")
        if method == "GET" and path == "/api/health":
            return health()
        principal = principal_from_event(event)
        for route_method, pattern, roles, fn in ROUTES:
            match = pattern.match(path)
            if match and route_method == method:
                if roles:
                    principal.require(*roles)
                body = parse_body(event) if method in ("POST", "PUT") else {}
                return fn(principal, body, **match.groupdict())
        raise HttpError(404, "Not found")
    except HttpError as err:
        return respond(err.status, {"error": err.message})
    except Exception:  # noqa: BLE001
        log.exception("Unhandled error")
        return respond(500, {"error": "Internal error, please retry"})


# --------------------------------------------------------------------------- health


def health():
    """Unauthenticated health check. dbServer shows which DB host answered:
    after a Multi-AZ failover it switches to the other AZ's data subnet."""
    started = time.perf_counter()
    row = db.one("SELECT now() AS db_time, host(inet_server_addr()) AS db_server")
    return respond(200, {
        "status": "ok",
        "db": "ok",
        "dbServer": row["db_server"],
        "dbLatencyMs": round((time.perf_counter() - started) * 1000, 1),
        "time": _now_iso(),
    })


# --------------------------------------------------------------------------- profile


@route("GET", "/api/me")
def me(p, body):
    db.query(
        """INSERT INTO app_user (sub, email, role) VALUES (:sub, :email, :role)
           ON CONFLICT (sub) DO UPDATE SET email = EXCLUDED.email, role = EXCLUDED.role, last_seen_at = now()""",
        sub=p.sub, email=p.email, role=p.role,
    )
    return respond(200, {"sub": p.sub, "email": p.email, "role": p.role, "groups": list(p.groups)})


# --------------------------------------------------------------------------- exams


@route("GET", "/api/exams")
def list_exams(p, body):
    if p.has("Tutors", "Admins"):
        exams = db.query(
            """SELECT e.id, e.title, e.duration_minutes, e.opens_at, e.closes_at, e.results_released,
                      (SELECT count(*) FROM question q WHERE q.exam_id = e.id) AS question_count,
                      (SELECT count(*) FROM attempt a WHERE a.exam_id = e.id) AS attempt_count,
                      (SELECT count(*) FROM attempt a WHERE a.exam_id = e.id AND a.status <> 'IN_PROGRESS') AS submitted_count
               FROM exam e ORDER BY e.opens_at DESC"""
        )
    else:
        exams = db.query(
            """SELECT e.id, e.title, e.duration_minutes, e.opens_at, e.closes_at, e.results_released,
                      a.id AS attempt_id, a.status AS attempt_status, a.deadline
               FROM exam e
               LEFT JOIN attempt a ON a.exam_id = e.id AND a.student_sub = :sub
               ORDER BY e.opens_at""",
            sub=p.sub,
        )
    return respond(200, {"exams": exams, "serverTime": _now_iso()})


@route("POST", "/api/exams", "Tutors", "Admins")
def create_exam(p, body):
    exam = validate_exam(body)

    def work(conn):
        exam_id = db.rows(
            conn,
            """INSERT INTO exam (title, duration_minutes, opens_at, closes_at, created_by)
               VALUES (:title, :duration, CAST(:opens AS timestamptz), CAST(:closes AS timestamptz), :by)
               RETURNING id""",
            title=exam["title"], duration=exam["duration"], opens=exam["opens_at"],
            closes=exam["closes_at"], by=p.email or p.sub,
        )[0]["id"]
        for position, q in enumerate(exam["questions"], start=1):
            db.rows(
                conn,
                """INSERT INTO question (exam_id, position, kind, prompt, options, correct_option, marks)
                   VALUES (CAST(:exam_id AS uuid), :position, :kind, :prompt, CAST(:options AS jsonb), :correct,
                           CAST(:marks AS numeric))""",
                exam_id=str(exam_id), position=position, kind=q["kind"], prompt=q["prompt"],
                options=json.dumps(q["options"]) if q["options"] is not None else None,
                correct=q["correct"], marks=str(q["marks"]),
            )
        return exam_id

    exam_id = db.transaction(work)
    log.info("exam created id=%s by=%s questions=%d", exam_id, p.sub, len(exam["questions"]))
    return respond(201, {"id": exam_id})


@route("PUT", "/api/exams/{exam_id}/schedule", "Admins")
def update_schedule(p, body, exam_id):
    s = validate_schedule(body)
    row = db.one(
        """UPDATE exam SET opens_at = CAST(:opens AS timestamptz), closes_at = CAST(:closes AS timestamptz),
                          duration_minutes = :duration
           WHERE id = CAST(:id AS uuid) RETURNING id, opens_at, closes_at, duration_minutes""",
        opens=s["opens_at"], closes=s["closes_at"], duration=s["duration"], id=exam_id,
    )
    if not row:
        raise HttpError(404, "Exam not found")
    return respond(200, row)


@route("POST", "/api/exams/{exam_id}/start", "Students")
def start_exam(p, body, exam_id):
    exam = db.one("SELECT id, title, duration_minutes, opens_at, closes_at FROM exam WHERE id = CAST(:id AS uuid)", id=exam_id)
    if not exam:
        raise HttpError(404, "Exam not found")
    # The deadline is fixed on the server when the attempt is created, so
    # refreshing the page or changing the PC clock cannot add time.
    db.query(
        """INSERT INTO attempt (exam_id, student_sub, student_email, deadline)
           SELECT e.id, :sub, :email, LEAST(now() + make_interval(mins => e.duration_minutes), e.closes_at)
           FROM exam e
           WHERE e.id = CAST(:id AS uuid) AND now() BETWEEN e.opens_at AND e.closes_at
           ON CONFLICT (exam_id, student_sub) DO NOTHING""",
        sub=p.sub, email=p.email, id=exam_id,
    )
    attempt = db.one(
        """SELECT id, status, deadline, answers, client_seq, saved_at FROM attempt
           WHERE exam_id = CAST(:id AS uuid) AND student_sub = :sub""",
        id=exam_id, sub=p.sub,
    )
    if attempt is None:
        raise HttpError(409, "This exam is not open right now")
    if attempt["status"] != "IN_PROGRESS":
        raise HttpError(409, "You have already submitted this exam")
    questions = db.query(
        """SELECT id, position, kind, prompt, options, marks FROM question
           WHERE exam_id = CAST(:id AS uuid) ORDER BY position""",
        id=exam_id,
    )  # correct_option is deliberately NOT selected: the answer key never leaves the server
    return respond(200, {
        "exam": {"id": exam["id"], "title": exam["title"], "durationMinutes": exam["duration_minutes"]},
        "attempt": attempt,
        "questions": questions,
        "serverTime": _now_iso(),
    })


@route("PUT", "/api/attempts/{attempt_id}/answers", "Students")
def autosave(p, body, attempt_id):
    answers = validate_answers(body.get("answers"))
    seq = validate_client_seq(body.get("clientSeq"))
    saved = db.one(
        f"""UPDATE attempt SET answers = CAST(:answers AS jsonb), client_seq = :seq, saved_at = now()
            WHERE id = CAST(:id AS uuid) AND student_sub = :sub AND status = 'IN_PROGRESS'
              AND client_seq < :seq AND now() <= deadline + {GRACE}
            RETURNING saved_at, client_seq""",
        answers=json.dumps(answers), seq=seq, id=attempt_id, sub=p.sub,
    )
    if saved:
        return respond(200, {"savedAt": saved["saved_at"], "clientSeq": saved["client_seq"]})
    current = db.one(
        f"""SELECT status, client_seq, saved_at, now() > deadline + {GRACE} AS expired
            FROM attempt WHERE id = CAST(:id AS uuid) AND student_sub = :sub""",
        id=attempt_id, sub=p.sub,
    )
    if current is None:
        raise HttpError(404, "Attempt not found")
    if current["status"] != "IN_PROGRESS" or current["expired"]:
        raise HttpError(409, "Time is over or the exam was already submitted. Your last saved answers are kept.")
    # A newer save already landed (this was a delayed retry): idempotent success.
    return respond(200, {"savedAt": current["saved_at"], "clientSeq": current["client_seq"], "stale": True})


@route("POST", "/api/attempts/{attempt_id}/submit", "Students")
def submit_attempt(p, body, attempt_id):
    answers = validate_answers(body.get("answers"))
    seq = validate_client_seq(body.get("clientSeq", 0), allow_zero=True)
    row = db.one(
        f"""UPDATE attempt SET
                answers      = CASE WHEN client_seq < :seq THEN CAST(:answers AS jsonb) ELSE answers END,
                saved_at     = CASE WHEN client_seq < :seq THEN now() ELSE saved_at END,
                client_seq   = GREATEST(client_seq, :seq),
                status       = 'SUBMITTED',
                submitted_at = now()
            WHERE id = CAST(:id AS uuid) AND student_sub = :sub AND status = 'IN_PROGRESS'
              AND now() <= deadline + {GRACE}
            RETURNING id, submitted_at""",
        answers=json.dumps(answers), seq=seq, id=attempt_id, sub=p.sub,
    )
    if row is None:
        current = db.one("SELECT status FROM attempt WHERE id = CAST(:id AS uuid) AND student_sub = :sub",
                         id=attempt_id, sub=p.sub)
        if current is None:
            raise HttpError(404, "Attempt not found")
        if current["status"] in ("SUBMITTED", "MARKED"):
            return respond(200, {"status": current["status"], "alreadySubmitted": True})
        raise HttpError(409, "Time is over. Your last autosaved answers will be submitted automatically.")
    send_messages(os.environ["MARKING_QUEUE_URL"], [{"attemptId": attempt_id}])
    return respond(200, {"status": "SUBMITTED", "submittedAt": row["submitted_at"]})


@route("GET", "/api/exams/{exam_id}/attempts", "Tutors", "Admins")
def list_attempts(p, body, exam_id):
    questions = db.query(
        "SELECT id, position, kind, prompt, marks FROM question WHERE exam_id = CAST(:id AS uuid) ORDER BY position",
        id=exam_id,
    )
    attempts = db.query(
        """SELECT id, student_email, status, started_at, deadline, saved_at, submitted_at, auto_submitted,
                  answers, mcq_score, mcq_total, short_score
           FROM attempt WHERE exam_id = CAST(:id AS uuid) ORDER BY student_email""",
        id=exam_id,
    )
    return respond(200, {"questions": questions, "attempts": attempts})


@route("PUT", "/api/attempts/{attempt_id}/short-score", "Tutors", "Admins")
def set_short_score(p, body, attempt_id):
    score = body.get("shortScore")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or score < 0:
        raise HttpError(400, "shortScore must be a number of marks (0 or more)")
    row = db.one(
        """UPDATE attempt a SET short_score = CAST(:score AS numeric)
           WHERE a.id = CAST(:id AS uuid) AND a.status <> 'IN_PROGRESS'
             AND CAST(:score AS numeric) <= (SELECT COALESCE(sum(q.marks), 0) FROM question q
                                             WHERE q.exam_id = a.exam_id AND q.kind = 'SHORT')
           RETURNING a.id, a.short_score""",
        score=str(score), id=attempt_id,
    )
    if not row:
        raise HttpError(400, "Attempt not submitted yet, or score exceeds the short-answer marks available")
    return respond(200, row)


@route("POST", "/api/exams/{exam_id}/release", "Tutors", "Admins")
def release_results(p, body, exam_id):
    exam = db.one(
        """UPDATE exam SET results_released = true, released_at = COALESCE(released_at, now())
           WHERE id = CAST(:id AS uuid) RETURNING title""",
        id=exam_id,
    )
    if not exam:
        raise HttpError(404, "Exam not found")
    attempts = db.query(
        "SELECT student_email FROM attempt WHERE exam_id = CAST(:id AS uuid) AND status = 'MARKED'", id=exam_id
    )
    pending = db.one(
        "SELECT count(*) AS n FROM attempt WHERE exam_id = CAST(:id AS uuid) AND status = 'SUBMITTED'", id=exam_id
    )["n"]
    site_url = os.environ.get("SITE_URL", "")
    # One message per student: SQS + the notifier's capped concurrency drain these at SES's send rate.
    notified = send_messages(
        os.environ["NOTIFY_QUEUE_URL"],
        [emails.results_released(a["student_email"], exam["title"], site_url) for a in attempts],
    )
    return respond(200, {"released": True, "notified": notified, "stillMarking": pending})


@route("GET", "/api/results", "Students")
def my_results(p, body):
    results = db.query(
        """SELECT e.id AS exam_id, e.title, a.submitted_at, a.auto_submitted, a.mcq_score, a.mcq_total, a.short_score,
                  (SELECT COALESCE(sum(q.marks), 0) FROM question q WHERE q.exam_id = e.id AND q.kind = 'SHORT') AS short_total,
                  e.released_at
           FROM attempt a JOIN exam e ON e.id = a.exam_id
           WHERE a.student_sub = :sub AND e.results_released AND a.status = 'MARKED'
           ORDER BY e.released_at DESC""",
        sub=p.sub,
    )
    return respond(200, {"results": results})


# --------------------------------------------------------------------------- assignments


@route("GET", "/api/assignments")
def list_assignments(p, body):
    if p.has("Tutors", "Admins"):
        items = db.query(
            """SELECT a.id, a.title, a.due_at, a.tutor_email,
                      (SELECT count(DISTINCT s.student_sub) FROM submission s
                       WHERE s.assignment_id = a.id AND s.status = 'RECEIVED') AS submission_count
               FROM assignment a ORDER BY a.due_at DESC"""
        )
    else:
        items = db.query(
            """SELECT a.id, a.title, a.due_at, s.file_name, s.size_bytes, s.received_at, s.status AS submission_status,
                      s.reject_reason, s.late
               FROM assignment a
               LEFT JOIN LATERAL (
                   SELECT file_name, size_bytes, received_at, status, reject_reason, late FROM submission
                   WHERE assignment_id = a.id AND student_sub = :sub ORDER BY received_at DESC LIMIT 1
               ) s ON true
               ORDER BY a.due_at""",
            sub=p.sub,
        )
    return respond(200, {"assignments": items, "maxUploadBytes": MAX_UPLOAD_BYTES})


@route("POST", "/api/assignments", "Tutors", "Admins")
def create_assignment(p, body):
    title = str(body.get("title") or "").strip()
    if not 3 <= len(title) <= 200:
        raise HttpError(400, "title must be 3-200 characters")
    due = parse_time(body.get("dueAt"), "dueAt")
    if not p.email:
        raise HttpError(400, "Your account has no email address for notifications")
    row = db.one(
        """INSERT INTO assignment (title, due_at, tutor_sub, tutor_email)
           VALUES (:title, CAST(:due AS timestamptz), :sub, :email) RETURNING id""",
        title=title, due=due.isoformat(), sub=p.sub, email=p.email,
    )
    return respond(201, {"id": row["id"]})


@route("POST", "/api/assignments/{assignment_id}/upload-url", "Students")
def create_upload_url(p, body, assignment_id):
    ext, content_type, file_name, _size = validate_upload(
        body.get("fileName"), body.get("contentType"), body.get("size"), MAX_UPLOAD_BYTES
    )
    if not db.one("SELECT id FROM assignment WHERE id = CAST(:id AS uuid)", id=assignment_id):
        raise HttpError(404, "Assignment not found")
    key = f"submissions/{assignment_id}/{p.sub}/{uuid.uuid4()}{ext}"
    # Presigned POST: the browser uploads straight to S3. The policy is signed, so S3
    # itself rejects anything larger than 50 MB or of a different content type.
    post = _s3_client().generate_presigned_post(
        Bucket=os.environ["UPLOAD_BUCKET"],
        Key=key,
        Fields={"Content-Type": content_type, "x-amz-meta-original-name": file_name},
        Conditions=[
            {"Content-Type": content_type},
            {"x-amz-meta-original-name": file_name},
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=UPLOAD_URL_TTL,
    )
    log.info("upload url issued assignment=%s student=%s key=%s", assignment_id, p.sub, key)
    return respond(200, {"url": post["url"], "fields": post["fields"], "key": key,
                         "expiresIn": UPLOAD_URL_TTL, "maxBytes": MAX_UPLOAD_BYTES})


@route("GET", "/api/assignments/{assignment_id}/submissions", "Tutors", "Admins")
def list_submissions(p, body, assignment_id):
    items = db.query(
        """SELECT id, student_email, file_name, size_bytes, content_type, status, reject_reason, late, received_at, s3_key
           FROM submission WHERE assignment_id = CAST(:id AS uuid) ORDER BY received_at DESC""",
        id=assignment_id,
    )
    for item in items:
        key = item.pop("s3_key")
        if item["status"] == "RECEIVED":
            item["downloadUrl"] = _s3_client().generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": os.environ["UPLOAD_BUCKET"],
                    "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{item["file_name"] or "submission"}"',
                },
                ExpiresIn=300,
            )
    return respond(200, {"submissions": items})


# --------------------------------------------------------------------------- admin


@route("GET", "/api/admin/users", "Admins")
def list_users(p, body):
    client = _cognito_client()
    users = []
    for group in ROLES:
        kwargs = {"UserPoolId": os.environ["USER_POOL_ID"], "GroupName": group, "Limit": 60}
        for _ in range(10):  # up to 600 users per role shown in the console view
            page = client.list_users_in_group(**kwargs)
            for u in page.get("Users", []):
                attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
                users.append({"email": attrs.get("email", u["Username"]), "role": group,
                              "status": u.get("UserStatus"), "enabled": u.get("Enabled"),
                              "created": u.get("UserCreateDate")})
            if not page.get("NextToken"):
                break
            kwargs["NextToken"] = page["NextToken"]
    return respond(200, {"users": users})


@route("POST", "/api/admin/users", "Admins")
def create_user(p, body):
    email = validate_email(body.get("email"))
    role = body.get("role")
    if role not in ROLES:
        raise HttpError(400, "role must be one of " + ", ".join(ROLES))
    client = _cognito_client()
    pool = os.environ["USER_POOL_ID"]
    try:
        client.admin_create_user(
            UserPoolId=pool,
            Username=email,
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            DesiredDeliveryMediums=["EMAIL"],  # Cognito emails a temporary password
        )
    except client.exceptions.UsernameExistsException as exc:
        raise HttpError(409, "A user with that email already exists") from exc
    client.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=role)
    log.info("user created email=%s role=%s by=%s", email, role, p.sub)
    return respond(201, {"email": email, "role": role})
