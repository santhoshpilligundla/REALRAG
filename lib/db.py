from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import pgserver
import pgserver._commands as _pgcmds
import psycopg

from lib.config import get_settings

# Patch pgserver's pg_ctl timeout from 10s → 120s so postgres has enough
# time to complete WAL recovery after an unclean shutdown.
_orig_command = _pgcmds.command
def _patched_command(cmd, pgdata, user, timeout=120, **kwargs):
    return _orig_command(cmd, pgdata, user, timeout=max(timeout, 120), **kwargs)
_pgcmds.command = _patched_command


@lru_cache
def _server() -> pgserver.PostgresServer:
    settings = get_settings()
    data_dir = Path(settings.pg_data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(data_dir)


@lru_cache
def get_dsn() -> str:
    settings = get_settings()
    base_uri = _server().get_uri()
    parsed = urlparse(base_uri)
    target_db = settings.pg_db_name

    if parsed.path.lstrip("/") == target_db:
        return base_uri

    admin_uri = base_uri
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target_db,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{target_db}"')

    return parsed._replace(path=f"/{target_db}").geturl()


def get_conn() -> psycopg.Connection:
    return psycopg.connect(get_dsn())


def apply_schema() -> None:
    schema_dir = Path(__file__).resolve().parent.parent / "db" / "schema"
    sql_files = sorted(schema_dir.glob("*.sql"))
    if not sql_files:
        return
    with get_conn() as conn:
        for sql_file in sql_files:
            conn.execute(sql_file.read_text(encoding="utf-8"))
        conn.commit()

    # Bootstrap shipped reference data (idempotent).
    try:
        from lib.glossary import bootstrap_glossaries  # late import: avoid cycle
        bootstrap_glossaries()
    except Exception:
        pass
