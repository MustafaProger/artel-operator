from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
import uuid
from .config import DATA


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    (DATA / "runs").mkdir(mode=0o700, exist_ok=True)
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, operator_id TEXT, run_date TEXT, trigger TEXT, status TEXT, created_at TEXT, payload TEXT)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS scheduled_once ON runs(operator_id, run_date) WHERE trigger='schedule'")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        for row in conn.execute("SELECT id, payload FROM runs WHERE status IN ('queued','running')"):
            payload = json.loads(row["payload"])
            payload.update(status="failed", error="Процесс был остановлен до завершения. Доступен ручной повтор.", finished_at=now_iso())
            conn.execute("UPDATE runs SET status='failed',payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"]))


@contextmanager
def db():
    conn = sqlite3.connect(DATA / "operator.sqlite3", timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_run(operator_id, run_date, start, end, trigger):
    record = dict(id=uuid.uuid4().hex, operator_id=operator_id, run_date=str(run_date), trigger=trigger,
                  period_start=str(start), period_end=str(end), status="queued", created_at=now_iso(),
                  finished_at=None, error=None, files=[], report="", events=[])
    with db() as conn:
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", (record["id"], operator_id, str(run_date), trigger, "queued", record["created_at"], json.dumps(record)))
    return record


def save_run(record):
    with db() as conn:
        conn.execute("UPDATE runs SET status=?,payload=? WHERE id=?", (record["status"], json.dumps(record, ensure_ascii=False), record["id"]))


def runs(limit=100):
    with db() as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]


def get_run(ident):
    with db() as conn:
        row = conn.execute("SELECT payload FROM runs WHERE id=?", (ident,)).fetchone()
        if row:
            return json.loads(row[0])
    raise ValueError("Запуск не найден")


def setting(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))


def credentials():
    if os.getenv("GLOPRO_USERNAME") and os.getenv("GLOPRO_PASSWORD"):
        return os.environ["GLOPRO_USERNAME"], os.environ["GLOPRO_PASSWORD"]
    path = DATA / "credentials.json"
    if path.is_file():
        saved = json.loads(path.read_text())
        return saved["username"], saved["password"]
    return None


def save_credentials(username, password):
    path = DATA / "credentials.json"
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(dict(username=username, password=password), stream)
    os.replace(temporary, path)
    set_setting("connection_verified", False)
