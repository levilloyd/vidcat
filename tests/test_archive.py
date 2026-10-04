import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from vidcat import archive, cli, duplicates, scanner
from vidcat.db import connect
from vidcat.web.app import create_app


@pytest.fixture
def lib(tmp_path, clip_dir):
    """Curated year folder plus an archive folder (named with an apostrophe, to exercise SQL quoting) holding an
    identical copy, a re-encoded copy of the same clip, a video found only there, and two copies of each other."""
    year, arch = tmp_path / "Videos" / "2019", tmp_path / "Videos" / "Grandma's Reunions"
    year.mkdir(parents=True)
    arch.mkdir()
    shutil.copy(clip_dir / "a.mp4", year / "Beach Trip.mp4")             # a.mp4 has an embedded capture date
    shutil.copy(clip_dir / "a.mp4", arch / "beach copy.mp4")             # identical
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip_dir / "a.mp4"), "-map_metadata", "0",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", str(arch / "beach export.mp4")], check=True)
    shutil.copy(clip_dir / "b.mp4", arch / "only here.mp4")
    (tmp_path / "Videos" / "Grandma's Reunions2").mkdir()                # a sibling whose name starts the same
    shutil.copy(clip_dir / "b.mp4", tmp_path / "Videos" / "Grandma's Reunions2" / "not archived.mp4")
    db = tmp_path / "catalog.db"
    conn = connect(db)
    scanner.scan(conn, [tmp_path / "Videos"], tmp_path / "thumbs")
    archive.add(conn, arch)
    archive.fingerprint(conn)
    return db, conn, year, arch


def names(client, **params):
    return sorted(v["name"] for v in client.get("/api/videos", params=params).json()["items"])


def test_hide_copies_shows_each_video_once(lib):
    db, conn, *_ = lib
    with TestClient(create_app(db)) as client:
        everything = names(client)
        assert len(everything) == 5
        shown = names(client, hide_copies=True)
        # the identical copy and the re-encoded export are hidden; "only here" and the curated video stay. The two
        # copies of b.mp4 (archive and non-archive sibling folder) are a non-archive original plus an archive copy.
        assert shown == ["Beach Trip.mp4", "not archived.mp4"]
        assert client.get("/api/videos", params={"hide_copies": True}).json()["total"] == 2


def test_copies_within_archives_show_once(lib, clip_dir):
    db, conn, year, arch = lib
    shutil.copy(clip_dir / "b.mp4", arch / "only here again.mp4")
    (arch.parent / "Grandma's Reunions2" / "not archived.mp4").unlink()
    scanner.scan(conn, [arch.parent], arch.parent.parent / "thumbs")
    archive.fingerprint(conn)
    with TestClient(create_app(db)) as client:
        shown = names(client, hide_copies=True)
        assert shown.count("only here.mp4") + shown.count("only here again.mp4") == 1   # the one added first


def test_archive_copies_are_not_flagged_or_offered_for_removal(lib):
    db, conn, year, arch = lib
    with TestClient(create_app(db)) as client:
        items = {v["name"]: v for v in client.get("/api/videos").json()["items"]}
        assert items["Beach Trip.mp4"]["dup_count"] == 0        # its only copy is in an archive
        assert items["beach copy.mp4"]["dup_count"] == 0
        assert client.get("/api/videos", params={"duplicates": True}).json()["total"] == 0
    assert duplicates.find_duplicate_groups(conn) == []
    archive.remove(conn, arch)
    groups = duplicates.find_duplicate_groups(conn)
    assert [sorted(r["name"] for r in g) for g in groups] == [["Beach Trip.mp4", "beach copy.mp4"], ["not archived.mp4", "only here.mp4"]]


def test_also_in_lists_copies(lib):
    db, conn, year, arch = lib
    with TestClient(create_app(db)) as client:
        vid = next(v["id"] for v in client.get("/api/videos").json()["items"] if v["name"] == "Beach Trip.mp4")
        copies = {c["path"].rsplit("/", 1)[1]: c for c in client.get(f"/api/videos/{vid}/copies").json()}
        assert set(copies) == {"beach copy.mp4", "beach export.mp4"}
        assert copies["beach copy.mp4"]["identical"] and copies["beach copy.mp4"]["archived"]
        assert not copies["beach export.mp4"]["identical"]


def test_archive_commands(lib, tmp_path):
    db, conn, year, arch = lib
    run = lambda *a: CliRunner().invoke(cli.app, ["--db", str(db), "archive", *a])
    r = run("list")
    assert "Grandma's Reunions" in r.output and "(3 cataloged)" in r.output
    assert "is not a folder" in run("add", str(tmp_path / "nope")).output
    assert "No longer an archive" in run("remove", str(arch)).output
    assert "No archive folders" in run("list").output
    assert "Not an archive folder" in run("remove", str(arch)).output


def test_in_archive_sql_matches_only_inside_the_folder(conn):
    for p in ("/v/Grandma's/a.mp4", "/v/Grandma's2/b.mp4", "/v/Grandma's"):
        conn.execute("INSERT INTO videos (path, dir, name, ext, size, mtime, created_at, added_at, scanned_at) "
                     "VALUES (?, '/v', 'x', 'mp4', 1, 1, 1, 1, 1)", (p,))
    rows = conn.execute(f"SELECT path FROM videos v WHERE {archive.in_archive_sql(['/v/Grandma' + chr(39) + 's'])}").fetchall()
    assert [r[0] for r in rows] == ["/v/Grandma's/a.mp4"]
    assert archive.in_archive_sql([]) == "0"
