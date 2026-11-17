"""End-to-end test of the real Lambda handlers against a real PostgreSQL.

Skipped unless INTEGRATION_DB=1. Run locally with:  ./scripts/integration-test.sh
(it starts a throwaway PostgreSQL, runs this file, then deletes the database).
Covers walkthrough 1 (start exam, autosave, submit, auto-mark, release) and
walkthrough 2 (presigned upload, S3 event processing, tutor notification).
"""
import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("INTEGRATION_DB"), reason="set INTEGRATION_DB=1")

SECRET = "test-origin-secret"
os.environ.update({
    "ORIGIN_VERIFY_SECRET": SECRET,
    "QUEUE_MODE": "log",
    "MARKING_QUEUE_URL": "https://sqs.local/marking",
    "NOTIFY_QUEUE_URL": "https://sqs.local/notifications",
    "UPLOAD_BUCKET": "uploads-test-bucket",
    "SITE_URL": "https://exams.example.edu",
    "AWS_REGION": "ap-southeast-2",
    "AWS_ACCESS_KEY_ID": "AKIAEXAMPLEEXAMPLE00",
    "AWS_SECRET_ACCESS_KEY": "example-secret-key-for-offline-presigning",
})

TUTOR = {"sub": "tutor-1", "email": "tutor@uni.edu", "groups": "[Tutors]"}
STUDENT = {"sub": "student-1", "email": "student@uni.edu", "groups": "[Students]"}
STUDENT2 = {"sub": "student-2", "email": "student2@uni.edu", "groups": "[Students]"}


@pytest.fixture(scope="module")
def modules():
    import api
    import marker
    import migrate
    import submission_processor

    result = migrate.handler({"seed": True, "tutorEmail": TUTOR["email"]}, None)
    assert {"exam", "attempt", "submission"} <= set(result["tables"])
    return api, marker, submission_processor


def call(api, user, method, path, body=None, origin=SECRET):
    claims = {"sub": user["sub"], "email": user["email"], "cognito:groups": user["groups"], "token_use": "id"}
    event = {
        "rawPath": path,
        "headers": {"x-origin-verify": origin},
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "body": json.dumps(body) if body is not None else None,
    }
    resp = api.handler(event, None)
    return resp["statusCode"], (json.loads(resp["body"]) if resp["body"] else None)


