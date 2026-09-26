from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vidcat import scanner
from vidcat.db import connect
from vidcat.web.app import create_app


@pytest.fixture
def client(tmp_path, library):
    db = tmp_path / "web.db"
    conn = connect(db)
    scanner.scan(conn, [library], tmp_path / "thumbs")
    conn.close()
    with TestClient(create_app(db)) as c:
        yield c


def test_list_search_sort_and_filters(client):
    data = client.get("/api/videos").json()
    assert data["total"] == 3 and len(data["items"]) == 3
    assert client.get("/api/videos", params={"q": "beach"}).json()["total"] == 1
    assert client.get("/api/videos", params={"bad_name": True}).json()["total"] == 2
    assert client.get("/api/videos", params={"duplicates": True}).json()["total"] == 2
    names = [v["name"] for v in client.get("/api/videos", params={"sort": "name", "order": "asc"}).json()["items"]]
    assert names == sorted(names, key=str.lower)
    assert client.get("/api/videos", params={"date_from": "not-a-date"}).status_code == 422
    assert client.get("/api/exts").json()[0]["ext"] == "mp4"
    assert client.get("/api/folders").json()


def test_tags_roundtrip(client):
    vid = client.get("/api/videos").json()["items"][0]
    r = client.post(f"/api/videos/{vid['id']}/tags", json={"tags": ["family", "Holiday"]}).json()
    assert r["tags"] == ["family", "Holiday"]
    assert client.get("/api/videos", params={"tag": "family"}).json()["total"] == 1
    assert [t["name"] for t in client.get("/api/tags").json()] == ["family", "Holiday"]
    r = client.delete(f"/api/videos/{vid['id']}/tags", params={"tag": "family"}).json()
    assert r["tags"] == ["Holiday"]


def test_rename_via_api(client):
    vid = next(v for v in client.get("/api/videos").json()["items"] if v["name"] == "MVI_0002.mp4")
    r = client.patch(f"/api/videos/{vid['id']}", json={"name": "Sunset Pier"})
    assert r.status_code == 200 and r.json()["name"] == "Sunset Pier.mp4" and not r.json()["bad_name"]
    assert client.patch(f"/api/videos/{vid['id']}", json={"name": "///"}).status_code == 409
    assert client.patch("/api/videos/99999", json={"name": "x"}).status_code == 404


def test_rotation_is_saved_validated_and_leaves_the_file_alone(client, library):
    items = client.get("/api/videos").json()["items"]
    vid = items[0]
    assert vid["rotation"] == 0
    before = Path(vid["path"]).read_bytes()

    for turn in (90, 180, 270, 0):
        r = client.patch(f"/api/videos/{vid['id']}", json={"rotation": turn})
        assert r.status_code == 200 and r.json()["rotation"] == turn
    client.patch(f"/api/videos/{vid['id']}", json={"rotation": 270})

    assert client.get(f"/api/videos/{vid['id']}").json()["rotation"] == 270
    listed = {v["id"]: v["rotation"] for v in client.get("/api/videos").json()["items"]}
    assert listed[vid["id"]] == 270 and sorted(set(listed.values())) == [0, 270]  # only that one video changed
    assert Path(vid["path"]).read_bytes() == before

    for bad in (45, -90, 360, "left"):
        assert client.patch(f"/api/videos/{vid['id']}", json={"rotation": bad}).status_code == 422
    # Changing other fields doesn't disturb it.
    r = client.patch(f"/api/videos/{vid['id']}", json={"caption": "hello"}).json()
    assert r["rotation"] == 270 and r["caption"] == "hello"


def test_suggest_name_without_ai(client):
    vid = next(v for v in client.get("/api/videos").json()["items"] if v["date_source"] == "metadata")
    r = client.post(f"/api/videos/{vid['id']}/suggest-name").json()
    assert r["suggestion"].startswith("2019-07-04") and r["warning"] is None


