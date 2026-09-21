"""One SQLite connection per short transaction; CAD work never holds a transaction."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("metadata", "payload", "result"):
        if key in result:
            result[key] = json.loads(result[key]) if result[key] else None
    return result


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self.transaction() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drawings (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, source_suffix TEXT NOT NULL,
                    current_revision TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT NOT NULL, drawing_id TEXT NOT NULL REFERENCES drawings(id),
                    number INTEGER NOT NULL, created_at TEXT NOT NULL, summary TEXT NOT NULL,
                    metadata TEXT NOT NULL, PRIMARY KEY(drawing_id, id));
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, drawing_id TEXT NOT NULL REFERENCES drawings(id),
                    title TEXT NOT NULL, busy INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, drawing_id TEXT NOT NULL REFERENCES drawings(id),
                    session_id TEXT REFERENCES sessions(id), kind TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, operation_id TEXT,
                    UNIQUE(drawing_id, operation_id));
            """)
            db.execute("INSERT OR IGNORE INTO projects VALUES (?, ?, ?)", ("default", "CAD 图纸工作台", now()))
            if "busy_since" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN busy_since REAL NOT NULL DEFAULT 0")
            db.execute("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running','cancelling')",
                       ("服务重启，任务已中断；可显式重试。", now()))
            db.execute("UPDATE drawings SET status='failed' WHERE current_revision IS NULL AND status='importing'")
            db.execute("UPDATE sessions SET busy=0")

    def one(self, sql: str, params=()):
        with self.transaction() as db:
            return decode(db.execute(sql, params).fetchone())

    def all(self, sql: str, params=()):
        with self.transaction() as db:
            return [decode(r) for r in db.execute(sql, params).fetchall()]

    def execute(self, sql: str, params=()):
        with self.transaction() as db:
            return db.execute(sql, params).rowcount
