from pathlib import Path

import pytest

from vidcat import names
from vidcat.names import name_quality, sanitize_stem


@pytest.mark.parametrize("stem", [
    "IMG_1234", "MVI_0042", "VID_20190704_123456", "PXL_20230704_123456789", "DSC00123", "GOPR0001",
    "20190704_123456", "2019-07-04", "123456", "Untitled", "video", "Video 12", "IMG_1234 copy",
    "WhatsApp Video 2019-07-04 at 12.30.15", "Screen Recording 2020-01-01 at 10.00.00",
    "3f2a9c1e8b7d4a6f9e0c1b2a3d4e5f60", "a3f9bc12-4d5e-4f60-8a1b-2c3d4e5f6a7b", "MOV0123",
    "PICT0885", "CIMG1234", "HPIM0042", "SANY0001", "SDC10001", "MVC-001F", "DJI_0001",
])
def test_bad_names(stem):
    assert name_quality(stem) < 50, stem


@pytest.mark.parametrize("stem", [
    "Beach Trip", "Emma's 5th Birthday", "2019-07-04 Fireworks at the Lake", "Grandma Christmas 2015",
    "Graduation", "祖母の誕生日",
])
def test_good_names(stem):
    assert name_quality(stem) >= 50, stem


def test_sanitize():
    assert sanitize_stem('a/b:c*d?"e<f>g|h') == "a b c d e f g h"
    assert sanitize_stem("  ..hello..  ") == "hello"
    assert sanitize_stem("///") == ""


def test_suggest_uses_date_folder_and_caption(tmp_path):
    video = {"path": str(tmp_path / "Beach Trip" / "IMG_1.mov"), "created_at": 1562275930}
    s = names.suggest_name(video)
    assert s.startswith("20") and "Beach Trip" in s
    assert names.suggest_name(video, "Kids Building Sandcastles").endswith("- Kids Building Sandcastles")


def test_bare_date_and_caption_are_joined_with_a_space_not_a_dash(tmp_path):
    # No meaningful folder, a trustworthy date, and a caption: "2020-11-29 Family Cooking Fun Together",
    # never "2020-11-29 - Family Cooking Fun Together". Three generic folders push pytest's own (descriptive-
    # looking) tmp_path name out of the 3-level lookup, so it isn't mistaken for a real folder label.
    video = {"path": str(tmp_path / "DCIM" / "100APPLE" / "Videos" / "clip.mov"),
             "created_at": 1606636800, "date_source": "metadata"}
    assert names._folder_label(Path(video["path"])) is None  # confirms the setup, not just the outcome
    s = names.suggest_name(video, "Family Cooking Fun Together")
    assert s == "2020-11-29 Family Cooking Fun Together"
    assert " - " not in s


def test_suggest_without_folder_adds_time(tmp_path):
    video = {"path": str(tmp_path / "DCIM" / "100APPLE" / "IMG_1.mov"), "created_at": 1562275930}
    s = names.suggest_name(video)
    assert "Beach" not in s and len(s.split()) == 2  # "YYYY-MM-DD HH-MM-SS"


@pytest.mark.parametrize("folder", [
    "Home Videos", "home videos", "HOME VIDEOS", "My Family Movies", "Family", "Old Videos", "Videos 2004",
    "Movies", "DCIM", "100APPLE", "Camera Uploads", "2007", "New Folder", "Family Video Archive", ".hidden",
])
def test_generic_folders_are_not_used_as_labels(folder):
    assert names.is_generic_folder(folder), folder


@pytest.mark.parametrize("folder", [
    "Isaac", "Otterstroms", "Levi and Rebecca", "Camp Zarahemla", "Home Videos - Christmas", "Priest Lake",
    "Grandma's House", "Eliza",
])
def test_meaningful_folders_are_kept(folder):
    assert not names.is_generic_folder(folder), folder


def test_suggestion_skips_home_videos_but_keeps_looking_upward(tmp_path):
    ts = 1562275930
    # The case from a real archive: files in year folders under "Home Videos"
    video = {"path": "/Volumes/Memories/Home Videos/2007/Elizas_first_steps.mov", "created_at": ts, "date_source": "mtime"}
    assert names.suggest_name(video, "Baby Takes First Steps") == "Baby Takes First Steps"
    assert "Home Videos" not in names.suggest_name(video)
    assert names.suggest_name({**video, "date_source": "metadata"}, "Baby Takes First Steps") == \
        "2019-07-04 Baby Takes First Steps"  # bare date + caption: a space, never "date - caption"

    # A generic folder is skipped, but a meaningful one further up is still found.
    deep = {"path": "/x/Isaac/Home Videos/Movies/clip.mov", "created_at": ts, "date_source": "metadata"}
    assert names.suggest_name(deep, "Painting In A High Chair") == "2019-07-04 Isaac - Painting In A High Chair"


@pytest.mark.parametrize("path", [
    "/Volumes/Memories/clip.mov",                       # straight on a share: its name isn't a label
    "/Volumes/Memories/Home Videos/clip.mov",
    "/Volumes/Big Backup Drive/2007/clip.mov",
])
def test_volume_names_are_never_used_as_labels(path):
    assert names._folder_label(Path(path)) is None
    assert "Memories" not in names.suggest_name({"path": path, "created_at": 1562275930, "date_source": "metadata"}, "X Y")
