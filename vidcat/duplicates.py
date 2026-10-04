"""Exact duplicate detection: size -> partial hash -> full SHA-256, with hashes cached in the catalog."""
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable

from . import archive, config, disposal, hashing
from .tags import merge_tags

Progress = Callable[[str, int, int], None]


def find_duplicate_groups(conn: sqlite3.Connection, on_progress: Progress | None = None) -> list[list[sqlite3.Row]]:
    """Return groups (len >= 2) of byte-identical files. Largest reclaimable space first.

    Files in archive folders are left out entirely, so they're never offered for removal.
    """
    report = on_progress or (lambda *_: None)

    by_size = defaultdict(list)
    in_archive = archive.in_archive_sql(archive.folders(conn), "videos")
    for r in conn.execute(f"SELECT * FROM videos WHERE missing = 0 AND size > 0 AND NOT {in_archive}"):
        by_size[r["size"]].append(r)
    candidates = [g for g in by_size.values() if len(g) > 1]

    # Stage 1: partial hash for files that share a size.
    need = [r for g in candidates for r in g if r["partial_hash"] is None]
    for i, r in enumerate(need, 1):
        report("Fingerprinting", i, len(need))
        try:
            h = hashing.partial_hash(r["path"], r["size"])
        except OSError:
            continue
        conn.execute("UPDATE videos SET partial_hash = ? WHERE id = ?", (h, r["id"]))
    conn.commit()

    by_partial = defaultdict(list)
    for g in candidates:
        for r in conn.execute(
            f"SELECT * FROM videos WHERE id IN ({','.join('?' * len(g))})", [r['id'] for r in g]
        ):
            if r["partial_hash"]:
                by_partial[(r["size"], r["partial_hash"])].append(r)
    candidates = [g for g in by_partial.values() if len(g) > 1]

    # Stage 2: full hash to confirm.
    need = [r for g in candidates for r in g if r["sha256"] is None]
    for i, r in enumerate(need, 1):
        report("Verifying", i, len(need))
        try:
            h = hashing.full_sha256(r["path"])
        except OSError:
            continue
        conn.execute("UPDATE videos SET sha256 = ? WHERE id = ?", (h, r["id"]))
    conn.commit()

    by_hash = defaultdict(list)
    for g in candidates:
        for r in conn.execute(
            f"SELECT * FROM videos WHERE id IN ({','.join('?' * len(g))})", [r['id'] for r in g]
        ):
            if r["sha256"]:
                by_hash[r["sha256"]].append(r)

    groups = []
    for g in by_hash.values():
        g = _drop_hardlinks(g)
        if len(g) > 1:
            groups.append(sorted(g, key=lambda r: r["path"]))
    groups.sort(key=lambda g: g[0]["size"] * (len(g) - 1), reverse=True)
    return groups


def _drop_hardlinks(group: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Two paths to the same inode aren't wasting space; trashing one gains nothing."""
    seen, out = set(), []
    for r in group:
        try:
            st = os.stat(r["path"])
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def pick_default_keep(group: list[sqlite3.Row]) -> int:
    """Index of the copy we'd keep by default: best name, then earliest capture date, then shortest path."""
    def key(item):
        _, r = item
        return (-r["name_score"], r["created_at"], len(r["path"]), r["path"])
    return min(enumerate(group), key=key)[0]


def remove_copies(
    conn: sqlite3.Connection, keep_id: int, remove_ids: list[int], mode: str = "trash"
) -> list[disposal.Disposal]:
    """Remove the given copies and fold their tags/caption into the kept file.

    `mode` is passed to `disposal.dispose`: "trash" (falling back to a holding folder on volumes that have no
    Trash), "archive", or "delete". Refuses to do anything unless the kept file still exists. A copy that
    can't be removed raises and is left in the catalog. Returns what happened to each removed file.
    """
    keep = conn.execute("SELECT * FROM videos WHERE id = ?", (keep_id,)).fetchone()
    if keep is None or not os.path.exists(keep["path"]):
        raise FileNotFoundError("The copy to keep is missing; nothing was removed.")
    removed = []
    for rid in remove_ids:
        if rid == keep_id:
            continue
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (rid,)).fetchone()
        if row is None:
            continue
        if os.path.exists(row["path"]):
            removed.append(disposal.dispose(row["path"], mode, config.DUPLICATES_DIR))
        merge_tags(conn, rid, keep_id)
        with conn:
            if row["caption"] and not keep["caption"]:
                conn.execute("UPDATE videos SET caption = ? WHERE id = ?", (row["caption"], keep_id))
            if row["rotation"] and not keep["rotation"]:  # identical bytes, so the same turn is needed
                conn.execute("UPDATE videos SET rotation = ? WHERE id = ?", (row["rotation"], keep_id))
            conn.execute("DELETE FROM videos WHERE id = ?", (rid,))
    return removed
