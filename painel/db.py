"""Acesso ao SQLite (histórico, estados de alerta, destinatários e ajustes)."""
import json
import os
import sqlite3
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    overall TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS check_state (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    since TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notified_status TEXT,
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT,
    label TEXT NOT NULL,
    prev_status TEXT,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    notify_pending INTEGER NOT NULL DEFAULT 0,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    baseline INTEGER NOT NULL,
    last INTEGER NOT NULL,
    ref TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "alerts_enabled": "1",
    "min_severity": "ATENCAO",
    "cooldown_minutes": "30",
    "reminder_hours": "24",
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(text):
    return datetime.fromisoformat(text)


def connect(path):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- ajustes

def get_settings(conn):
    values = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ---------------------------------------------------------------- destinatários

def list_recipients(conn):
    return conn.execute("SELECT * FROM recipients ORDER BY email").fetchall()


def enabled_recipients(conn):
    return [r["email"] for r in conn.execute("SELECT email FROM recipients WHERE enabled = 1 ORDER BY email")]


def add_recipient(conn, email):
    conn.execute("INSERT INTO recipients(email, enabled, created_at) VALUES(?, 1, ?)", (email, now_iso()))


def toggle_recipient(conn, rid):
    conn.execute("UPDATE recipients SET enabled = 1 - enabled WHERE id = ?", (rid,))


def delete_recipient(conn, rid):
    conn.execute("DELETE FROM recipients WHERE id = ?", (rid,))


# ---------------------------------------------------------------- coletas e eventos

def save_snapshot(conn, ts, overall, data):
    conn.execute("INSERT INTO snapshots(ts, overall, data) VALUES(?, ?, ?)",
                 (ts, overall, json.dumps(data, ensure_ascii=False)))


def latest_snapshot(conn):
    row = conn.execute("SELECT data FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["data"]) if row else None


def prune(conn, retention_days, now):
    limit = (now - timedelta(days=retention_days)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM snapshots WHERE ts < ? AND id NOT IN "
                 "(SELECT id FROM snapshots ORDER BY id DESC LIMIT 1)", (limit,))
    events_limit = (now - timedelta(days=365)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM events WHERE ts < ?", (events_limit,))


def insert_event(conn, ts, kind, key, label, prev_status, status, message, notify=False):
    conn.execute(
        "INSERT INTO events(ts, kind, key, label, prev_status, status, message, notify_pending) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, kind, key, label, prev_status, status, message, 1 if notify else 0))


def recent_events(conn, limit=200, status=None):
    if status:
        return conn.execute("SELECT * FROM events WHERE status = ? ORDER BY id DESC LIMIT ?",
                            (status, limit)).fetchall()
    return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def count_events_by_status(conn):
    return {r["status"]: r["n"] for r in
            conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status")}


def list_counters(conn):
    return conn.execute("SELECT * FROM counters ORDER BY key").fetchall()
