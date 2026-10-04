import sqlite3
from datetime import datetime

import pytest

from vidcat import db, media, names, scanner


def d(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


@pytest.mark.parametrize("name, expected", [
    ("02_19_04 017.mpg", "2004-02-19"),          # month_day_year, the camcorder-export style
    ("03_20_04 020.mpg", "2004-03-20"),
    ("2-19-2004 birthday.mpg", "2004-02-19"),
    ("Party 12.25.99.avi", "1999-12-25"),
    ("25_12_04.mpg", "2004-12-25"),               # day-first only when month-first is impossible
    ("VID_20190704_123456.mp4", "2019-07-04"),
    ("2019-07-04 Fireworks.mov", "2019-07-04"),
    ("Clip 2019_07_04.mov", "2019-07-04"),
])
def test_date_from_filename(name, expected):
    assert d(media.date_from_filename(name)) == expected


@pytest.mark.parametrize("name", [
    "priest_lake 023.mpg", "IMG_4821.MOV", "otterstroms2.mpg", "Wendy's sixtieth B-day Party.MPG",
    "13_45_04.mpg",      # no valid month/day reading
    "02_30_04.mpg",      # Feb 30
    "1_2_3.mpg",         # not a plausible year form
    "123456.mp4",
])
def test_no_date_in_filename(name):
    assert media.date_from_filename(name) is None


def test_date_source_precedence():
    mtime = datetime(2004, 9, 15, 14, 9).timestamp()
    meta = {"format": {"duration": "3", "tags": {"creation_time": "2019-07-04T21:32:10Z"}},
            "streams": [{"codec_type": "video"}]}
    bare = {"format": {"duration": "3"}, "streams": [{"codec_type": "video"}]}

    assert media.parse_probe(meta, "02_19_04 017.mpg", mtime)["date_source"] == "metadata"
    info = media.parse_probe(bare, "02_19_04 017.mpg", mtime)
    assert info["date_source"] == "filename" and d(info["created_at"]) == "2004-02-19"
    info = media.parse_probe(bare, "kids.wmv", mtime)
    assert info["date_source"] == "mtime" and info["created_at"] == int(mtime)
    assert media.parse_probe(None, "kids.wmv", mtime)["date_source"] == "mtime"  # unreadable file


def test_bogus_metadata_date_is_ignored():
    mtime = datetime(2004, 9, 15).timestamp()
    data = {"format": {"tags": {"creation_time": "1970-01-01T00:00:00Z"}}, "streams": [{"codec_type": "video"}]}
    assert media.parse_probe(data, "x.mpg", mtime)["date_source"] == "mtime"


def test_suggestions_do_not_present_a_copy_date_as_capture_date(tmp_path):
    ts = int(datetime(2004, 9, 15, 14, 9).timestamp())
    base = {"path": str(tmp_path / "Otterstroms" / "IMG_1.mpg"), "created_at": ts}

    # mtime-only date + a descriptive folder: leave the date out
    assert names.suggest_name({**base, "date_source": "mtime"}) == "Otterstroms"
    # ...unless an AI caption/folder isn't available, then the date (with time) is the best we have
    bare = {**base, "path": "/Volumes/DCIM/100APPLE/IMG_1.mpg", "date_source": "mtime"}  # nothing descriptive
    assert names.suggest_name(bare) == "2004-09-15 14-09-00"
    # trustworthy dates are used; filename dates carry no time of day
    assert names.suggest_name({**base, "date_source": "metadata"}) == "2004-09-15 Otterstroms"
    assert names.suggest_name({**bare, "date_source": "filename"}) == "2004-09-15"
    assert names.suggest_name({**base, "date_source": "mtime"}, "Wedding Dance") == "Otterstroms - Wedding Dance"


def test_scan_records_date_source(conn, tmp_path, clip_dir, thumbs):
    import shutil
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_dir / "b.mp4", lib / "03_20_04 020.mp4")   # no embedded date -> from file name
    shutil.copy(clip_dir / "a.mp4", lib / "with_meta.mp4")      # embedded creation_time
    shutil.copy(clip_dir / "b.mp4", lib / "kids.mp4")           # nothing -> file modified time
    scanner.scan(conn, [lib], thumbs)
    got = {r["name"]: (r["date_source"], d(r["created_at"])) for r in conn.execute("SELECT * FROM videos")}
    assert got["03_20_04 020.mp4"] == ("filename", "2004-03-20")
    assert got["with_meta.mp4"] == ("metadata", "2019-07-04")
    assert got["kids.mp4"][0] == "mtime"


