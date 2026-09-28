"""Scan -> dupes -> rename -> tags, against real ffmpeg-generated clips."""
import os
from pathlib import Path

import pytest

from vidcat import disposal, duplicates, hashing, names, queries, scanner, tags


def do_scan(conn, lib, thumbs, **kw):
    return scanner.scan(conn, [lib], thumbs, **kw)


def test_scan_reads_metadata_and_is_incremental(conn, library, thumbs):
    s = do_scan(conn, library, thumbs)
    assert (s.added, s.unchanged) == (3, 0)
    row = conn.execute("SELECT * FROM videos WHERE name = 'IMG_0001.mp4'").fetchone()
    assert row["width"] == 160 and row["height"] == 120
    assert 0.5 < row["duration"] < 1.5
    assert row["created_at"] == 1562275930  # 2019-07-04T21:32:10Z from container metadata
    assert row["name_score"] < 50
    assert (thumbs / f"{row['id']}.jpg").stat().st_size > 0

    s2 = do_scan(conn, library, thumbs)
    assert (s2.added, s2.updated, s2.unchanged) == (0, 0, 3)


def test_scan_marks_missing_and_detects_moves(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    target = conn.execute("SELECT id FROM videos WHERE name = 'MVI_0002.mp4'").fetchone()["id"]
    tags.add_tags(conn, target, ["family"])

    moved_to = library / "Elsewhere"
    moved_to.mkdir()
    os.rename(library / "Summer Trip" / "sub" / "MVI_0002.mp4", moved_to / "MVI_0002.mp4")
    s = do_scan(conn, library, thumbs)
    assert s.moved == 1
    row = conn.execute("SELECT * FROM videos WHERE name = 'MVI_0002.mp4'").fetchone()
    assert row["path"].endswith("Elsewhere/MVI_0002.mp4") and row["missing"] == 0
    assert tags.tags_for(conn, [row["id"]])[row["id"]] == ["family"]  # tags followed the file

    (moved_to / "MVI_0002.mp4").unlink()
    assert do_scan(conn, library, thumbs).missing == 1
    assert queries.search_videos(conn)["total"] == 2


def case_insensitive(folder: Path) -> bool:
    probe = folder / "CaseProbe"
    probe.touch()
    try:
        return (folder / "caseprobe").exists()
    finally:
        probe.unlink()


def test_case_only_rename_keeps_one_row(conn, library, thumbs):
    if not case_insensitive(library):
        pytest.skip("needs a case-insensitive volume (the macOS default, like most NAS shares)")
    do_scan(conn, library, thumbs)
    row = conn.execute("SELECT * FROM videos WHERE name = 'MVI_0002.mp4'").fetchone()
    tags.add_tags(conn, row["id"], ["family"])

    sub = library / "Summer Trip" / "sub"
    os.rename(sub / "MVI_0002.mp4", sub / "mvi_0002.mp4")   # file renamed, only the case changes
    os.rename(library / "Summer Trip", library / "summer trip")  # ...and a folder above it too
    s = do_scan(conn, library, thumbs)
    assert (s.added, s.updated, s.missing, s.moved) == (0, 3, 0, 0)  # the folder holds all 3 videos

    rows = conn.execute("SELECT * FROM videos WHERE name LIKE 'mvi_0002.mp4'").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == row["id"]
    assert rows[0]["path"].endswith("summer trip/sub/mvi_0002.mp4") and rows[0]["name"] == "mvi_0002.mp4"
    assert rows[0]["name_score"] == row["name_score"]
    assert tags.tags_for(conn, [row["id"]])[row["id"]] == ["family"]
    assert queries.search_videos(conn)["total"] == 3
    assert do_scan(conn, library, thumbs).unchanged == 3


def test_rotation_survives_rescan_change_and_move(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    row = conn.execute("SELECT * FROM videos WHERE name = 'MVI_0002.mp4'").fetchone()
    conn.execute("UPDATE videos SET rotation = 90 WHERE id = ?", (row["id"],))
    conn.commit()
    path = Path(row["path"])

    os.utime(path, (1_000_000_000, 1_000_000_000))  # looks like a changed file: re-read from scratch
    assert do_scan(conn, library, thumbs).updated == 1
    assert conn.execute("SELECT rotation FROM videos WHERE id = ?", (row["id"],)).fetchone()[0] == 90

    dest = library / "Elsewhere"
    dest.mkdir()
    os.rename(path, dest / path.name)                # moved: a new row is made, then reconciled with the old
    assert do_scan(conn, library, thumbs).moved == 1
    moved = conn.execute("SELECT * FROM videos WHERE name = 'MVI_0002.mp4'").fetchone()
    assert moved["path"].endswith("Elsewhere/MVI_0002.mp4") and moved["rotation"] == 90


def test_removing_a_duplicate_keeps_the_rotation(conn, library, thumbs, monkeypatch):
    do_scan(conn, library, thumbs)
    keep, drop = sorted(duplicates.find_duplicate_groups(conn)[0], key=lambda r: r["name"])  # Beach Trip, IMG_0001
    conn.execute("UPDATE videos SET rotation = 180 WHERE id = ?", (drop["id"],))
    conn.commit()
    monkeypatch.setattr(disposal, "send2trash", os.remove)
    duplicates.remove_copies(conn, keep["id"], [drop["id"]])
    assert conn.execute("SELECT rotation FROM videos WHERE id = ?", (keep["id"],)).fetchone()[0] == 180


def test_scan_ignores_hidden_and_other_files_but_keeps_corrupt_videos(conn, tmp_path, thumbs):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "._IMG_1.mp4").write_bytes(b"appledouble")  # macOS metadata sidecar
    (lib / "readme.txt").write_text("not a video")
    (lib / "broken.mp4").write_bytes(b"this is not really an mp4")  # unreadable, but the user should still see it
    s = do_scan(conn, lib, thumbs)
    assert s.added == 1
    row = conn.execute("SELECT * FROM videos").fetchone()
    assert row["name"] == "broken.mp4" and row["duration"] is None and row["created_at"] > 0


def test_hashing_distinguishes_same_size_files(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.write_bytes(b"x" * 5_000_000)
    b.write_bytes(b"x" * 5_000_000)
    c.write_bytes(b"x" * 2_500_000 + b"y" + b"x" * 2_499_999)  # differs only in the middle sample
    assert hashing.partial_hash(a) == hashing.partial_hash(b)
    assert hashing.full_sha256(a) == hashing.full_sha256(b)
    assert hashing.full_sha256(a) != hashing.full_sha256(c)


def test_find_duplicates_and_remove(conn, library, thumbs, monkeypatch):
    do_scan(conn, library, thumbs)
    groups = duplicates.find_duplicate_groups(conn)
    assert len(groups) == 1
    names_in_group = sorted(r["name"] for r in groups[0])
    assert names_in_group == ["Beach Trip.mp4", "IMG_0001.mp4"]

    keep_idx = duplicates.pick_default_keep(groups[0])
    assert groups[0][keep_idx]["name"] == "Beach Trip.mp4"  # the well-named copy wins

    keep, drop = groups[0][keep_idx], groups[0][1 - keep_idx]
    tags.add_tags(conn, drop["id"], ["birthday"])
    trashed_paths = []
    monkeypatch.setattr(disposal, "send2trash", lambda p: (trashed_paths.append(p), os.remove(p)))
    result = duplicates.remove_copies(conn, keep["id"], [drop["id"]])

    assert [d.path for d in result] == [drop["path"]] == trashed_paths and result[0].action == "trashed"
    assert Path(keep["path"]).exists() and not Path(drop["path"]).exists()
    assert tags.tags_for(conn, [keep["id"]])[keep["id"]] == ["birthday"]  # tags merged onto the survivor
    assert conn.execute("SELECT COUNT(*) FROM videos WHERE id = ?", (drop["id"],)).fetchone()[0] == 0
    assert duplicates.find_duplicate_groups(conn) == []


def test_remove_copies_refuses_when_kept_file_is_gone(conn, library, thumbs, monkeypatch):
    do_scan(conn, library, thumbs)
    group = duplicates.find_duplicate_groups(conn)[0]
    keep, drop = group
    os.remove(keep["path"])
    monkeypatch.setattr(disposal, "send2trash", lambda p: pytest.fail("must not trash anything"))
    with pytest.raises(FileNotFoundError):
        duplicates.remove_copies(conn, keep["id"], [drop["id"]])
    assert Path(drop["path"]).exists()


def test_hardlinks_are_not_duplicates(conn, library, thumbs):
    src = library / "Summer Trip" / "IMG_0001.mp4"
    os.link(src, library / "Summer Trip" / "hardlink.mp4")
    (library / "Summer Trip" / "Beach Trip.mp4").unlink()
    do_scan(conn, library, thumbs)
    assert duplicates.find_duplicate_groups(conn) == []


def test_rename_and_undo(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    vid = conn.execute("SELECT * FROM videos WHERE name = 'MVI_0002.mp4'").fetchone()
    new_path = names.rename_video(conn, vid["id"], "Sunset at the Pier.mp4")  # typed extension is tolerated
    assert Path(new_path).name == "Sunset at the Pier.mp4" and Path(new_path).exists()
    row = conn.execute("SELECT * FROM videos WHERE id = ?", (vid["id"],)).fetchone()
    assert row["name"] == "Sunset at the Pier.mp4" and row["name_score"] >= 50

    assert names.undo_last_rename(conn) is not None
    assert Path(vid["path"]).exists() and not Path(new_path).exists()
    assert names.undo_last_rename(conn) is None


def test_rename_never_overwrites(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    vid = conn.execute("SELECT * FROM videos WHERE name = 'IMG_0001.mp4'").fetchone()
    new_path = names.rename_video(conn, vid["id"], "Beach Trip")  # already exists in that folder
    assert Path(new_path).name == "Beach Trip (2).mp4"
    assert (Path(vid["dir"]) / "Beach Trip.mp4").exists()


def test_rename_rejects_empty_name(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    vid = conn.execute("SELECT id FROM videos LIMIT 1").fetchone()
    with pytest.raises(ValueError):
        names.rename_video(conn, vid["id"], " / : ")


def test_tags_are_case_insensitive_and_deduped(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    vid = conn.execute("SELECT id FROM videos LIMIT 1").fetchone()["id"]
    tags.add_tags(conn, vid, ["Family", "family", " FAMILY ", "Beach"])
    assert tags.tags_for(conn, [vid])[vid] == ["Beach", "Family"]
    tags.remove_tags(conn, vid, ["family"])
    assert tags.tags_for(conn, [vid])[vid] == ["Beach"]
    assert [t["name"] for t in tags.all_tags(conn)] == ["Beach"]  # orphaned tag cleaned up


def test_search_filters_and_sorts(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM videos")}
    tags.add_tags(conn, ids["Beach Trip.mp4"], ["vacation"])

    assert queries.search_videos(conn, q="beach")["total"] == 1
    assert queries.search_videos(conn, q="summer trip")["total"] == 3  # matches the folder in the path
    assert queries.search_videos(conn, q="vacation")["total"] == 1  # matches tag
    assert queries.search_videos(conn, tags=["Vacation"])["total"] == 1
    assert queries.search_videos(conn, q="100%")["total"] == 0  # wildcards are escaped
    assert queries.search_videos(conn, bad_name=True)["total"] == 2
    assert queries.search_videos(conn, duplicates=True)["total"] == 2
    assert queries.search_videos(conn, min_dur=1.5)["total"] == 1  # only the 2-second clip
    assert queries.search_videos(conn, date_from="2019-07-04", date_to="2019-07-04")["total"] == 2
    assert queries.search_videos(conn, exts=["mov"])["total"] == 0
    assert queries.search_videos(conn, folder=str(library))["total"] == 3
    by_name = [v["name"] for v in queries.search_videos(conn, sort="name", order="asc")["items"]]
    assert by_name == sorted(by_name, key=str.lower)
    assert len(queries.search_videos(conn, page_size=2)["items"]) == 2


def test_several_tags_match_all_or_any(conn, library, thumbs):
    do_scan(conn, library, thumbs)
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM videos")}
    tags.add_tags(conn, ids["Beach Trip.mp4"], ["2019", "isaac"])
    tags.add_tags(conn, ids["IMG_0001.mp4"], ["2019"])
    tags.add_tags(conn, ids["MVI_0002.mp4"], ["2020", "isaac"])

    def names(**kw):
        return sorted(v["name"] for v in queries.search_videos(conn, **kw)["items"])

    assert names(tags=["2019", "isaac"]) == ["Beach Trip.mp4"]  # default: every selected tag
    assert names(tags=["2019", "isaac"], tag_mode="all") == ["Beach Trip.mp4"]
    assert names(tags=["2019", "2020"], tag_mode="all") == []
    assert names(tags=["2019", "2020"], tag_mode="any") == ["Beach Trip.mp4", "IMG_0001.mp4", "MVI_0002.mp4"]
    assert names(tags=["2019", "isaac"], tag_mode="any") == ["Beach Trip.mp4", "IMG_0001.mp4", "MVI_0002.mp4"]
    assert names(tags=["ISAAC"], tag_mode="any") == ["Beach Trip.mp4", "MVI_0002.mp4"]  # tags ignore case
    assert queries.search_videos(conn, tags=[], tag_mode="any")["total"] == 3  # no tags: no tag filter
    assert [r["name"] for r in queries.all_matches(conn, tags=["2020", "isaac"], tag_mode="any", sort="name",
                                                   order="asc")] == ["Beach Trip.mp4", "MVI_0002.mp4"]
    with pytest.raises(ValueError):
        queries.search_videos(conn, tags=["2019"], tag_mode="either")