def iso(delta_minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


def test_health_and_origin_check(modules):
    api, _, _ = modules
    status, body = call(api, STUDENT, "GET", "/api/health")
    assert status == 200 and body["db"] == "ok"
    status, _ = call(api, STUDENT, "GET", "/api/health", origin="wrong")
    assert status == 403


def test_walkthrough_1_exam_autosave_submit_mark_release(modules):
    api, marker, _ = modules
    assert call(api, TUTOR, "GET", "/api/me")[1]["role"] == "Tutors"
    assert call(api, STUDENT, "GET", "/api/me")[0] == 200

    # tutor creates an exam
    status, created = call(api, TUTOR, "POST", "/api/exams", {
        "title": "Networking Quiz", "durationMinutes": 30, "opensAt": iso(-5), "closesAt": iso(120),
        "questions": [
            {"kind": "MCQ", "prompt": "Port for HTTPS?", "options": ["80", "443", "22"], "correctOption": 1, "marks": 2},
            {"kind": "MCQ", "prompt": "Layer of IP?", "options": ["3", "7"], "correctOption": 0},
            {"kind": "SHORT", "prompt": "Explain NAT.", "marks": 5},
        ]})
    assert status == 201, created
    exam_id = created["id"]

    # students cannot create exams
    assert call(api, STUDENT, "POST", "/api/exams", {"title": "x"})[0] == 403

    # student starts: gets questions WITHOUT the answer key and a server deadline
    status, started = call(api, STUDENT, "POST", f"/api/exams/{exam_id}/start")
    assert status == 200, started
    assert all("correct_option" not in q for q in started["questions"])
    attempt_id = started["attempt"]["id"]
    q1, q2, q3 = (q["id"] for q in started["questions"])

    # starting again resumes the same attempt
    assert call(api, STUDENT, "POST", f"/api/exams/{exam_id}/start")[1]["attempt"]["id"] == attempt_id

    # autosave every 30 s with an increasing client sequence
    status, saved = call(api, STUDENT, "PUT", f"/api/attempts/{attempt_id}/answers", {"answers": {q1: 0}, "clientSeq": 1})
    assert status == 200 and saved["clientSeq"] == 1
    status, saved = call(api, STUDENT, "PUT", f"/api/attempts/{attempt_id}/answers",
                         {"answers": {q1: 1, q3: "Translates private IPs"}, "clientSeq": 2})
    assert status == 200 and saved["clientSeq"] == 2
    # a delayed retry of save #1 must NOT overwrite save #2
    status, stale = call(api, STUDENT, "PUT", f"/api/attempts/{attempt_id}/answers", {"answers": {q1: 0}, "clientSeq": 1})
    assert status == 200 and stale.get("stale") is True

    # another student cannot write to this attempt
    assert call(api, STUDENT2, "PUT", f"/api/attempts/{attempt_id}/answers", {"answers": {}, "clientSeq": 9})[0] == 404

    # tutor can watch autosaved answers live
    status, view = call(api, TUTOR, "GET", f"/api/exams/{exam_id}/attempts")
    assert view["attempts"][0]["answers"][q1] == 1

    # submit (final answers included) is idempotent
    status, sub = call(api, STUDENT, "POST", f"/api/attempts/{attempt_id}/submit",
                       {"answers": {q1: 1, q2: 0, q3: "Translates private IPs to a public IP"}, "clientSeq": 3})
    assert status == 200 and sub["status"] == "SUBMITTED"
    assert call(api, STUDENT, "POST", f"/api/attempts/{attempt_id}/submit", {})[1]["alreadySubmitted"] is True
    assert call(api, STUDENT, "PUT", f"/api/attempts/{attempt_id}/answers", {"answers": {}, "clientSeq": 4})[0] == 409

    # marking worker (SQS batch) auto-marks the MCQs
    result = marker.handler({"Records": [{"messageId": "m1", "body": json.dumps({"attemptId": attempt_id})}]}, None)
    assert result == {"batchItemFailures": []}
    attempt = call(api, TUTOR, "GET", f"/api/exams/{exam_id}/attempts")[1]["attempts"][0]
    assert attempt["status"] == "MARKED" and attempt["mcq_score"] == 3 and attempt["mcq_total"] == 3

    # tutor marks the short answer (cannot exceed available marks) and releases
    assert call(api, TUTOR, "PUT", f"/api/attempts/{attempt_id}/short-score", {"shortScore": 9})[0] == 400
    assert call(api, TUTOR, "PUT", f"/api/attempts/{attempt_id}/short-score", {"shortScore": 4})[0] == 200
    status, released = call(api, TUTOR, "POST", f"/api/exams/{exam_id}/release")
    assert status == 200 and released["notified"] == 1

    results = call(api, STUDENT, "GET", "/api/results")[1]["results"]
    mine = [r for r in results if r["exam_id"] == exam_id][0]
    assert (mine["mcq_score"], mine["short_score"], mine["short_total"]) == (3, 4, 5)


def test_sweeper_auto_submits_expired_attempts(modules):
    api, marker, _ = modules
    from common import db

    exam_id = call(api, TUTOR, "POST", "/api/exams", {
        "title": "Sweep test", "durationMinutes": 5, "opensAt": iso(-10), "closesAt": iso(60),
        "questions": [{"kind": "MCQ", "prompt": "?", "options": ["a", "b"], "correctOption": 0}]})[1]["id"]
    attempt_id = call(api, STUDENT2, "POST", f"/api/exams/{exam_id}/start")[1]["attempt"]["id"]
    db.query("UPDATE attempt SET deadline = now() - interval '5 minutes' WHERE id = CAST(:id AS uuid)", id=attempt_id)
    assert marker.handler({"sweep": True}, None)["autoSubmitted"] >= 1
    assert db.one("SELECT status, auto_submitted FROM attempt WHERE id = CAST(:id AS uuid)", id=attempt_id) == \
        {"status": "SUBMITTED", "auto_submitted": True}


def test_walkthrough_2_upload_and_tutor_notified(modules, capsys):
    api, _, processor = modules
    status, created = call(api, TUTOR, "POST", "/api/assignments", {"title": "Design Report", "dueAt": iso(60 * 24)})
    assert status == 201
    assignment_id = created["id"]

    # 1. student asks for an upload slot: presigned POST limited to 50 MB + PDF
    status, slot = call(api, STUDENT, "POST", f"/api/assignments/{assignment_id}/upload-url",
                        {"fileName": "report.pdf", "contentType": "application/pdf", "size": 40 * 1024 * 1024})
    assert status == 200, slot
    assert slot["url"] == "https://uploads-test-bucket.s3.ap-southeast-2.amazonaws.com/"
    assert slot["fields"]["key"].startswith(f"submissions/{assignment_id}/{STUDENT['sub']}/")
    assert slot["fields"]["Content-Type"] == "application/pdf" and "policy" in slot["fields"]
    assert call(api, STUDENT, "POST", f"/api/assignments/{assignment_id}/upload-url",
                {"fileName": "big.pdf", "contentType": "application/pdf", "size": 60 * 1024 * 1024})[0] == 400

    # 2. browser uploads to S3 (simulated) -> S3 event -> SQS -> processor
    key = slot["fields"]["key"]
    fake_s3 = mock.Mock()
    fake_s3.head_object.return_value = {"ContentLength": 40 * 1024 * 1024, "ContentType": "application/pdf",
                                        "VersionId": "v123", "Metadata": {"original-name": "report.pdf"}}
    fake_s3.get_object.return_value = {"Body": mock.Mock(read=lambda: b"%PDF-1.7")}
    s3_event = {"Records": [{"s3": {"bucket": {"name": "uploads-test-bucket"}, "object": {"key": key}}}]}
    sqs_event = {"Records": [{"messageId": "u1", "body": json.dumps(s3_event)}]}
    with mock.patch.object(processor, "_s3_client", return_value=fake_s3):
        capsys.readouterr()
        assert processor.handler(sqs_event, None) == {"batchItemFailures": []}
        out = capsys.readouterr().out
        assert "New submission" in out and TUTOR["email"] in out          # tutor notified
        assert "Submission receipt" in out and STUDENT["email"] in out    # student receipt
        # SQS redelivers the same event: recorded once, no duplicate emails
        assert processor.handler(sqs_event, None) == {"batchItemFailures": []}
        assert "New submission" not in capsys.readouterr().out

    # 3. tutor sees it with a short-lived download link
    status, subs = call(api, TUTOR, "GET", f"/api/assignments/{assignment_id}/submissions")
    assert status == 200 and len(subs["submissions"]) == 1
    assert subs["submissions"][0]["status"] == "RECEIVED" and "X-Amz-Signature" in subs["submissions"][0]["downloadUrl"]

    # student view shows the receipt status
    mine = [a for a in call(api, STUDENT, "GET", "/api/assignments")[1]["assignments"] if a["id"] == assignment_id][0]
    assert mine["submission_status"] == "RECEIVED"


def test_renamed_file_is_rejected(modules, capsys):
    api, _, processor = modules
    assignment_id = call(api, TUTOR, "POST", "/api/assignments", {"title": "Zip lab", "dueAt": iso(60)})[1]["id"]
    key = f"submissions/{assignment_id}/{STUDENT['sub']}/abc.zip"
    fake_s3 = mock.Mock()
    fake_s3.head_object.return_value = {"ContentLength": 1000, "ContentType": "application/zip", "Metadata": {}}
    fake_s3.get_object.return_value = {"Body": mock.Mock(read=lambda: b"MZ\x90\x00")}  # an .exe renamed to .zip
    body = json.dumps({"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]})
    with mock.patch.object(processor, "_s3_client", return_value=fake_s3):
        capsys.readouterr()
        processor.handler({"Records": [{"messageId": "z", "body": body}]}, None)
        assert "Submission problem" in capsys.readouterr().out