def test_old_catalog_is_migrated_and_refreshed_by_rescan(tmp_path, clip_dir, thumbs):
    """A catalog made before date_source existed gains the column, and the next scan fills it in without
    losing tags/captions or invalidating cached hashes."""
    import shutil
    from vidcat import tags

    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_dir / "b.mp4", lib / "03_20_04 020.mp4")
    path = tmp_path / "old.db"

    conn = db.connect(path)
    scanner.scan(conn, [lib], thumbs)
    vid = conn.execute("SELECT id FROM videos").fetchone()["id"]
    tags.add_tags(conn, vid, ["wedding"])
    conn.execute("UPDATE videos SET caption = 'kept', sha256 = 'abc', created_at = 0, date_source = NULL WHERE id = ?", (vid,))
    conn.commit()
    # Simulate the pre-migration schema: drop the column entirely.
    conn.execute("ALTER TABLE videos DROP COLUMN date_source")
    conn.commit()
    conn.close()

    conn = db.connect(path)  # migration adds the column back
    assert "date_source" in {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    s = scanner.scan(conn, [lib], thumbs)
    assert (s.updated, s.added, s.unchanged) == (1, 0, 0)

    row = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    assert (row["date_source"], d(row["created_at"])) == ("filename", "2004-03-20")
    assert row["caption"] == "kept" and row["sha256"] == "abc"  # nothing else was disturbed
    assert tags.tags_for(conn, [vid])[vid] == ["wedding"]
    assert scanner.scan(conn, [lib], thumbs).unchanged == 1  # and it settles


def test_catalog_without_rotation_column_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO videos (path, dir, name, ext, size, mtime, created_at, added_at, scanned_at) "
        "VALUES ('/x/a.mp4', '/x', 'a.mp4', 'mp4', 1, 1, 1, 1, 1)"
    )
    conn.commit()
    conn.execute("ALTER TABLE videos DROP COLUMN rotation")   # what a catalog from before this feature looks like
    conn.commit()
    conn.close()

    conn = db.connect(path)
    assert conn.execute("SELECT rotation FROM videos").fetchone()[0] == 0   # existing rows: not rotated
    conn.execute("UPDATE videos SET rotation = 90")
    conn.commit()
    conn.close()
    assert db.connect(path).execute("SELECT rotation FROM videos").fetchone()[0] == 90  # and reconnecting is a no-op


def test_keep_name_forever_survives_a_metadata_refresh(conn, tmp_path, clip_dir, thumbs):
    import shutil
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_dir / "a.mp4", lib / "IMG_0001.mp4")
    scanner.scan(conn, [lib], thumbs)
    conn.execute("UPDATE videos SET name_score = 100, date_source = NULL")  # user chose "keep", old-style row
    conn.commit()
    scanner.scan(conn, [lib], thumbs)
    assert conn.execute("SELECT name_score FROM videos").fetchone()[0] == 100


@pytest.mark.parametrize("value, expected", [
    ("+37.6498-121.7799+153.234/", (37.6498, -121.7799)),   # iPhone, with altitude
    ("+51.5007-000.1246/", (51.5007, -0.1246)),             # Android / ffmpeg, no altitude
    ("-33.8568+151.2153/", (-33.8568, 151.2153)),
    ("+00.0000+000.0000/", None),                           # camera with no GPS fix
    ("+95.0000+010.0000/", None),
    ("", None),
    ("somewhere", None),
])
def test_parse_iso6709(value, expected):
    assert media.parse_iso6709(value) == expected


def test_location_from_probe():
    mtime = datetime(2004, 9, 15).timestamp()
    iphone = {"format": {"tags": {"com.apple.quicktime.location.ISO6709": "+37.6498-121.7799+153.234/"}},
              "streams": [{"codec_type": "video"}]}
    android = {"format": {"tags": {"location": "+51.5007-000.1246/"}}, "streams": [{"codec_type": "video"}]}
    empty = {"format": {"tags": {"com.apple.quicktime.location.ISO6709": ""}}, "streams": [{"codec_type": "video"}]}
    info = media.parse_probe(iphone, "IMG_1.MOV", mtime)
    assert (info["latitude"], info["longitude"]) == (37.6498, -121.7799)
    assert media.parse_probe(android, "x.mp4", mtime)["latitude"] == 51.5007
    assert media.parse_probe(empty, "x.mov", mtime)["latitude"] is None
    assert media.parse_probe(None, "x.mov", mtime)["latitude"] is None


def test_scan_records_location_and_old_catalogs_pick_it_up(tmp_path, thumbs):
    import subprocess
    lib = tmp_path / "lib"
    lib.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                    "-metadata", "location=+37.6498-121.7799/", "-pix_fmt", "yuv420p", str(lib / "geo.mp4")], check=True)
    path = tmp_path / "old.db"
    conn = db.connect(path)
    scanner.scan(conn, [lib], thumbs)
    row = conn.execute("SELECT * FROM videos").fetchone()
    assert (row["latitude"], row["longitude"], row["location_read"]) == (37.6498, -121.7799, 1)

    # A catalog from before locations were recorded: the columns are added and the next scan fills them in.
    conn.execute("UPDATE videos SET sha256 = 'abc'")
    conn.commit()
    for col in ("latitude", "longitude", "location_read"):
        conn.execute(f"ALTER TABLE videos DROP COLUMN {col}")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT latitude, location_read FROM videos").fetchone()[:] == (None, 0)
    s = scanner.scan(conn, [lib], thumbs)
    assert (s.updated, s.added) == (1, 0)
    row = conn.execute("SELECT * FROM videos").fetchone()
    assert (row["latitude"], row["longitude"], row["sha256"]) == (37.6498, -121.7799, "abc")
    assert scanner.scan(conn, [lib], thumbs).unchanged == 1


def test_formats_without_location_are_not_reread_after_migration(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    for name in ("a.avi", "b.mov"):
        conn.execute(
            "INSERT INTO videos (path, dir, name, ext, size, mtime, created_at, added_at, scanned_at) "
            f"VALUES ('/x/{name}', '/x', '{name}', '{name[2:]}', 1, 1, 1, 1, 1)"
        )
    conn.commit()
    conn.execute("ALTER TABLE videos DROP COLUMN location_read")
    conn.execute("ALTER TABLE videos DROP COLUMN latitude")
    conn.execute("ALTER TABLE videos DROP COLUMN longitude")
    conn.commit()
    conn.close()
    got = dict(db.connect(path).execute("SELECT ext, location_read FROM videos").fetchall())
    assert got == {"avi": 1, "mov": 0}
