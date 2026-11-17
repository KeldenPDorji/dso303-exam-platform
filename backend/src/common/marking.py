"""Pure auto-marking logic (no AWS or database imports, so it is unit-testable)."""
from decimal import Decimal


def score_attempt(questions, answers):
    """Auto-mark the multiple-choice questions of one attempt.

    questions: iterable of dicts with id, kind, correct_option, marks
    answers:   {question_id (str): chosen option index (int) | short-answer text (str)}
    Returns (score, total) over MCQ questions only. Short answers are left for the tutor.
    """
    answers = answers or {}
    score = Decimal("0")
    total = Decimal("0")
    for q in questions:
        if q["kind"] != "MCQ":
            continue
        marks = Decimal(str(q["marks"]))
        total += marks
        given = answers.get(str(q["id"]))
        if isinstance(given, bool) or given is None or q.get("correct_option") is None:
            continue
        try:
            if int(given) == int(q["correct_option"]):
                score += marks
        except (TypeError, ValueError):
            continue
    return score, total
