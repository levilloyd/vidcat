import struct

from vidcat import media, scanner


def klv(key: bytes, typ: str, size: int, repeat: int, body: bytes) -> bytes:
    """One GPMF entry, padded to 4 bytes like the camera writes it."""
    return key + typ.encode() + bytes([size]) + struct.pack(">H", repeat) + body + b"\0" * (-len(body) % 4)


def nested(key: bytes, *children: bytes) -> bytes:
    body = b"".join(children)
    return key + b"\0" + bytes([1]) + struct.pack(">H", len(body)) + body


def gps5_stream(fix: int, dop: int, *points: tuple[float, float]) -> bytes:
    scal = struct.pack(">5i", 10_000_000, 10_000_000, 1000, 1000, 100)
    samples = b"".join(struct.pack(">5i", round(lat * 1e7), round(lon * 1e7), 12000, 0, 0) for lat, lon in points)
    return nested(b"DEVC", nested(
        b"STRM",
        klv(b"STNM", "c", 1, 4, b"GPS!"),
        klv(b"GPSF", "L", 4, 1, struct.pack(">I", fix)),
        klv(b"GPSP", "S", 2, 1, struct.pack(">H", dop)),
        klv(b"SCAL", "l", 4, 5, scal),
        klv(b"GPS5", "l", 20, len(points), samples),
    ))


def test_gps5_first_fixed_position():
    data = gps5_stream(3, 150, (36.9337765, -121.8656116), (36.934, -121.866))
    assert media.parse_gpmf(data) == (36.9337765, -121.8656116)


def test_positions_before_a_fix_are_skipped():
    data = gps5_stream(0, 9999, (12.0, 34.0)) + gps5_stream(3, 150, (37.7359941, -121.6702724))
    assert media.parse_gpmf(data) == (37.7359941, -121.6702724)


def test_precise_position_preferred_over_an_earlier_rough_one():
    data = gps5_stream(2, 2500, (37.0, -122.0)) + gps5_stream(3, 200, (37.5, -121.5))
    assert media.parse_gpmf(data) == (37.5, -121.5)
    assert media.parse_gpmf(gps5_stream(2, 2500, (37.0, -122.0))) == (37.0, -122.0)  # rough beats nothing


def test_gps9_carries_fix_in_each_sample():
    scal = struct.pack(">9i", 10_000_000, 10_000_000, 1000, 1000, 100, 1, 1000, 100, 1)
    def sample(lat, lon, dop, fix):
        return struct.pack(">7i2H", round(lat * 1e7), round(lon * 1e7), 0, 0, 0, 0, 0, dop, fix)
    data = nested(b"DEVC", nested(
        b"STRM", klv(b"SCAL", "l", 4, 9, scal),
        klv(b"GPS9", "?", 32, 2, sample(1.0, 2.0, 9999, 0) + sample(51.5007, -0.1246, 120, 3)),
    ))
    assert media.parse_gpmf(data) == (51.5007, -0.1246)


def test_no_gps_in_telemetry():
    assert media.parse_gpmf(b"") is None
    assert media.parse_gpmf(gps5_stream(0, 9999, (12.0, 34.0))) is None
    assert media.parse_gpmf(b"\x01\x02garbage") is None
    accel = nested(b"DEVC", nested(b"STRM", klv(b"ACCL", "s", 6, 1, b"\0" * 6)))
    assert media.parse_gpmf(accel) is None


def test_gopro_location_only_reads_files_with_a_telemetry_track(monkeypatch):
    calls = []
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: calls.append(a) or None)
    assert media.gopro_location("x.mp4", {"streams": [{"index": 0, "codec_tag_string": "avc1"}]}) is None
    assert media.gopro_location("x.mp4", None) is None
    assert not calls


def test_scan_uses_gopro_gps(conn, tmp_path, clip_dir, thumbs, monkeypatch):
    import shutil
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_dir / "b.mp4", lib / "GOPR0001.MP4")
    monkeypatch.setattr(media, "gopro_location", lambda path, data: (36.9337765, -121.8656116))
    scanner.scan(conn, [lib], thumbs)
    row = conn.execute("SELECT latitude, longitude FROM videos").fetchone()
    assert tuple(row) == (36.9337765, -121.8656116)


def test_catalogs_scanned_before_gopro_support_look_again(tmp_path):
    from vidcat import db
    path = tmp_path / "old.db"
    conn = db.connect(path)
    for name, lat in (("a.mp4", None), ("b.mp4", 37.0), ("c.avi", None)):
        conn.execute(
            "INSERT INTO videos (path, dir, name, ext, size, mtime, created_at, added_at, scanned_at, latitude, "
            "location_read) VALUES (?, '/x', ?, ?, 1, 1, 1, 1, 1, ?, 1)", (f"/x/{name}", name, name[2:], lat))
    conn.execute("PRAGMA user_version = 0")   # what a catalog from the first location release looks like
    conn.commit()
    conn.close()
    conn = db.connect(path)
    got = dict(conn.execute("SELECT name, location_read FROM videos").fetchall())
    assert got == {"a.mp4": 0, "b.mp4": 1, "c.avi": 1}
    conn.execute("UPDATE videos SET location_read = 1")
    conn.commit()
    conn.close()
    assert db.connect(path).execute("SELECT MIN(location_read) FROM videos").fetchone()[0] == 1  # only once
