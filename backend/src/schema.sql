-- Exam platform schema (PostgreSQL 16). Idempotent: the migrate Lambda runs it on every deploy.
-- NOTE: statements are split on semicolons, so do not put semicolons inside comments or strings.

CREATE TABLE IF NOT EXISTS app_user (
  sub           TEXT PRIMARY KEY,
  email         TEXT NOT NULL,
  role          TEXT NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS exam (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title             TEXT NOT NULL,
  duration_minutes  INT NOT NULL CHECK (duration_minutes BETWEEN 5 AND 600),
  opens_at          TIMESTAMPTZ NOT NULL,
  closes_at         TIMESTAMPTZ NOT NULL,
  created_by        TEXT NOT NULL,
  results_released  BOOLEAN NOT NULL DEFAULT false,
  released_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (closes_at > opens_at)
);

CREATE TABLE IF NOT EXISTS question (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id         UUID NOT NULL REFERENCES exam(id) ON DELETE CASCADE,
  position        INT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('MCQ', 'SHORT')),
  prompt          TEXT NOT NULL,
  options         JSONB,
  correct_option  INT,
  marks           NUMERIC(6,2) NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS question_exam_idx ON question (exam_id, position);

-- One attempt per student per exam. Autosave overwrites "answers" in place,
-- guarded by client_seq so a late retry can never overwrite newer answers.
CREATE TABLE IF NOT EXISTS attempt (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id         UUID NOT NULL REFERENCES exam(id) ON DELETE CASCADE,
  student_sub     TEXT NOT NULL,
  student_email   TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'IN_PROGRESS' CHECK (status IN ('IN_PROGRESS', 'SUBMITTED', 'MARKED')),
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  deadline        TIMESTAMPTZ NOT NULL,
  answers         JSONB NOT NULL DEFAULT '{}'::jsonb,
  client_seq      BIGINT NOT NULL DEFAULT 0,
  saved_at        TIMESTAMPTZ,
  submitted_at    TIMESTAMPTZ,
  auto_submitted  BOOLEAN NOT NULL DEFAULT false,
  mcq_score       NUMERIC(8,2),
  mcq_total       NUMERIC(8,2),
  short_score     NUMERIC(8,2),
  marked_at       TIMESTAMPTZ,
  UNIQUE (exam_id, student_sub)
);
CREATE INDEX IF NOT EXISTS attempt_status_deadline_idx ON attempt (status, deadline);

CREATE TABLE IF NOT EXISTS assignment (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL,
  due_at       TIMESTAMPTZ NOT NULL,
  tutor_sub    TEXT NOT NULL,
  tutor_email  TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- s3_key is UNIQUE so a redelivered S3 event (SQS is at-least-once) is recorded only once.
CREATE TABLE IF NOT EXISTS submission (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  assignment_id  UUID NOT NULL REFERENCES assignment(id) ON DELETE CASCADE,
  student_sub    TEXT NOT NULL,
  student_email  TEXT,
  s3_key         TEXT NOT NULL UNIQUE,
  s3_version_id  TEXT,
  file_name      TEXT,
  size_bytes     BIGINT NOT NULL,
  content_type   TEXT,
  status         TEXT NOT NULL CHECK (status IN ('RECEIVED', 'REJECTED')),
  reject_reason  TEXT,
  late           BOOLEAN NOT NULL DEFAULT false,
  notified       BOOLEAN NOT NULL DEFAULT false,
  received_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS submission_assignment_idx ON submission (assignment_id, received_at DESC);
