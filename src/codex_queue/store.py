from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3


DEFAULTS = {
    "parallel": "1",
    "priority": "5",
    "branch": "per-task",
    "base": "",
    "on_abort": "pause",
    "sync": "auto",
    "language": "en",
    "planner_model": "",
    "implementation_model": "",
    "allowed_models": "",
    "idea_planning": "manual",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    branch TEXT CHECK (branch IN ('per-task', 'current')),
    base TEXT,
    retired INTEGER NOT NULL DEFAULT 0,
    planner_model TEXT,
    implementation_model TEXT,
    allowed_models TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 9),
    status TEXT NOT NULL DEFAULT 'QUEUED',
    completed_at TEXT,
    idea_id INTEGER REFERENCES ideas(id),
    abstract TEXT NOT NULL DEFAULT '',
    implementation_model TEXT
);
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    planned_at TEXT,
    planning_error TEXT
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status, priority, id);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    number INTEGER NOT NULL,
    path TEXT NOT NULL,
    mode TEXT NOT NULL,
    base TEXT,
    prompt TEXT NOT NULL,
    original_branch TEXT,
    branch TEXT,
    start_commit TEXT,
    end_commit TEXT,
    session TEXT,
    phase TEXT NOT NULL DEFAULT 'PREPARING',
    reason TEXT,
    report TEXT,
    answer TEXT,
    retry_at REAL,
    failures INTEGER NOT NULL DEFAULT 0,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    implementation_model TEXT,
    UNIQUE(task_id, number)
);
CREATE TABLE IF NOT EXISTS task_actions (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    action TEXT NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS remote_links (
    list TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS remote_tasks (
    connection TEXT NOT NULL,
    uid TEXT NOT NULL,
    task_id INTEGER UNIQUE NOT NULL REFERENCES tasks(id),
    list TEXT NOT NULL,
    url TEXT NOT NULL,
    etag TEXT,
    writeback TEXT,
    pending_project INTEGER REFERENCES projects(id),
    pending_remove INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(connection, uid)
);
"""

WORKFLOW_COLUMNS = {
    "ideas": ("planning_error TEXT",),
    "projects": ("planner_model TEXT", "implementation_model TEXT", "allowed_models TEXT"),
    "tasks": (
        "idea_id INTEGER REFERENCES ideas(id)", "abstract TEXT NOT NULL DEFAULT ''",
        "implementation_model TEXT",
    ),
    "attempts": ("implementation_model TEXT",),
}


def queue_folder(create=True):
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    folder = Path(os.environ.get("CQ_WORKER_DATA_DIR") or home / "CodexQueue").resolve()
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def saved_language():
    path = queue_folder(create=False) / "queue.sqlite3"
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                return get_setting(db, "language", DEFAULTS["language"])
        except sqlite3.Error:
            pass
    return DEFAULTS["language"]


@contextmanager
def open_queue():
    db = sqlite3.connect(queue_folder() / "queue.sqlite3", timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(SCHEMA)
        with write(db):
            for table, definitions in WORKFLOW_COLUMNS.items():
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for definition in definitions:
                    if definition.split()[0] not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        yield db
    finally:
        db.close()


@contextmanager
def write(db):
    with db:
        db.execute("BEGIN IMMEDIATE")
        yield


def settings(db):
    values = dict(db.execute("SELECT key, value FROM settings"))
    return {key: values.get(key, default) for key, default in DEFAULTS.items()}


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else row[0]


def set_setting(db, key, value):
    db.execute(
        """INSERT INTO settings(key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )


def update(db, table, row_id, values):
    if values:
        assignments = ", ".join(f"{column} = ?" for column in values)
        db.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?",
            (*values.values(), row_id),
        )
