"""Input validation for the API (pure Python, unit-tested)."""
import re
from datetime import datetime

from .http import HttpError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UPLOAD_TYPES = {".pdf": "application/pdf", ".zip": "application/zip"}
ZIP_ALIASES = {"application/zip", "application/x-zip-compressed", "application/x-zip"}
MAX_ANSWER_CHARS = 5000
MAX_QUESTIONS = 200


def _int(value, field, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise HttpError(400, f"{field} must be a whole number between {lo} and {hi}")
    return value


def _number(value, field, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise HttpError(400, f"{field} must be a number between {lo} and {hi}")
    return value


def _text(value, field, lo, hi):
    text = str(value or "").strip()
    if not lo <= len(text) <= hi:
        raise HttpError(400, f"{field} must be {lo}-{hi} characters")
    return text


def parse_time(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HttpError(400, f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise HttpError(400, f"{field} must include a timezone (e.g. 2026-11-20T09:00:00Z)")
    return parsed


def validate_schedule(data):
    opens = parse_time(data.get("opensAt"), "opensAt")
    closes = parse_time(data.get("closesAt"), "closesAt")
    if closes <= opens:
        raise HttpError(400, "closesAt must be after opensAt")
    duration = _int(data.get("durationMinutes"), "durationMinutes", 5, 600)
    return {"opens_at": opens.isoformat(), "closes_at": closes.isoformat(), "duration": duration}


def validate_exam(data):
    exam = validate_schedule(data)
    exam["title"] = _text(data.get("title"), "title", 3, 200)
    questions = data.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise HttpError(400, f"questions must be a list of 1-{MAX_QUESTIONS} items")
    clean = []
    for i, q in enumerate(questions, start=1):
        if not isinstance(q, dict):
            raise HttpError(400, f"question {i} must be an object")
        kind = q.get("kind")
        item = {
            "kind": kind,
            "prompt": _text(q.get("prompt"), f"question {i} prompt", 1, 4000),
            "marks": _number(q.get("marks", 1), f"question {i} marks", 0.5, 100),
            "options": None,
            "correct": None,
        }
        if kind == "MCQ":
            options = q.get("options")
            if not isinstance(options, list) or not 2 <= len(options) <= 10:
                raise HttpError(400, f"question {i} needs 2-10 options")
            item["options"] = [_text(o, f"question {i} option", 1, 500) for o in options]
            item["correct"] = _int(q.get("correctOption"), f"question {i} correctOption", 0, len(options) - 1)
        elif kind != "SHORT":
            raise HttpError(400, f"question {i} kind must be MCQ or SHORT")
        clean.append(item)
    exam["questions"] = clean
    return exam


def validate_answers(answers):
    """Answers map question-id -> option index (MCQ) or text (short answer)."""
    if answers is None:
        return {}
    if not isinstance(answers, dict) or len(answers) > MAX_QUESTIONS:
        raise HttpError(400, "answers must be an object keyed by question id")
    clean = {}
    for key, value in answers.items():
        if not isinstance(key, str) or len(key) > 64:
            raise HttpError(400, "invalid question id in answers")
        if isinstance(value, bool):
            raise HttpError(400, "invalid answer value")
        if isinstance(value, int):
            clean[key] = value
        elif isinstance(value, str):
            if len(value) > MAX_ANSWER_CHARS:
                raise HttpError(400, f"answers are limited to {MAX_ANSWER_CHARS} characters")
            clean[key] = value
        elif value is not None:
            raise HttpError(400, "invalid answer value")
    return clean


def validate_client_seq(value, allow_zero=False):
    lo = 0 if allow_zero else 1
    return _int(value, "clientSeq", lo, 10**12)


def safe_file_name(name):
    """ASCII-only, path-free file name that is safe to store as S3 metadata."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .") or "upload"
    return cleaned[-100:]


def validate_upload(file_name, content_type, size, max_bytes):
    name = safe_file_name(file_name)
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in UPLOAD_TYPES:
        raise HttpError(400, "Only PDF or ZIP files can be submitted")
    expected = UPLOAD_TYPES[ext]
    given = str(content_type or "").lower()
    if not (given == expected or (expected == "application/zip" and given in ZIP_ALIASES)):
        raise HttpError(400, "File type does not match its extension")
    size = _int(size, "size", 1, max_bytes)
    return ext, expected, name, size


def validate_email(value):
    email = str(value or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HttpError(400, "A valid email address is required")
    return email
