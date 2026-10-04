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
