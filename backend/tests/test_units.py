"""Fast unit tests (no AWS, no database). Run in CI on every pull request."""
from decimal import Decimal

import pytest

from common.auth import parse_groups, principal_from_event
from common.http import HttpError
from common.marking import score_attempt
from common.validation import safe_file_name, validate_answers, validate_exam, validate_upload

MB = 1024 * 1024


# ---------------------------------------------------------------- marking
QUESTIONS = [
    {"id": "q1", "kind": "MCQ", "correct_option": 1, "marks": 1},
    {"id": "q2", "kind": "MCQ", "correct_option": 0, "marks": Decimal("2.5")},
    {"id": "q3", "kind": "SHORT", "correct_option": None, "marks": 4},
]


def test_scores_only_correct_mcqs():
    assert score_attempt(QUESTIONS, {"q1": 1, "q2": 3, "q3": "essay"}) == (Decimal("1"), Decimal("3.5"))


def test_unanswered_and_bad_values_score_zero():
    assert score_attempt(QUESTIONS, {"q1": True, "q2": "abc"}) == (Decimal("0"), Decimal("3.5"))
    assert score_attempt(QUESTIONS, None) == (Decimal("0"), Decimal("3.5"))


def test_full_marks():
    assert score_attempt(QUESTIONS, {"q1": 1, "q2": 0}) == (Decimal("3.5"), Decimal("3.5"))


# ---------------------------------------------------------------- auth
def test_parse_groups_formats():
    assert parse_groups("[Tutors Admins]") == ["Tutors", "Admins"]
    assert parse_groups('["Students"]') == ["Students"]
    assert parse_groups(["Students"]) == ["Students"]
    assert parse_groups(None) == []


def _event(groups, token_use="id"):
    claims = {"sub": "abc", "email": "s@uni.edu", "cognito:groups": groups, "token_use": token_use}
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def test_principal_role_priority():
    assert principal_from_event(_event("[Students Admins]")).role == "Admins"


def test_principal_requires_group_and_id_token():
    with pytest.raises(HttpError) as err:
        principal_from_event(_event("[]"))
    assert err.value.status == 403
    with pytest.raises(HttpError) as err:
        principal_from_event(_event("[Students]", token_use="access"))
    assert err.value.status == 401


def test_student_cannot_act_as_tutor():
    p = principal_from_event(_event("[Students]"))
    with pytest.raises(HttpError) as err:
        p.require("Tutors", "Admins")
    assert err.value.status == 403


# ---------------------------------------------------------------- validation
def test_upload_accepts_pdf_and_zip_up_to_50mb():
    assert validate_upload("Report.PDF", "application/pdf", 40 * MB, 50 * MB)[:2] == (".pdf", "application/pdf")
    assert validate_upload("code.zip", "application/x-zip-compressed", 10, 50 * MB)[1] == "application/zip"


@pytest.mark.parametrize("name,ctype,size", [
    ("virus.exe", "application/octet-stream", 10),
    ("report.pdf", "application/zip", 10),
    ("report.pdf", "application/pdf", 50 * MB + 1),
    ("report.pdf", "application/pdf", 0),
])
def test_upload_rejections(name, ctype, size):
    with pytest.raises(HttpError):
        validate_upload(name, ctype, size, 50 * MB)


def test_safe_file_name_strips_paths_and_unicode():
    assert safe_file_name("../../etc/passwd") == "passwd"
    assert safe_file_name("C:\\Users\\me\\Résumé final.pdf") == "R_sum_ final.pdf"


def test_validate_exam():
    exam = validate_exam({
        "title": "Midterm", "durationMinutes": 60,
        "opensAt": "2026-11-20T09:00:00Z", "closesAt": "2026-11-20T12:00:00Z",
        "questions": [
            {"kind": "MCQ", "prompt": "2+2?", "options": ["3", "4"], "correctOption": 1},
            {"kind": "SHORT", "prompt": "Explain", "marks": 5},
        ],
    })
    assert exam["duration"] == 60 and len(exam["questions"]) == 2
    assert exam["questions"][0]["correct"] == 1


@pytest.mark.parametrize("change", [
    {"durationMinutes": 2},
    {"closesAt": "2026-11-20T08:00:00Z"},
    {"opensAt": "2026-11-20T09:00:00"},  # no timezone
    {"questions": [{"kind": "MCQ", "prompt": "x", "options": ["a", "b"], "correctOption": 5}]},
    {"questions": [{"kind": "ESSAY", "prompt": "x"}]},
])
def test_validate_exam_rejects(change):
    base = {"title": "Midterm", "durationMinutes": 60, "opensAt": "2026-11-20T09:00:00Z",
            "closesAt": "2026-11-20T12:00:00Z", "questions": [{"kind": "SHORT", "prompt": "x"}]}
    with pytest.raises(HttpError):
        validate_exam({**base, **change})


def test_validate_answers():
    assert validate_answers({"a": 1, "b": "text", "c": None}) == {"a": 1, "b": "text"}
    with pytest.raises(HttpError):
        validate_answers({"a": "x" * 5001})
    with pytest.raises(HttpError):
        validate_answers({"a": True})
