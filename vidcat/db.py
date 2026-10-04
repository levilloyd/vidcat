import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    dir          TEXT NOT NULL,
    name         TEXT NOT NULL,            -- file name including extension
    ext          TEXT NOT NULL,            -- lowercase, no dot
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    created_at   INTEGER NOT NULL,         -- epoch seconds (UTC): metadata date, else filename date, else mtime
    date_source  TEXT,                     -- 'metadata' | 'filename' | 'mtime'; NULL = not yet determined
    duration    REAL,
    width        INTEGER,
    height       INTEGER,
    codec        TEXT,
    partial_hash TEXT,
    sha256       TEXT,
    name_score   INTEGER NOT NULL DEFAULT 100,
    caption      TEXT,
    rotation     INTEGER NOT NULL DEFAULT 0,  -- extra clockwise turn applied when viewing: 0, 90, 180 or 270
    latitude     REAL,                     -- where it was filmed (GPS from the file's metadata), if recorded
    longitude    REAL,
    location_read INTEGER NOT NULL DEFAULT 0,  -- 1 once latitude/longitude have been looked for
    missing      INTEGER NOT NULL DEFAULT 0,
    added_at     INTEGER NOT NULL,
    scanned_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_size ON videos(size);
CREATE INDEX IF NOT EXISTS idx_videos_sha256 ON videos(sha256);
CREATE INDEX IF NOT EXISTS idx_videos_dir ON videos(dir);
CREATE INDEX IF NOT EXISTS idx_videos_created ON videos(created_at);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags(tag_id);

-- Place names looked up for GPS coordinates (see places.py), keyed by lat/lon * 1000 rounded; '' = nothing there.
CREATE TABLE IF NOT EXISTS places (
    lat_key INTEGER NOT NULL,
    lon_key INTEGER NOT NULL,
    name    TEXT NOT NULL,
    PRIMARY KEY (lat_key, lon_key)
);

-- Folders kept as they are whose copies of videos found elsewhere are hidden and never removed (see archive.py).
CREATE TABLE IF NOT EXISTS archive_folders (
    path TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS rename_history (
    id       INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL,
    old_path TEXT NOT NULL,
    new_path TEXT NOT NULL,
    at       INTEGER NOT NULL
);
"""


def connect(path: Path | str | None = None, init: bool = True, cross_thread: bool = False) -> sqlite3.Connection:
    """Open the catalog.

    `init=False` skips schema creation (for per-request web connections). `cross_thread=True` lets a
    connection be used from a different thread than the one that opened it; FastAPI opens a request's
    connection in one worker thread and may run the endpoint in another. Each such connection is only
    ever used by one request at a time, so this is safe.
    """
    from . import config

    path = Path(path) if path else config.db_path()
    if init:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=not cross_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring catalogs created by older versions up to date."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "date_source" not in cols:
        # Left NULL for existing rows; the next `vidcat scan` re-reads them to fill it in.
        conn.execute("ALTER TABLE videos ADD COLUMN date_source TEXT")
    if "rotation" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN rotation INTEGER NOT NULL DEFAULT 0")
    if "location_read" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN latitude REAL")
        conn.execute("ALTER TABLE videos ADD COLUMN longitude REAL")
        conn.execute("ALTER TABLE videos ADD COLUMN location_read INTEGER NOT NULL DEFAULT 0")
        # Only MP4/QuickTime-family files can hold a location; the next `vidcat scan` re-reads just those.
        conn.execute("UPDATE videos SET location_read = 1 WHERE ext NOT IN ('mov', 'mp4', 'm4v', '3gp')")
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        # GoPro telemetry GPS came after the first location release: look again at files that found none.
        conn.execute("UPDATE videos SET location_read = 0 WHERE latitude IS NULL AND ext IN ('mov', 'mp4')")
        conn.execute("PRAGMA user_version = 1")
    conn.commit()
