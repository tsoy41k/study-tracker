"""
SQLite database layer for the Study Tracker app.

All SQL lives here so the rest of the code never writes raw SQL. Every other
module (app.py, ml_model.py) calls the helper functions below.

Tables:
  users          - registered students (numeric id + password hash + analysis period)
  subjects       - subjects per user (6 defaults auto-created on register)
  study_records  - completed study sessions (timer results)

The ML models work purely on study_records (time data), so there is no
grades table.
"""

import os                                   # read the optional DB_PATH env var
import sqlite3                              # the built-in SQLite driver
from contextlib import contextmanager      # to build the db_cursor() helper
from datetime import datetime, timedelta   # date math for the analysis period
from pathlib import Path                    # cross-platform filesystem paths

# DB location: override with the DB_PATH env var (e.g. a Render disk path).
# Default = a local file next to this script, used during development.
_default = Path(__file__).parent / "study_tracker.db"          # default file path
DB_PATH = Path(os.environ.get("DB_PATH", str(_default)))       # env override or default
DB_PATH.parent.mkdir(parents=True, exist_ok=True)             # ensure the folder exists


# The six subjects every new student starts with. They can add/delete their own.
DEFAULT_SUBJECTS = [
    "Internet of Things",
    "Digital Marketing",
    "System analysis and design",
    "Distributed systems and cloud computing",
    "Technology law",
    "Business information systems project",
]


def get_connection():
    """Open a new SQLite connection configured the way we want it."""
    conn = sqlite3.connect(DB_PATH)             # connect to the database file
    conn.row_factory = sqlite3.Row              # rows behave like dicts (row["col"])
    conn.execute("PRAGMA foreign_keys = ON;")   # enable ON DELETE CASCADE enforcement
    return conn


@contextmanager
def db_cursor():
    """
    Context manager that yields a cursor and handles commit/rollback/close.

    Usage:
        with db_cursor() as cur:
            cur.execute(...)
    On success it commits; on any exception it rolls back and re-raises; it
    always closes the connection.
    """
    conn = get_connection()        # open the connection
    try:
        cur = conn.cursor()        # create a cursor to run statements
        yield cur                  # hand it to the caller's `with` block
        conn.commit()              # if no exception, persist the changes
    except Exception:
        conn.rollback()            # on error, undo any partial changes
        raise                      # re-raise so the caller sees the error
    finally:
        conn.close()               # always release the connection


def init_db():
    """Create all tables and indexes if they do not already exist."""
    with db_cursor() as cur:
        # --- users: one row per registered student ---
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id                  INTEGER PRIMARY KEY,           -- numeric student ID
                password_hash       TEXT NOT NULL,                 -- hashed password
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                analysis_period_days INTEGER,                       -- NULL until chosen
                analysis_started_at TEXT                            -- ISO datetime
            );
            """
        )

        # --- subjects: one row per subject per user (unique name per user) ---
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subjects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, name),                              -- no duplicate names
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

        # --- study_records: one row per completed timer session ---
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS study_records (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                subject_id      INTEGER NOT NULL,
                started_at      TEXT NOT NULL,                       -- session start
                ended_at        TEXT NOT NULL,                       -- session end
                duration_sec    INTEGER NOT NULL,                    -- length in seconds
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            );
            """
        )

        # Indexes speed up the most common lookups (by user / by subject).
        cur.execute("CREATE INDEX IF NOT EXISTS idx_records_user    ON study_records(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_records_subject ON study_records(subject_id);")


# ---------- Users ----------

def create_user(user_id: int, password_hash: str) -> None:
    """Insert a new user AND auto-create their 6 default subjects."""
    with db_cursor() as cur:
        # Insert the user row.
        cur.execute(
            "INSERT INTO users (id, password_hash) VALUES (?, ?)",
            (user_id, password_hash),
        )
        # Give the new user the six standard subjects to start with.
        for name in DEFAULT_SUBJECTS:
            cur.execute(
                "INSERT INTO subjects (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )


def get_user(user_id: int):
    """Return the user row as a dict, or None if not found."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()                       # at most one row (id is PK)
        return dict(row) if row else None          # convert Row → dict (or None)


def set_analysis_period(user_id: int, days: int) -> None:
    """Store the chosen analysis period and stamp the start time as 'now'."""
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
    Compute the analysis-period status used by the dashboard banner:
      { configured, immediate, period_days, started_at, ends_at, finished, days_left }

    Special case: period_days == 0 means "immediate mode" — recommendations
    are unlocked right after registration with no waiting period.
    """
    # If the period was never set, report "not configured".
    if user.get("analysis_period_days") is None or not user.get("analysis_started_at"):
        return {"configured": False}

    started = datetime.fromisoformat(user["analysis_started_at"])  # when it began
    period_days = int(user["analysis_period_days"])                # chosen length

    # Immediate mode: treat the period as already finished.
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

    ends = started + timedelta(days=period_days)        # when the period ends
    now = datetime.now()                                # current time
    finished = now >= ends                              # has it elapsed?
    # Days remaining (0 if finished; otherwise round up so "today" counts).
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
    """Insert one subject for a user and return its new id."""
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO subjects (user_id, name) VALUES (?, ?)",
            (user_id, name.strip()),
        )
        return cur.lastrowid                       # id of the row we just inserted


def get_subjects(user_id: int):
    """Return all subjects for a user (oldest first) as a list of dicts."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM subjects WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]   # convert each Row to a dict


def get_subject(user_id: int, subject_id: int):
    """Return one subject (verifying it belongs to the user), or None."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM subjects WHERE id = ? AND user_id = ?",
            (subject_id, user_id),                 # user_id guards against cross-user access
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_subject(user_id: int, subject_id: int) -> bool:
    """Delete a subject (cascades to its study records). Return True if deleted."""
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM subjects WHERE id = ? AND user_id = ?",
            (subject_id, user_id),
        )
        return cur.rowcount > 0                     # rowcount 0 means nothing matched


# ---------- Study records ----------

def add_record(user_id: int, subject_id: int, started_at: str,
               ended_at: str, duration_sec: int) -> int:
    """Insert one completed study session and return its id."""
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
    """
    Return study records joined with the subject name.
    If subject_id is given, filter to that subject; otherwise return all.
    Newest first, capped at `limit` rows.
    """
    with db_cursor() as cur:
        if subject_id is None:
            # All subjects for this user.
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
            # A single subject.
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


# Running this file directly just (re)creates the schema — handy for setup.
if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")
