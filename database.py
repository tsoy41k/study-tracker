"""
SQLite database layer for the Study Tracker app.

Tables:
  users          - registered students (numeric id + password hash + analysis period)
  subjects       - subjects per user (6 defaults auto-created on register)
  study_records  - completed study sessions (timer results)
  grades         - grades (0-100) entered manually per subject; used by ML
                   to predict the next grade based on accumulated study time.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# DB location: override with DB_PATH env var (used on Render's persistent disk).
# Default = local file next to this script for development.
_default = Path(__file__).parent / "study_tracker.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_default)))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# Default subjects auto-created for every new student
DEFAULT_SUBJECTS = [
    "Internet of Things",
    "Digital Marketing",
    "System analysis and design",
    "Distributed systems and cloud computing",
    "Technology law",
    "Business information systems project",
]


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they do not exist."""
    with db_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id                  INTEGER PRIMARY KEY,           -- numeric student ID
                password_hash       TEXT NOT NULL,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                analysis_period_days INTEGER,                       -- NULL until chosen
                analysis_started_at TEXT                            -- ISO datetime
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subjects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS study_records (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                subject_id      INTEGER NOT NULL,
                started_at      TEXT NOT NULL,
                ended_at        TEXT NOT NULL,
                duration_sec    INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS grades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                subject_id  INTEGER NOT NULL,
                grade       REAL NOT NULL,                  -- 0..100
                note        TEXT,                           -- optional: "midterm", "quiz", etc.
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            );
            """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_records_user    ON study_records(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_records_subject ON study_records(subject_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_grades_user     ON grades(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_grades_subject  ON grades(subject_id);")


# ---------- Users ----------

def create_user(user_id: int, password_hash: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, password_hash) VALUES (?, ?)",
            (user_id, password_hash),
        )
        # Auto-create 6 default subjects
        for name in DEFAULT_SUBJECTS:
            cur.execute(
                "INSERT INTO subjects (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )


def get_user(user_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_analysis_period(user_id: int, days: int) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET analysis_period_days = ?, analysis_started_at = datetime('now')
            WHERE id = ?
            """,
            (days, user_id),
        )


def analysis_status(user: dict) -> dict:
    """
    Return analysis-period info for the dashboard:
      { configured, immediate, period_days, started_at, ends_at, finished, days_left }

    Special case: period_days == 0 means "immediate mode" — recommendations
    are unlocked right after registration with no waiting period.
    """
    if user.get("analysis_period_days") is None or not user.get("analysis_started_at"):
        return {"configured": False}

    started = datetime.fromisoformat(user["analysis_started_at"])
    period_days = int(user["analysis_period_days"])

    if period_days == 0:
        return {
            "configured": True,
            "immediate":  True,
            "period_days": 0,
            "started_at": started.isoformat(sep=" ", timespec="seconds"),
            "ends_at":   started.isoformat(sep=" ", timespec="seconds"),
            "finished":  True,
            "days_left": 0,
        }

    ends = started + timedelta(days=period_days)
    now = datetime.now()
    finished = now >= ends
    days_left = max(0, (ends - now).days + (0 if finished else 1))

    return {
        "configured": True,
        "immediate":  False,
        "period_days": period_days,
        "started_at": started.isoformat(sep=" ", timespec="seconds"),
        "ends_at":   ends.isoformat(sep=" ", timespec="seconds"),
        "finished":  finished,
        "days_left": days_left,
    }


# ---------- Subjects ----------

def add_subject(user_id: int, name: str) -> int:
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO subjects (user_id, name) VALUES (?, ?)",
            (user_id, name.strip()),
        )
        return cur.lastrowid


def get_subjects(user_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM subjects WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_subject(user_id: int, subject_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM subjects WHERE id = ? AND user_id = ?",
            (subject_id, user_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_subject(user_id: int, subject_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM subjects WHERE id = ? AND user_id = ?",
            (subject_id, user_id),
        )
        return cur.rowcount > 0


# ---------- Study records ----------

def add_record(user_id: int, subject_id: int, started_at: str,
               ended_at: str, duration_sec: int) -> int:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO study_records
                (user_id, subject_id, started_at, ended_at, duration_sec)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, subject_id, started_at, ended_at, duration_sec),
        )
        return cur.lastrowid


def get_records(user_id: int, subject_id: int | None = None, limit: int = 200):
    with db_cursor() as cur:
        if subject_id is None:
            cur.execute(
                """
                SELECT r.*, s.name AS subject_name
                FROM study_records r
                JOIN subjects s ON s.id = r.subject_id
                WHERE r.user_id = ?
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT r.*, s.name AS subject_name
                FROM study_records r
                JOIN subjects s ON s.id = r.subject_id
                WHERE r.user_id = ? AND r.subject_id = ?
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (user_id, subject_id, limit),
            )
        return [dict(r) for r in cur.fetchall()]


# ---------- Grades ----------

def add_grade(user_id: int, subject_id: int, grade: float, note: str = "") -> int:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO grades (user_id, subject_id, grade, note)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, subject_id, grade, note or ""),
        )
        return cur.lastrowid


def get_grades(user_id: int, subject_id: int | None = None):
    with db_cursor() as cur:
        if subject_id is None:
            cur.execute(
                """
                SELECT g.*, s.name AS subject_name
                FROM grades g
                JOIN subjects s ON s.id = g.subject_id
                WHERE g.user_id = ?
                ORDER BY g.created_at DESC
                """,
                (user_id,),
            )
        else:
            cur.execute(
                """
                SELECT g.*, s.name AS subject_name
                FROM grades g
                JOIN subjects s ON s.id = g.subject_id
                WHERE g.user_id = ? AND g.subject_id = ?
                ORDER BY g.created_at ASC
                """,
                (user_id, subject_id),
            )
        return [dict(r) for r in cur.fetchall()]


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")
