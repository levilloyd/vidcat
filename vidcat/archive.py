"""Archive folders: kept as they are (raw footage, collections that repeat videos found elsewhere), so copies in
them are hidden in the web viewer and never offered for removal by `vidcat dupes`."""
import os
import sqlite3
from collections import defaultdict

from . import hashing


def _normalize(path) -> str:
    return os.path.realpath(os.path.expanduser(str(path))).rstrip("/")


def folders(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT path FROM archive_folders ORDER BY path")]


def add(conn: sqlite3.Connection, path) -> str:
    p = _normalize(path)
    if not os.path.isdir(p):
        raise ValueError(f"{p} is not a folder")
    with conn:
        conn.execute("INSERT OR IGNORE INTO archive_folders (path) VALUES (?)", (p,))
    return p


def remove(conn: sqlite3.Connection, path) -> bool:
    """Stop treating a folder as an archive. Works even if the folder no longer exists."""
    p = str(path).rstrip("/")
    with conn:
        cur = conn.execute("DELETE FROM archive_folders WHERE path IN (?, ?)", (p, _normalize(path)))
    return cur.rowcount > 0


def contains(archive: list[str], path: str) -> bool:
    return any(path.startswith(f + "/") for f in archive)


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def in_archive_sql(archive: list[str], alias: str = "v") -> str:
    """SQL that is true when `alias`'s path is inside an archive folder. Paths are inlined as quoted literals so
    the expression can sit anywhere in a query (a SELECT list, a subquery) without parameter bookkeeping."""
    if not archive:
        return "0"
    return "(" + " OR ".join(
        f"substr({alias}.path, 1, {len(f) + 1}) = {_quote(f + '/')}" for f in archive
    ) + ")"


# `c` is a copy of `v`: the same file (fingerprints match), or the same clip saved as a different file (a
# re-export or conversion: same capture time from the camera and the same length).
COPY_OF = (
    "c.id != v.id AND c.missing = 0 AND ("
    "(c.size = v.size AND v.size > 0 AND ((c.sha256 IS NOT NULL AND c.sha256 = v.sha256) "
    "OR (c.partial_hash IS NOT NULL AND c.partial_hash = v.partial_hash))) "
    "OR (c.date_source = 'metadata' AND v.date_source = 'metadata' "
    "AND c.created_at BETWEEN v.created_at - 2 AND v.created_at + 2 AND abs(c.duration - v.duration) <= 0.1))"
)


def hidden_copy_sql(archive: list[str]) -> str:
    """SQL that is true for a video `v` that "Hide copies" hides: it's in an archive folder and has a copy that
    is shown instead, one outside the archives or, among archive copies only, the one added first."""
    if not archive:
        return "0"
    return (f"({in_archive_sql(archive, 'v')} AND EXISTS (SELECT 1 FROM videos c WHERE {COPY_OF} "
            f"AND (NOT {in_archive_sql(archive, 'c')} OR c.id < v.id)))")


def copies(conn: sqlite3.Connection, video_id: int) -> list[dict]:
    """Other cataloged copies of a video, for the detail view's "Also in"."""
    archive = folders(conn)
    rows = conn.execute(
        f"SELECT c.path, c.dir, (c.size = v.size AND ((c.sha256 IS NOT NULL AND c.sha256 = v.sha256) "
        f"OR (c.partial_hash IS NOT NULL AND c.partial_hash = v.partial_hash))) AS identical "
        f"FROM videos v JOIN videos c ON {COPY_OF} WHERE v.id = ? ORDER BY c.path",
        (video_id,),
    )
    return [{"path": r["path"], "dir": r["dir"], "identical": bool(r["identical"]),
             "archived": contains(archive, r["path"])} for r in rows]


def fingerprint(conn: sqlite3.Connection, on_progress=None) -> int:
    """Fingerprint (partial hash) archive videos that share a size with another video, and that other video, so
    identical copies can be recognized. Cheap: three 1 MiB reads per file. Returns how many were fingerprinted."""
    archive = folders(conn)
    if not archive:
        return 0
    by_size = defaultdict(list)
    for r in conn.execute("SELECT id, path, size, partial_hash FROM videos WHERE missing = 0 AND size > 0"):
        by_size[r["size"]].append(r)
    need = [r for group in by_size.values() if len(group) > 1 and any(contains(archive, g["path"]) for g in group)
            for r in group if r["partial_hash"] is None]
    done = 0
    for i, r in enumerate(need, 1):
        if on_progress:
            on_progress("Fingerprinting copies", i, len(need))
        try:
            h = hashing.partial_hash(r["path"], r["size"])
        except OSError:
            continue
        conn.execute("UPDATE videos SET partial_hash = ? WHERE id = ?", (h, r["id"]))
        done += 1
    conn.commit()
    return done
