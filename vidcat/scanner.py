"""Walk directories and keep the `videos` table in sync with what's on disk."""
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import config, media
from .names import name_quality

Progress = Callable[[str, int, int], None]  # (stage, done, total)


@dataclass
class ScanStats:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    moved: int = 0
    skipped: int = 0  # not actually video files
    thumbnails: int = 0


def iter_video_files(roots: Iterable[Path]):
    """Yield (path, size, mtime) for every video-looking file under the roots."""
    seen = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in config.SKIP_DIRS]
            for fn in filenames:
                if fn.startswith("."):  # includes macOS AppleDouble "._" files
                    continue
                if os.path.splitext(fn)[1].lower() not in config.VIDEO_EXTS:
                    continue
                p = os.path.join(dirpath, fn)
                if p in seen:
                    continue
                seen.add(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                yield p, st.st_size, st.st_mtime


def thumb_path(thumbs: Path, video_id: int) -> Path:
    return thumbs / f"{video_id}.jpg"


def _volume_offline(path: str) -> bool:
    """True if the file lives on an external volume that isn't currently mounted."""
    parts = Path(path).parts
    return len(parts) > 2 and parts[1] == "Volumes" and not os.path.isdir(os.path.join("/", "Volumes", parts[2]))


def scan(
    conn: sqlite3.Connection,
    roots: list[Path],
    thumbs: Path,
    workers: int = 4,
    make_thumbs: bool = True,
    on_progress: Progress | None = None,
) -> ScanStats:
    """Sync the catalog with everything under `roots` (also marks vanished files missing, detects moves)."""
    media.require_tools()
    (on_progress or (lambda *_: None))("Finding files", 0, 0)
    found = list(iter_video_files(r.resolve() for r in roots))
    return _sync(conn, found, thumbs, workers, make_thumbs, on_progress, full=True)


def scan_files(
    conn: sqlite3.Connection,
    files: list[Path],
    thumbs: Path,
    workers: int = 4,
    make_thumbs: bool = True,
    on_progress: Progress | None = None,
) -> ScanStats:
    """Add or refresh just these files; the rest of the catalog is left untouched."""
    media.require_tools()
    found, seen = [], set()
    for f in files:
        p = f.resolve()
        if str(p) in seen:
            continue
        seen.add(str(p))
        st = p.stat()
        found.append((str(p), st.st_size, st.st_mtime))
    return _sync(conn, found, thumbs, workers, make_thumbs, on_progress, full=False)


def _sync(
    conn: sqlite3.Connection,
    found: list[tuple[str, int, float]],
    thumbs: Path,
    workers: int,
    make_thumbs: bool,
    on_progress: Progress | None,
    full: bool,
) -> ScanStats:
    report = on_progress or (lambda *_: None)
    stats = ScanStats()
    now = int(time.time())
    existing = {r["path"]: r for r in conn.execute("SELECT * FROM videos")}
    found_paths = {path for path, _, _ in found}
    by_folded: dict[str, list[sqlite3.Row]] = {}
    for p, r in existing.items():
        if p not in found_paths:
            by_folded.setdefault(p.casefold(), []).append(r)

    todo, seen_paths = [], set()
    for path, size, mtime in found:
        seen_paths.add(path)
        row = existing.get(path)
        if row is None:
            row = _renamed_by_case(path, by_folded.get(path.casefold(), []))
            if row is not None:
                by_folded[path.casefold()].remove(row)
        if (row and row["path"] == path and row["size"] == size and abs(row["mtime"] - mtime) < 1e-6
                and row["date_source"] is not None and row["location_read"]):
            stats.unchanged += 1
            if row["missing"]:
                conn.execute("UPDATE videos SET missing = 0 WHERE id = ?", (row["id"],))
            conn.execute("UPDATE videos SET scanned_at = ? WHERE id = ?", (now, row["id"]))
            # Re-score in case the scoring rules changed since this file was cataloged. 100 is left alone,
            # since that's what "keep name forever" sets.
            score = name_quality(os.path.splitext(row["name"])[0])
            if row["name_score"] < 100 and score != row["name_score"]:
                conn.execute("UPDATE videos SET name_score = ? WHERE id = ?", (score, row["id"]))
        else:
            todo.append((path, size, mtime, row))
    conn.commit()

    def read(job):
        data = media.probe(job[0])
        return data, media.gopro_location(job[0], data)

    # Probe new/changed files in parallel; write to the DB from this thread only.
    new_rows, thumb_jobs = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(read, todo)
        for i, ((path, size, mtime, row), (data, gopro)) in enumerate(zip(todo, results), 1):
            report("Reading metadata", i, len(todo))
            name = os.path.basename(path)
            info = media.parse_probe(data, name, mtime)
            if info["latitude"] is None and gopro:
                info["latitude"], info["longitude"] = gopro
            if info["has_video"] is False:  # ffprobe worked and found no video stream
                stats.skipped += 1
                continue
            values = dict(
                path=path, dir=os.path.dirname(path), name=name,
                ext=os.path.splitext(name)[1].lower().lstrip("."), size=size, mtime=mtime,
                created_at=info["created_at"], date_source=info["date_source"], duration=info["duration"],
                width=info["width"], height=info["height"], codec=info["codec"],
                latitude=info["latitude"], longitude=info["longitude"], name_score=name_quality(os.path.splitext(name)[0]), scanned_at=now,
            )
            if row:
                # A row is re-read either because the file changed or because an older catalog lacked
                # some field. Only a real content change invalidates hashes and thumbnails.
                content_changed = row["size"] != size or abs(row["mtime"] - mtime) >= 1e-6
                reset = ", partial_hash=NULL, sha256=NULL" if content_changed else ""
                # name_score is deliberately left alone unless the name changed, so "keep name forever" sticks.
                # A change of letter case alone doesn't make a name more or less useful.
                conn.execute(
                    "UPDATE videos SET path=:path, dir=:dir, name=:name, ext=:ext, size=:size, mtime=:mtime, "
                    "created_at=:created_at, date_source=:date_source, duration=:duration, width=:width, "
                    "height=:height, codec=:codec, latitude=:latitude, longitude=:longitude, location_read=1, missing=0, "
                    "scanned_at=:scanned_at"
                    + (", name_score=:name_score" if content_changed or row["name"].casefold() != name.casefold() else "")
                    + reset + " WHERE id=:id",
                    {**values, "id": row["id"]},
                )
                video_id = row["id"]
                stats.updated += 1
                if content_changed:
                    thumb_jobs.append((video_id, path, info["duration"]))
            else:
                cur = conn.execute(
                    "INSERT INTO videos (path, dir, name, ext, size, mtime, created_at, date_source, duration, "
                    "width, height, codec, latitude, longitude, location_read, name_score, added_at, scanned_at) "
                    "VALUES (:path, :dir, :name, :ext, :size, :mtime, :created_at, :date_source, :duration, :width, "
                    ":height, :codec, :latitude, :longitude, 1, :name_score, :scanned_at, :scanned_at)",
                    values,
                )
                video_id = cur.lastrowid
                new_rows.append({**values, "id": video_id})
                stats.added += 1
                thumb_jobs.append((video_id, path, info["duration"]))
    conn.commit()

    # Anything in the catalog we didn't see this time: mark missing (unless its drive is just unmounted).
    for path, row in (existing.items() if full else ()):
        if path not in seen_paths and not row["missing"] and not os.path.exists(path):
            if not _volume_offline(path):
                conn.execute("UPDATE videos SET missing = 1 WHERE id = ?", (row["id"],))
                stats.missing += 1
    conn.commit()

    stats.moved = _reconcile_moves(conn, new_rows) if full else 0

    if make_thumbs and thumb_jobs:
        def make(job):
            vid, path, duration = job
            return media.make_thumbnail(path, thumb_path(thumbs, vid), duration)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, ok in enumerate(pool.map(make, thumb_jobs), 1):
                report("Making thumbnails", i, len(thumb_jobs))
                stats.thumbnails += bool(ok)
    return stats


def _renamed_by_case(path: str, candidates: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The catalog row for `path` under its old spelling, if the file (or a folder above it) was renamed
    only in letter case.

    On a case-insensitive volume (the macOS default, and most NAS shares) the old spelling still "exists",
    so without this the file would get a second row and the old one would never be marked missing.
    `candidates` are rows whose path matches `path` ignoring case. One only counts when it is the same file
    and its old spelling is no longer listed on disk; on a case-sensitive volume `clip.mp4` and `Clip.mp4`
    can be two different files, or hard links to one.
    """
    new_parts = Path(path).parts
    for row in candidates:
        old_parts = Path(row["path"]).parts
        if len(old_parts) != len(new_parts):
            continue
        try:
            still_listed = any(
                old != new and old in os.listdir(os.path.join(*old_parts[:i]))
                for i, (old, new) in enumerate(zip(old_parts, new_parts)) if i
            )
            if not still_listed and os.path.samefile(row["path"], path):
                return row
        except OSError:
            continue
    return None


def _reconcile_moves(conn: sqlite3.Connection, new_rows: list[dict]) -> int:
    """A newly seen file that matches a missing entry's size+mtime is that file, moved: carry over its data."""
    if not new_rows:
        return 0
    missing = [dict(r) for r in conn.execute("SELECT * FROM videos WHERE missing = 1")]
    moved = 0
    for new in new_rows:
        cands = [m for m in missing if m["size"] == new["size"] and abs(m["mtime"] - new["mtime"]) < 1.0]
        if len(cands) > 1:
            cands = [m for m in cands if m["name"] == new["name"]]
        if len(cands) != 1:
            continue
        old = cands[0]
        missing.remove(old)
        with conn:
            conn.execute("UPDATE OR IGNORE video_tags SET video_id = ? WHERE video_id = ?", (new["id"], old["id"]))
            conn.execute("UPDATE rename_history SET video_id = ? WHERE video_id = ?", (new["id"], old["id"]))
            conn.execute(
                "UPDATE videos SET caption = ?, rotation = ?, partial_hash = ?, sha256 = ?, added_at = ? WHERE id = ?",
                (old["caption"], old["rotation"], old["partial_hash"], old["sha256"], old["added_at"], new["id"]),
            )
            conn.execute("DELETE FROM videos WHERE id = ?", (old["id"],))
        moved += 1
    return moved
