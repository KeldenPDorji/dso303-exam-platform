"""PostgreSQL access through RDS Proxy over TLS, using pg8000 (pure Python).

* Credentials come from the RDS-managed secret in Secrets Manager (rotated
  automatically). They are cached per Lambda container and re-read once if
  authentication fails after a rotation.
* One connection is reused per warm container. RDS Proxy multiplexes these onto
  a small pool of real database connections, so thousands of concurrent Lambda
  executions cannot exhaust PostgreSQL's max_connections.
* If the connection drops (e.g. during a Multi-AZ failover) the statement is
  retried once on a fresh connection. All writes in this app are idempotent.
"""
import json
import logging
import os
import ssl

import pg8000.native
from pg8000.exceptions import DatabaseError, InterfaceError

log = logging.getLogger(__name__)

_creds = None
_conn = None


def _load_credentials(force=False):
    global _creds
    if _creds is None or force:
        if os.environ.get("DB_PASSWORD"):  # local development / integration tests only
            _creds = {"username": os.environ.get("DB_USER", "postgres"), "password": os.environ["DB_PASSWORD"]}
        else:
            import boto3

            raw = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])
            _creds = json.loads(raw["SecretString"])
    return _creds


def _tls_context():
    """Verified TLS. RDS Proxy presents a publicly trusted certificate; a direct RDS
    connection presents an RDS CA certificate, so the RDS CA bundle is added when present."""
    context = ssl.create_default_context()
    bundle = os.environ.get("DB_CA_BUNDLE")
    if bundle and os.path.exists(bundle):
        context.load_verify_locations(cafile=bundle)
    return context


def _connect():
    use_tls = os.environ.get("DB_TLS", "true") != "false"
    kwargs = {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ.get("DB_PORT", "5432")),
        "database": os.environ.get("DB_NAME", "exams"),
        "ssl_context": _tls_context() if use_tls else None,
        "timeout": 10,
        "application_name": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "exam-platform"),
    }
    creds = _load_credentials()
    try:
        return pg8000.native.Connection(user=creds["username"], password=creds["password"], **kwargs)
    except DatabaseError as exc:
        log.warning("Database login failed, re-reading rotated secret: %s", exc)
        creds = _load_credentials(force=True)
        return pg8000.native.Connection(user=creds["username"], password=creds["password"], **kwargs)


def _connection():
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def _reset():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001 - connection is already broken
            pass
    _conn = None


def rows(conn, sql, **params):
    """Run a statement on a specific connection and return rows as dicts."""
    result = conn.run(sql, **params) or []
    columns = [c["name"] for c in (conn.columns or [])]
    return [dict(zip(columns, r)) for r in result] if columns else []


def query(sql, **params):
    """Run one statement (autocommit) and return rows as a list of dicts."""
    for attempt in (1, 2):
        conn = _connection()
        try:
            return rows(conn, sql, **params)
        except (InterfaceError, ConnectionError, OSError, ssl.SSLError) as exc:
            log.warning("Database connection lost (attempt %s): %s", attempt, exc)
            _reset()
            if attempt == 2:
                raise
    return []


def one(sql, **params):
    result = query(sql, **params)
    return result[0] if result else None


def transaction(work):
    """Run work(conn) inside a single transaction."""
    conn = _connection()
    try:
        conn.run("START TRANSACTION")
        result = work(conn)
        conn.run("COMMIT")
        return result
    except Exception:
        try:
            conn.run("ROLLBACK")
        except Exception:  # noqa: BLE001
            _reset()
        raise
