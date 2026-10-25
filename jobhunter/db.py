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
    posted        TEXT,                   -- as given by the feed
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


def connect(path=DB_PATH):
    path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log_event(conn, job_id, kind, detail=None):
    conn.execute("INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)", (job_id, now(), kind, detail))
