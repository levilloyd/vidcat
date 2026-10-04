import httpx
import pytest

from vidcat import places

SANTA_CRUZ = {  # trimmed Nominatim reply
    "category": "leisure", "name": "Lighthouse Field",
    "address": {"road": "West Cliff Drive", "suburb": "Downtown", "city": "Santa Cruz", "county": "Santa Cruz County",
                "state": "California", "country": "United States"},
}


@pytest.mark.parametrize("result, expected", [
    (SANTA_CRUZ, "Lighthouse Field, Santa Cruz, California, United States"),
    ({**SANTA_CRUZ, "category": "highway", "name": "West Cliff Drive"}, "Santa Cruz, California, United States"),
    ({"category": "highway", "name": "Half Dome Trail",   # no town: the county stands in
      "address": {"county": "Mariposa County", "state": "California", "country": "United States"}},
     "Mariposa County, California, United States"),
    ({"category": "boundary", "name": "Livermore", "address": {"city": "Livermore", "state": "California"}},
     "Livermore, California"),
    ({}, ""),
])
def test_describe(result, expected):
    assert places.describe(result) == expected


def test_place_names_are_cached_by_spot(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(places, "_lookup", lambda lat, lon, lang: calls.append((lat, lon)) or "Santa Cruz")
    assert places.place_name(conn, 36.95171, -122.02581) == "Santa Cruz"
    assert places.place_name(conn, 36.95174, -122.02579) == "Santa Cruz"   # a few metres away: same lookup
    assert len(calls) == 1
    places.place_name(conn, 37.6846, -121.7294)                            # somewhere else
    assert len(calls) == 2


def test_nowhere_is_cached_and_failures_are_not(conn, monkeypatch):
    monkeypatch.setattr(places, "_lookup", lambda *a: "")
    assert places.place_name(conn, 36.5, -123.5) is None                   # open sea
    def offline(*a):
        raise places.PlaceUnavailable("offline")
    monkeypatch.setattr(places, "_lookup", offline)
    assert places.place_name(conn, 36.5, -123.5) is None                   # answered from the cache
    with pytest.raises(places.PlaceUnavailable):
        places.place_name(conn, 10.0, 10.0)
    assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1


def test_lookup_request_follows_nominatim_policy(monkeypatch):
    sent = []
    def fake_get(url, params, headers, timeout):
        sent.append((params, headers))
        return httpx.Response(200, json=SANTA_CRUZ, request=httpx.Request("GET", url))
    monkeypatch.setattr(places.httpx, "get", fake_get)
    monkeypatch.setattr(places, "_last_request", 0.0)
    sleeps = []
    monkeypatch.setattr(places.time, "sleep", sleeps.append)
    assert places._lookup(36.9517, -122.0258, "en-US") == "Lighthouse Field, Santa Cruz, California, United States"
    places._lookup(36.9517, -122.0258, None)
    params, headers = sent[0]
    assert headers["User-Agent"].startswith("vidcat/") and headers["Accept-Language"] == "en-US"
    assert sent[1][1]["Accept-Language"] == "en"
    assert sleeps[-1] > 0.9                                                 # second request waited about a second


def test_lookup_errors_become_place_unavailable(monkeypatch):
    def fail(*a, **k):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(places.httpx, "get", fail)
    monkeypatch.setattr(places.time, "sleep", lambda s: None)
    with pytest.raises(places.PlaceUnavailable):
        places._lookup(1.0, 1.0, None)


@pytest.mark.parametrize("lat, lon", [
    (36.9517, -122.0258), (12.0625, -121.0625),   # x1000 is exactly .5: Python's round() would go to even
    (-33.8568, 151.2153), (0.0005, -0.0005), (89.9999, 179.9999),
])
def test_cache_key_matches_the_sql_lookup(conn, lat, lon):
    conn.execute("INSERT INTO videos (path, dir, name, ext, size, mtime, created_at, added_at, scanned_at, latitude, "
                 "longitude) VALUES ('/x/a.mp4', '/x', 'a.mp4', 'mp4', 1, 1, 1, 1, 1, ?, ?)", (lat, lon))
    conn.execute("INSERT INTO places (lat_key, lon_key, name) VALUES (?, ?, 'Here')", places._key(lat, lon))
    assert conn.execute(f"SELECT {places.PLACE_SQL} FROM videos v").fetchone()[0] == "Here"


def _catalog(tmp_path, *positions):
    from vidcat import db
    path = tmp_path / "places.db"
    conn = db.connect(path)
    for i, (lat, lon) in enumerate(positions):
        conn.execute("INSERT INTO videos (path, dir, name, ext, size, mtime, created_at, added_at, scanned_at, latitude, "
                     "longitude) VALUES (?, '/x', ?, 'mp4', 1, 1, ?, 1, 1, ?, ?)", (f"/x/{i}.mp4", f"{i}.mp4", i, lat, lon))
    conn.commit()
    return path, conn


def test_unnamed_spots_groups_nearby_videos_and_skips_saved_ones(tmp_path):
    _, conn = _catalog(tmp_path, (36.95171, -122.02581), (36.95174, -122.02579), (37.6846, -121.7294), (None, None))
    assert len(places.unnamed_spots(conn)) == 2                   # two clips a few metres apart are one spot
    conn.execute("INSERT INTO places (lat_key, lon_key, name) VALUES (?, ?, '')", places._key(37.6846, -121.7294))
    assert places.unnamed_spots(conn) == [(36.95171, -122.02581)]  # "nothing there" counts as looked up


def test_places_command_looks_up_each_spot_once(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from vidcat import cli
    path, conn = _catalog(tmp_path, (36.95171, -122.02581), (36.95174, -122.02579), (37.6846, -121.7294), (10.0, -150.0))
    conn.close()
    calls = []
    def lookup(lat, lon, lang):
        calls.append(lang)
        return "" if lat == 10.0 else f"Place {len(calls)}"
    monkeypatch.setattr(places, "_lookup", lookup)
    run = lambda *args: CliRunner().invoke(cli.app, ["--db", str(path), "places", *args])

    r = run("--dry-run")
    assert r.exit_code == 0 and "3 spots to look up" in r.output and not calls
    r = run("--language", "fr")
    assert r.exit_code == 0 and "2 place names saved, 1 with nothing there" in r.output
    assert calls == ["fr", "fr", "fr"]
    r = run()
    assert "already has its place name" in r.output and len(calls) == 3  # nothing left to do


def test_places_command_stops_when_offline_and_keeps_what_it_found(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from vidcat import cli
    path, conn = _catalog(tmp_path, *[(30.0 + i, -120.0) for i in range(6)])
    conn.close()
    answers = iter(["Somewhere"])
    def lookup(*a):
        try:
            return next(answers)
        except StopIteration:
            raise places.PlaceUnavailable("Place lookup failed: no network") from None
    monkeypatch.setattr(places, "_lookup", lookup)
    r = CliRunner().invoke(cli.app, ["--db", str(path), "places"])
    assert r.exit_code == 0 and "Stopped: Place lookup failed" in r.output
    assert "1 place name saved; 5 still to look up" in r.output
