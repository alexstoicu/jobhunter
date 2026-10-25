"""Tracker database (data/tracker.db): every job, its status and its history."""
import sqlite3
from datetime import datetime, timezone

from jobhunter import DATA

DB_PATH = DATA / "tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- feed kind: workday, phenom, ...
    employer      TEXT NOT NULL,
    external_id   TEXT NOT NULL,          -- id within the feed
    title         TEXT NOT NULL,
    location      TEXT,                   -- location text as the feed gives it
    url           TEXT,
    posted        TEXT,                   -- ISO date the posting went up (estimated for Workday)
    description   TEXT,
    salary_min    REAL,                   -- annual USD, parsed from the posting
    salary_max    REAL,
    work_mode     TEXT,                   -- remote / hybrid / onsite / unknown
    city          TEXT,                   -- resolved "City, ST" used for commute
    drive_min     REAL,
    drive_miles   REAL,
    lanes         TEXT,                   -- comma-separated lane keys matched by keywords
    fingerprint   TEXT NOT NULL,          -- employer|title|city, for repost detection
    status        TEXT NOT NULL,          -- new / filtered_out / candidate / ...
    status_reason TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    UNIQUE (source, employer, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs (fingerprint);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    job_id  INTEGER REFERENCES jobs (id),
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT
);

CREATE TABLE IF NOT EXISTS geo_cache (
    query  TEXT PRIMARY KEY,              -- "City, ST" or "origin->dest"
    lat    REAL,
    lon    REAL,
    minutes REAL,
    miles  REAL
);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    step          TEXT NOT NULL,          -- screen / tailor / verify / prefill
    model         TEXT,
    jobs          INTEGER,                -- jobs handled by the call
    input_tokens  INTEGER,                -- uncached input
    cache_write   INTEGER,
    cache_read    INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,                   -- API-equivalent cost reported by the CLI
    duration_ms   INTEGER,
    ok            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY,
    started   TEXT NOT NULL,
    finished  TEXT,
    step      TEXT NOT NULL,
    summary   TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first release; added to existing databases on connect.
MIGRATIONS = [
    ("jobs", "fit_score", "INTEGER"),      # Screener, 0-100
    ("jobs", "screen", "TEXT"),            # Screener output, JSON
    ("jobs", "screened_at", "TEXT"),
    ("jobs", "rank_score", "REAL"),        # fit score adjusted by lane, freshness, commute
    ("jobs", "queued_on", "TEXT"),         # date picked as one of the day's applications to prepare
    ("jobs", "resume", "TEXT"),            # Tailor plan after verification, JSON
    ("jobs", "resume_dir", "TEXT"),        # folder under private/applications with the .docx and .pdf
    ("jobs", "prepared_at", "TEXT"),
    ("jobs", "decided_at", "TEXT"),        # Alex's approve/reject on the dashboard
]


def connect(path=DB_PATH):
    path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, column, kind in MIGRATIONS:
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()
    return conn


def log_event(conn, job_id, kind, detail=None):
    conn.execute("INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)", (job_id, now(), kind, detail))
