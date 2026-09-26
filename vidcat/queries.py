"""Catalog search shared by the web API and the CLI."""
import sqlite3
import time
from collections.abc import Sequence
from datetime import date, timedelta

from . import config
from .tags import tags_for

SORTS = {
    "name": "v.name COLLATE NOCASE",
    "date": "v.created_at",
    "size": "v.size",
    "duration": "v.duration",
    "added": "v.added_at",
    "path": "v.path COLLATE NOCASE",
}

# Other visible files that could be a copy of this one: same size, and not proven different by hash.
_DUP_COUNT = (
    "(SELECT COUNT(*) FROM videos d WHERE d.size = v.size AND d.id != v.id AND d.missing = 0 "
    "AND v.size > 0 AND (v.sha256 IS NULL OR d.sha256 IS NULL OR d.sha256 = v.sha256))"
)


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _local_epoch(d: str, end_of_day: bool = False) -> int:
    day = date.fromisoformat(d) + (timedelta(days=1) if end_of_day else timedelta())
    return int(time.mktime(day.timetuple()))


def item_from_row(row: sqlite3.Row, tags: list[str]) -> dict:
    d = {k: row[k] for k in (
        "id", "path", "dir", "name", "ext", "size", "created_at", "date_source", "duration", "width", "height",
        "codec", "name_score", "caption", "rotation",
    )}
    # Changes whenever the file behind this id does: new content, or a new file given a deleted row's id
    # (SQLite reuses the highest id). The UI puts it in media/thumbnail URLs so browsers never show a cached
    # copy of a different video.
    d["rev"] = f"{row['added_at']}-{row['size']}-{int(row['mtime'])}"
    d["bad_name"] = row["name_score"] < config.BAD_NAME_THRESHOLD
    d["dup_count"] = row["dup_count"]
    d["tags"] = tags
    return d


def get_video(conn: sqlite3.Connection, video_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT v.*, {_DUP_COUNT} AS dup_count FROM videos v WHERE v.id = ? AND v.missing = 0", (video_id,)
    ).fetchone()
    return item_from_row(row, tags_for(conn, [video_id])[video_id]) if row else None


def _filter_clause(
    *,
    q: str = "",
    tags: Sequence[str] = (),
    exts: Sequence[str] = (),
    folder: str | None = None,
    min_dur: float | None = None,
    max_dur: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    bad_name: bool = False,
    duplicates: bool = False,
) -> tuple[str, list]:
    """SQL WHERE clause (over `videos v`) and its parameters for the search filters."""
    where, params = ["v.missing = 0"], []

    for term in q.split():
        like = _like(term)
        where.append(
            "(v.name LIKE ? ESCAPE '\\' OR v.path LIKE ? ESCAPE '\\' OR v.caption LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM video_tags vt JOIN tags t ON t.id = vt.tag_id "
            "WHERE vt.video_id = v.id AND t.name LIKE ? ESCAPE '\\'))"
        )
        params += [like, like, like, like]
    for tag in tags:
        where.append(
            "EXISTS (SELECT 1 FROM video_tags vt JOIN tags t ON t.id = vt.tag_id "
            "WHERE vt.video_id = v.id AND t.name = ?)"
        )
        params.append(tag)
    if exts:
        where.append(f"v.ext IN ({','.join('?' * len(exts))})")
        params += [e.lower().lstrip(".") for e in exts]
    if folder:
        where.append("(v.dir = ? OR substr(v.path, 1, ?) = ?)")
        prefix = folder.rstrip("/") + "/"
        params += [folder.rstrip("/"), len(prefix), prefix]
    if min_dur is not None:
        where.append("v.duration >= ?")
        params.append(min_dur)
    if max_dur is not None:
        where.append("v.duration <= ?")
        params.append(max_dur)
    if date_from:
        where.append("v.created_at >= ?")
        params.append(_local_epoch(date_from))
    if date_to:
        where.append("v.created_at < ?")
        params.append(_local_epoch(date_to, end_of_day=True))
    if bad_name:
        where.append("v.name_score < ?")
        params.append(config.BAD_NAME_THRESHOLD)
    if duplicates:
        where.append(f"{_DUP_COUNT} > 0")
    return " AND ".join(where), params


def _order_by(sort: str, order: str) -> str:
    direction = "ASC" if order.lower() == "asc" else "DESC"
    return f"ORDER BY {SORTS.get(sort, SORTS['date'])} {direction}, v.id {direction}"


def search_videos(
    conn: sqlite3.Connection,
    *,
    sort: str = "date",
    order: str = "desc",
    page: int = 1,
    page_size: int = 60,
    **filters,
) -> dict:
    """One page of the videos matching `filters` (the keyword arguments of `_filter_clause`)."""
    clause, params = _filter_clause(**filters)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))

    total = conn.execute(f"SELECT COUNT(*) FROM videos v WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT v.*, {_DUP_COUNT} AS dup_count FROM videos v WHERE {clause} {_order_by(sort, order)} "
        "LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    tag_map = tags_for(conn, [r["id"] for r in rows])
    return {
        "items": [item_from_row(r, tag_map[r["id"]]) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def all_matches(conn: sqlite3.Connection, *, sort: str = "date", order: str = "desc", **filters) -> list[sqlite3.Row]:
    """Every video matching `filters`, in display order (not just one page), for bulk actions."""
    clause, params = _filter_clause(**filters)
    return conn.execute(f"SELECT v.* FROM videos v WHERE {clause} {_order_by(sort, order)}", params).fetchall()


def list_folders(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT dir AS path, COUNT(*) AS count FROM videos WHERE missing = 0 GROUP BY dir ORDER BY dir")
    return [dict(r) for r in rows]


def list_exts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT ext, COUNT(*) AS count FROM videos WHERE missing = 0 GROUP BY ext ORDER BY count DESC")
    return [dict(r) for r in rows]