def test_media_supports_range_requests(client):
    vid = client.get("/api/videos").json()["items"][0]
    full = client.get(f"/media/{vid['id']}")
    assert full.status_code == 200 and full.headers["content-type"].startswith("video/")
    part = client.get(f"/media/{vid['id']}", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"].startswith("bytes 0-99/")


def test_thumbnails_served_and_lazily_created(client, tmp_path):
    vid = client.get("/api/videos").json()["items"][0]
    (tmp_path / "thumbs" / f"{vid['id']}.jpg").unlink()
    r = client.get(f"/thumb/{vid['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_reused_id_gets_new_media_urls(client, tmp_path, library, clip_dir):
    # SQLite hands a deleted top id to the next new file. The page puts `rev` in media and thumbnail URLs,
    # so it must change, or the browser would play its cached copy of the old video.
    db = tmp_path / "web.db"
    conn = connect(db)
    old = conn.execute("SELECT * FROM videos ORDER BY id DESC LIMIT 1").fetchone()
    old_rev = client.get(f"/api/videos/{old['id']}").json()["rev"]
    Path(old["path"]).unlink()
    conn.execute("DELETE FROM videos WHERE id = ?", (old["id"],))
    conn.commit()
    new_file = library / "Surfing.mp4"
    other = next(c for c in ("a.mp4", "b.mp4") if (clip_dir / c).stat().st_size != old["size"])  # a different video
    new_file.write_bytes((clip_dir / other).read_bytes())
    scanner.scan_files(conn, [new_file], tmp_path / "thumbs")
    conn.close()

    new = client.get(f"/api/videos/{old['id']}").json()
    assert new["name"] == "Surfing.mp4"  # same id as the deleted video...
    assert new["rev"] != old_rev          # ...but different URLs
    assert client.get(f"/media/{new['id']}", params={"v": new["rev"]}).content == new_file.read_bytes()


def test_no_arbitrary_file_access(client):
    assert client.get("/media/../../etc/passwd").status_code in (404, 422)
    assert client.get("/media/999999").status_code == 404


def test_rejects_foreign_host_and_origin(client):
    assert client.get("/api/videos", headers={"host": "evil.example.com"}).status_code == 403
    vid = client.get("/api/videos").json()["items"][0]
    r = client.post(f"/api/videos/{vid['id']}/tags", json={"tags": ["x"]}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_concurrent_requests_do_not_trip_sqlite_thread_check(client):
    """FastAPI may open a request's DB connection in one worker thread and use it in another."""
    from concurrent.futures import ThreadPoolExecutor

    vid = client.get("/api/videos").json()["items"][0]["id"]
    urls = ["/api/videos", "/api/tags", f"/api/videos/{vid}", f"/thumb/{vid}"] * 40
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert set(codes) == {200}


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Video Library" in r.text
    assert client.get("/app.js").status_code == 200


# ---------- "Save all": copy every matching video to a chosen folder

@pytest.fixture
def saver(tmp_path, library):
    """A client whose folder picker returns `picked["dest"]` (None = the user cancelled)."""
    db = tmp_path / "save.db"
    conn = connect(db)
    scanner.scan(conn, [library], tmp_path / "thumbs")
    conn.close()
    picked = {"dest": tmp_path / "Saved", "prompts": []}
    picked["dest"].mkdir()

    def choose(prompt):
        picked["prompts"].append(prompt)
        return str(picked["dest"]) if picked["dest"] else None

    with TestClient(create_app(db, choose_folder=choose)) as c:
        yield c, picked


def wait_for_save(client):
    import time
    for _ in range(200):
        job = client.get("/api/export").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("save never finished")


def test_save_all_copies_every_match_without_overwriting(saver, library):
    client, picked = saver
    dest = picked["dest"]
    (dest / "Beach Trip.mp4").write_bytes(b"already here")

    r = client.post("/api/export")
    assert r.status_code == 200 and r.json()["total"] == 3
    assert picked["prompts"] == [f"Save 3 videos ({r.json()['total_bytes'] / 1024:.1f} KB) to:"]
    job = wait_for_save(client)
    assert (job["state"], job["copied"], job["skipped"]) == ("done", 3, [])

    assert (dest / "Beach Trip.mp4").read_bytes() == b"already here"  # never overwritten
    for src in library.rglob("*.mp4"):
        copy = dest / (src.name if src.name != "Beach Trip.mp4" else "Beach Trip (2).mp4")
        assert copy.read_bytes() == src.read_bytes()
        assert abs(copy.stat().st_mtime - src.stat().st_mtime) < 1  # modified date kept
    assert not list(dest.glob(".*"))  # no temp files left
    assert sorted(p.name for p in library.rglob("*.mp4")) == ["Beach Trip.mp4", "IMG_0001.mp4", "MVI_0002.mp4"]


def test_save_all_uses_the_current_filters(saver):
    client, picked = saver
    client.post("/api/export", params={"q": "beach"})
    assert wait_for_save(client)["copied"] == 1
    assert [p.name for p in picked["dest"].iterdir()] == ["Beach Trip.mp4"]
    assert client.post("/api/export", params={"q": "no such video"}).status_code == 400


def test_save_all_cancelled_picker_copies_nothing(saver):
    client, picked = saver
    picked["dest"], saved = None, picked["dest"]
    assert client.post("/api/export").json()["state"] == "not started"
    assert client.get("/api/export").json()["state"] == "none"
    assert not any(saved.iterdir())


def test_save_all_refuses_when_there_is_no_room(saver, monkeypatch):
    from collections import namedtuple
    client, picked = saver
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("vidcat.export.shutil.disk_usage", lambda p: usage(10**9, 10**9 - 100, 100))
    r = client.post("/api/export")
    assert r.status_code == 409 and "Not enough space" in r.json()["detail"]
    assert not any(picked["dest"].iterdir())


def test_save_all_skips_missing_files(saver, library):
    client, picked = saver
    (library / "Summer Trip" / "IMG_0001.mp4").unlink()
    client.post("/api/export")
    job = wait_for_save(client)
    assert (job["state"], job["copied"]) == ("done", 2)
    assert job["skipped"] == [{"name": "IMG_0001.mp4", "reason": "file is missing"}]


def test_cancelled_save_leaves_no_partial_file(tmp_path, clip_dir, monkeypatch):
    import threading
    from vidcat import export

    class CancelPartWay(threading.Event):
        """Reports "cancelled" once the copy is a few chunks into the file."""
        def __init__(self, after):
            super().__init__()
            self.after = after

        def is_set(self):
            self.after -= 1
            return self.after < 0

    monkeypatch.setattr(export, "CHUNK", 64)  # many small reads, so the cancel lands mid-file
    dest = tmp_path / "out"
    dest.mkdir()
    src = clip_dir / "b.mp4"
    job = export.ExportJob(dest=dest, files=[(str(src), "b.mp4", src.stat().st_size)], cancel_event=CancelPartWay(5))
    export.run(job)
    assert (job.state, job.copied, job.done_bytes) == ("cancelled", 0, 0)
    assert list(dest.iterdir()) == []
