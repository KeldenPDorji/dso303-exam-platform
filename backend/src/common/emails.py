"""Email message builders. Workers put these on the notifications queue and the
notifier Lambda sends them with SES. Grades are never put in email (privacy):
students get a link and see their marks after signing in."""


def _mb(size_bytes):
    return f"{size_bytes / 1_048_576:.1f} MB"


def submission_receipt(to, assignment_title, file_name, size_bytes, received_at, version_id, late):
    late_note = "\nNote: this submission was received after the due date and is marked LATE.\n" if late else ""
    return {
        "to": to,
        "subject": f"Submission receipt: {assignment_title}",
        "body": (
            f"We received your submission for \"{assignment_title}\".\n\n"
            f"File: {file_name} ({_mb(size_bytes)})\n"
            f"Received: {received_at:%Y-%m-%d %H:%M:%S %Z}\n"
            f"Receipt ID (storage version): {version_id or 'n/a'}\n{late_note}\n"
            "Keep this email as proof of submission."
        ),
    }


def submission_rejected(to, assignment_title, file_name, reason):
    return {
        "to": to,
        "subject": f"Submission problem: {assignment_title}",
        "body": (
            f"Your upload \"{file_name}\" for \"{assignment_title}\" could not be accepted: {reason}.\n\n"
            "Please upload a valid PDF or ZIP file (max 50 MB) before the due date."
        ),
    }


def tutor_new_submission(to, student_email, assignment_title, file_name, size_bytes, late):
    return {
        "to": to,
        "subject": f"New submission{' (LATE)' if late else ''}: {assignment_title}",
        "body": (
            f"{student_email or 'A student'} submitted \"{file_name}\" ({_mb(size_bytes)}) "
            f"for \"{assignment_title}\".\n\nOpen the platform to download and mark it."
        ),
    }


def results_released(to, exam_title, site_url):
    return {
        "to": to,
        "subject": f"Results released: {exam_title}",
        "body": f"Your results for \"{exam_title}\" are now available.\n\nSign in to view them: {site_url}",
    }
