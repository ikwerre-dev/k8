import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

NEW_DB_DIR = "/db"
FALLBACK_DB_DIR = os.path.join(os.path.dirname(__file__), "db")
DB_PATH = os.path.join(NEW_DB_DIR, "app.db")
OLD_DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")
try:
    os.makedirs(NEW_DB_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH) and os.path.exists(OLD_DB_PATH):
        try:
            os.replace(OLD_DB_PATH, DB_PATH)
        except Exception:
            pass
except Exception:
    pass
use_path = DB_PATH
if not os.path.isdir(NEW_DB_DIR) or not os.access(NEW_DB_DIR, os.W_OK):
    use_path = os.path.join(FALLBACK_DB_DIR, "app.db")
    try:
        os.makedirs(FALLBACK_DB_DIR, exist_ok=True)
        if not os.path.exists(use_path) and os.path.exists(OLD_DB_PATH):
            try:
                os.replace(OLD_DB_PATH, use_path)
            except Exception:
                pass
    except Exception:
        pass
DB_PATH = use_path
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    conn = get_conn()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                app_id TEXT PRIMARY KEY,
                container_id TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT,
                timecreated TEXT,
                cpu REAL,
                ram INTEGER
            )
            """
        )


def upsert_application(app_id: str, container_id: Optional[str]) -> None:
    conn = get_conn()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with conn:
        cur = conn.execute("SELECT app_id FROM applications WHERE app_id=?", (app_id,))
        if cur.fetchone():
            conn.execute("UPDATE applications SET container_id=? WHERE app_id=?", (container_id, app_id))
        else:
            conn.execute(
                "INSERT INTO applications(app_id, container_id, created_at) VALUES(?,?,?)",
                (app_id, container_id, ts),
            )


def list_applications() -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.execute("SELECT app_id, container_id, created_at FROM applications")
    rows = cur.fetchall()
    return [{"app_id": r["app_id"], "container_id": r["container_id"], "created_at": r["created_at"]} for r in rows]


def add_metric(app_id: str, cpu: float, ram: int, timecreated: Optional[str] = None) -> None:
    conn = get_conn()
    ts = timecreated or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with conn:
        conn.execute(
            "INSERT INTO metrics(app_id, timecreated, cpu, ram) VALUES(?,?,?,?)",
            (app_id, ts, cpu, ram),
        )


def get_metrics(app_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.execute(
        "SELECT app_id, timecreated, cpu, ram FROM metrics WHERE app_id=? ORDER BY timecreated DESC LIMIT ?",
        (app_id, limit),
    )
    rows = cur.fetchall()
    return [
        {"app_id": r["app_id"], "timecreated": r["timecreated"], "cpu": r["cpu"], "ram": r["ram"]}
        for r in rows
    ]