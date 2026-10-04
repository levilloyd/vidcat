import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vidcat import cli, db, disposal, media, tags, transcode
from vidcat.transcode import Settings, TranscodeError

runner = CliRunner()
FAST = Settings(preset="ultrafast")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture
def old_lib(tmp_path, old_clips):
    lib = tmp_path / "Old Tapes"
    lib.mkdir()
    shutil.copy(old_clips / "a.mpg", lib / "03_20_04 020.mpg")   # date only in the file name
    shutil.copy(old_clips / "b.wmv", lib / "kids.wmv")           # no date anywhere but the file time
    return lib


def streams(path):
    data = media.probe(path)
    return {s["codec_type"]: s for s in data["streams"]}, float(data["format"]["duration"]), data["format"]


def test_convert_produces_verified_h264_aac_mp4(old_lib):
    src = old_lib / "03_20_04 020.mpg"
    ts = int(datetime(2004, 3, 20).timestamp())
    progress = []
    dst, encoded = transcode.convert(src, FAST, creation_time=ts, on_progress=progress.append)

    assert encoded and dst == old_lib / "03_20_04 020.mp4"
    s, duration, fmt = streams(dst)
    assert s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert (s["video"]["width"], s["video"]["height"]) == (160, 120)
    assert abs(duration - 2.0) < 0.3
    assert fmt["tags"]["creation_time"].startswith("2004-03-2")          # date carried into the file
    assert dst.stat().st_mtime == pytest.approx(src.stat().st_mtime, abs=1)  # mtime preserved
    assert src.exists()                                                  # original untouched
    assert not list(old_lib.glob(".*vidcat-tmp*"))                       # no temp leftovers
    assert progress and progress[-1] == pytest.approx(1.0, abs=0.1)


def test_iphone_location_is_carried_into_the_mp4(tmp_path):
    src = tmp_path / "IMG_0001.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=1",
                    "-c:v", "mjpeg", "-movflags", "use_metadata_tags",
                    "-metadata", "com.apple.quicktime.location.ISO6709=+37.6498-121.7799+153.234/", str(src)], check=True)
    dst, _ = transcode.convert(src, FAST)
    info = media.parse_probe(media.probe(dst), dst.name, 0)
    assert (info["latitude"], info["longitude"]) == (37.6498, -121.7799)


def test_wmv_converts_too(old_lib):
    dst, encoded = transcode.convert(old_lib / "kids.wmv", FAST)
    s, _, fmt = streams(dst)
    assert encoded and s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert "creation_time" not in fmt.get("tags", {})  # nothing trustworthy to record


def test_hevc_option(old_lib):
    dst, _ = transcode.convert(old_lib / "kids.wmv", Settings(codec="hevc", preset="ultrafast"))
    s, _, _ = streams(dst)
    assert s["video"]["codec_name"] == "hevc" and s["video"]["codec_tag_string"] == "hvc1"


def test_rerun_reuses_existing_good_output(old_lib):
    src = old_lib / "kids.wmv"
    dst, _ = transcode.convert(src, FAST)
    before = dst.stat().st_mtime_ns
    dst2, encoded = transcode.convert(src, FAST)
    assert dst2 == dst and encoded is False and dst.stat().st_mtime_ns == before


def test_unrelated_existing_mp4_is_never_overwritten(old_lib, clip_dir):
    src = old_lib / "kids.wmv"
    other = old_lib / "kids.mp4"
    shutil.copy(clip_dir / "b.mp4", other)  # 2s smpte clip: different length? make it clearly different
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=9", "-pix_fmt", "yuv420p", str(other))
    original_bytes = other.read_bytes()
    with pytest.raises(TranscodeError, match="doesn't match"):
        transcode.convert(src, FAST)
    assert other.read_bytes() == original_bytes


def test_unreadable_source_fails_cleanly(tmp_path):
    bad = tmp_path / "broken.mpg"
    bad.write_bytes(b"not video at all")
    with pytest.raises(TranscodeError, match="can't read"):
        transcode.convert(bad, FAST)
    assert not list(tmp_path.glob(".*"))


def test_ffmpeg_failure_leaves_no_partial_file(old_lib, monkeypatch):
    real_build = transcode.build_command
    monkeypatch.setattr(transcode, "build_command",
                        lambda *a, **k: real_build(*a, **k)[:-1] + ["/nonexistent-dir/out.mp4"])
    with pytest.raises(TranscodeError):
        transcode.convert(old_lib / "kids.wmv", FAST)
    assert not (old_lib / "kids.mp4").exists() and not list(old_lib.glob(".*vidcat-tmp*"))


def test_verify_output_catches_truncated_or_silent_results(old_lib):
    dst, _ = transcode.convert(old_lib / "kids.wmv", FAST)
    assert transcode.verify_output(dst, 2.0, need_audio=True) is None
    assert "length differs" in transcode.verify_output(dst, 60.0, need_audio=True)
    silent = old_lib / "silent.mp4"
    ffmpeg("-i", str(dst), "-an", "-c:v", "copy", str(silent))
    assert "audio" in transcode.verify_output(silent, 2.0, need_audio=True)
    assert transcode.verify_output(silent, 2.0, need_audio=False) is None


def test_interlace_detection(tmp_path):
    progressive = tmp_path / "p.mp4"
    interlaced = tmp_path / "i.mpg"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3", "-pix_fmt", "yuv420p", str(progressive))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=50:duration=3", "-vf", "tinterlace=mode=interleave_top",
           "-flags", "+ilme+ildct", "-c:v", "mpeg2video", "-b:v", "4M", str(interlaced))
    assert transcode.detect_interlaced(progressive) is False
    assert transcode.detect_interlaced(interlaced) is True


# --------------------------------------------------------------------------- CLI end to end

def run(db_path, *args):
    result = runner.invoke(cli.app, ["--db", str(db_path), *args])
    return result


def test_cli_dry_run_changes_nothing(tmp_path, old_lib):
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(old_lib)).exit_code == 0
    r = run(db_path, "transcode", "--dry-run")
    assert r.exit_code == 0 and "kids.mp4" in r.output and "Dry run" in r.output
    assert sorted(p.name for p in old_lib.iterdir()) == ["03_20_04 020.mpg", "kids.wmv"]


def test_cli_rejects_bad_options(tmp_path, old_lib):
    db_path = tmp_path / "c.db"
    run(db_path, "scan", str(old_lib))
    assert run(db_path, "transcode", "--codec", "vp9").exit_code == 2
    assert run(db_path, "transcode", "--originals", "shred").exit_code == 2


def test_cli_transcode_then_trash_originals(tmp_path, old_lib, monkeypatch):
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(old_lib)).exit_code == 0
    conn = db.connect(db_path)
    orig = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM videos")}
    tags.add_tags(conn, orig["03_20_04 020.mpg"], ["wedding", "2004"])
    conn.execute("UPDATE videos SET caption = 'Dancing', rotation = 90 WHERE id = ?", (orig["03_20_04 020.mpg"],))
    conn.commit()
    conn.close()

    # 1) Convert, keep originals (non-interactive default for --originals ask).
    r = run(db_path, "transcode", "--preset", "ultrafast")
    assert r.exit_code == 0, r.output
    assert "Converted 2 file(s)" in r.output and "Originals kept" in r.output
    assert sorted(p.name for p in old_lib.iterdir()) == [
        "03_20_04 020.mp4", "03_20_04 020.mpg", "kids.mp4", "kids.wmv"]

    conn = db.connect(db_path)
    new = conn.execute("SELECT * FROM videos WHERE name = '03_20_04 020.mp4'").fetchone()
    assert tags.tags_for(conn, [new["id"]])[new["id"]] == ["2004", "wedding"]   # tags followed
    assert new["caption"] == "Dancing" and new["rotation"] == 90   # same picture, same turn needed
    assert new["date_source"] == "filename"  # still "day only, from the file name", not a made-up midnight
    assert datetime.fromtimestamp(new["created_at"]).strftime("%Y-%m-%d") == "2004-03-20"
    kids_old = conn.execute("SELECT * FROM videos WHERE name = 'kids.wmv'").fetchone()
    kids_new = conn.execute("SELECT * FROM videos WHERE name = 'kids.mp4'").fetchone()
    assert kids_new["date_source"] == "mtime" and abs(kids_new["created_at"] - kids_old["created_at"]) <= 1
    conn.close()

    # 2) Re-run with trash: nothing is re-encoded, originals are trashed and their catalog rows dropped.
    trashed = []
    monkeypatch.setattr(disposal, "send2trash", lambda p: (trashed.append(Path(p).name), os.remove(p)))
    mtimes = {p.name: p.stat().st_mtime_ns for p in old_lib.glob("*.mp4")}
    r = run(db_path, "transcode", "--originals", "trash")
    assert r.exit_code == 0, r.output
    assert sorted(trashed) == ["03_20_04 020.mpg", "kids.wmv"]
    assert {p.name: p.stat().st_mtime_ns for p in old_lib.glob("*.mp4")} == mtimes  # not re-encoded
    assert sorted(p.name for p in old_lib.iterdir()) == ["03_20_04 020.mp4", "kids.mp4"]

    conn = db.connect(db_path)
    assert sorted(r["name"] for r in conn.execute("SELECT name FROM videos")) == ["03_20_04 020.mp4", "kids.mp4"]
    assert tags.tags_for(conn, [new["id"]])[new["id"]] == ["2004", "wedding"]
    conn.close()


def test_cli_never_trashes_original_when_conversion_fails(tmp_path, old_lib, monkeypatch):
    db_path = tmp_path / "c.db"
    run(db_path, "scan", str(old_lib))
    monkeypatch.setattr(disposal, "send2trash", lambda p: pytest.fail("must not trash anything"))
    monkeypatch.setattr(transcode, "encode", lambda *a, **k: (_ for _ in ()).throw(TranscodeError("boom")))
    r = run(db_path, "transcode", "--originals", "trash")
    assert "Skipped" in r.output and "boom" in r.output
    assert sorted(p.name for p in old_lib.iterdir()) == ["03_20_04 020.mpg", "kids.wmv"]


# --------------------------------------------------------------------------- same name, different format

def test_output_names_avoid_collisions_between_formats(tmp_path, old_clips):
    d = tmp_path / "d"
    d.mkdir()
    for name, src in (("clip.mpg", "a.mpg"), ("clip.wmv", "b.wmv"), ("Mix.MPG", "a.mpg"), ("mix.wmv", "b.wmv"),
                      ("solo.mpg", "a.mpg"), ("other.mpg", "a.mpg"), ("other.mp4", "a.mpg")):
        shutil.copy(old_clips / src, d / name)

    assert transcode.output_path(d / "clip.mpg").name == "clip (mpg).mp4"
    assert transcode.output_path(d / "clip.wmv").name == "clip (wmv).mp4"
    assert transcode.output_path(d / "Mix.MPG").name == "Mix (mpg).mp4"      # names compare ignoring case
    assert transcode.output_path(d / "mix.wmv").name == "mix (wmv).mp4"
    assert transcode.output_path(d / "solo.mpg").name == "solo.mp4"          # nothing to clash with: plain name
    assert transcode.output_path(d / "other.mpg").name == "other.mp4"        # an .mp4 sibling isn't a source


def test_cli_converts_same_name_different_format_files_separately(tmp_path, old_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(old_clips / "a.mpg", lib / "tummy.mpg")
    shutil.copy(old_clips / "b.wmv", lib / "tummy.wmv")   # same clip, other encoding: near-identical length
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(lib)).exit_code == 0

    r = run(db_path, "transcode", "--preset", "ultrafast", "--originals", "keep")
    assert r.exit_code == 0, r.output
    assert "Converted 2 file(s)" in r.output
    assert sorted(p.name for p in lib.iterdir()) == ["tummy (mpg).mp4", "tummy (wmv).mp4", "tummy.mpg", "tummy.wmv"]
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM videos WHERE ext = 'mp4'").fetchone()[0] == 2

    # Each original now has its own output, so removing both originals loses nothing.
    r = run(db_path, "transcode", "--originals", "archive")
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in lib.iterdir() if p.is_file()) == ["tummy (mpg).mp4", "tummy (wmv).mp4"]
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2


def test_cataloging_the_same_path_twice_is_harmless(conn, tmp_path, clip_dir, thumbs):
    from vidcat import scanner
    f = tmp_path / "x.mp4"
    shutil.copy(clip_dir / "a.mp4", f)
    stats = scanner.scan_files(conn, [f, f, tmp_path / "." / "x.mp4"], thumbs)   # three spellings of one file
    assert stats.added == 1 and conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1


# --------------------------------------------------------------------------- AVI (DV and MPEG-4)

def test_avi_is_converted_by_default():
    assert "avi" in transcode.DEFAULT_EXTS


def test_dv_avi_keeps_its_widescreen_shape_and_is_deinterlaced(tmp_path, avi_clips, monkeypatch):
    src = tmp_path / "tape.avi"
    shutil.copy(avi_clips / "dv.avi", src)
    assert transcode.detect_interlaced(src) is True            # tape footage: fields are detected

    commands = []
    real_build = transcode.build_command
    monkeypatch.setattr(transcode, "build_command", lambda *a, **k: commands.append(real_build(*a, **k)) or commands[-1])
    dst, encoded = transcode.convert(src, FAST)
    assert encoded and "yadif" in " ".join(commands[0])        # deinterlacing was applied

    s, duration, _ = streams(dst)
    assert s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert (s["video"]["width"], s["video"]["height"]) == (720, 480)
    assert s["video"]["display_aspect_ratio"] == "16:9"        # not squashed to 4:3
    assert abs(duration - 1.0) < 0.3
    assert dst.stat().st_size < src.stat().st_size / 5         # DV is ~3.5 MB per second


def test_xvid_avi_is_left_progressive(tmp_path, avi_clips):
    src = tmp_path / "old.avi"
    shutil.copy(avi_clips / "xvid.avi", src)
    assert transcode.detect_interlaced(src) is False
    dst, _ = transcode.convert(src, FAST)
    s, _, _ = streams(dst)
    assert s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert s["video"]["display_aspect_ratio"] == "4:3"


def test_cli_picks_up_avi_files_without_asking(tmp_path, avi_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(avi_clips / "dv.avi", lib / "tape one.avi")
    shutil.copy(avi_clips / "xvid.avi", lib / "clip.AVI")
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(lib)).exit_code == 0
    r = run(db_path, "transcode", "--dry-run")
    assert "2 video(s) to convert" in r.output and "tape one.mp4" in r.output and "clip.mp4" in r.output

    r = run(db_path, "transcode", "--preset", "ultrafast", "--originals", "keep")
    assert r.exit_code == 0 and "Converted 2 file(s)" in r.output
    assert sorted(p.name for p in lib.iterdir()) == ["clip.AVI", "clip.mp4", "tape one.avi", "tape one.mp4"]


# --------------------------------------------------------------------------- QuickTime (.mov)

PLAYABLE_MOVS = ["playable.mov", "silent.mov"]
UNPLAYABLE_MOVS = ["mjpeg.mov", "pcm_audio.mov", "yuv422.mov"]


def test_mov_is_converted_by_default_when_needed():
    assert "mov" in transcode.DEFAULT_EXTS


@pytest.mark.parametrize("name, expected", [(n, True) for n in PLAYABLE_MOVS] + [(n, False) for n in UNPLAYABLE_MOVS])
def test_browser_compatibility_check(mov_clips, name, expected):
    assert transcode.browser_compatible(media.probe(mov_clips / name)) is expected


@pytest.mark.parametrize("name", UNPLAYABLE_MOVS)
def test_unplayable_mov_is_converted_to_something_browsers_play(tmp_path, mov_clips, name):
    src = tmp_path / name
    shutil.copy(mov_clips / name, src)
    dst, encoded = transcode.convert(src, FAST)
    assert encoded and dst.suffix == ".mp4"
    assert transcode.browser_compatible(media.probe(dst))          # the result is what browsers play
    s, duration, _ = streams(dst)
    assert s["video"]["codec_name"] == "h264" and s["video"]["pix_fmt"] == "yuv420p"
    assert s["audio"]["codec_name"] == "aac" and abs(duration - 1.0) < 0.3


@pytest.mark.parametrize("name", PLAYABLE_MOVS)
def test_playable_mov_is_left_alone_unless_forced(tmp_path, mov_clips, name):
    src = tmp_path / name
    shutil.copy(mov_clips / name, src)
    with pytest.raises(transcode.AlreadyPlayable):
        transcode.convert(src, FAST)
    assert not src.with_suffix(".mp4").exists()                    # nothing was written

    dst, encoded = transcode.convert(src, FAST, include_playable=True)
    assert encoded and dst.exists()


def test_the_playability_skip_only_applies_to_browser_containers(tmp_path, avi_clips, old_clips):
    """An H.264 .avi or a .wmv can't be played by a browser regardless of what's inside, so they always convert."""
    src = tmp_path / "x.avi"
    shutil.copy(avi_clips / "xvid.avi", src)
    assert transcode.convert(src, FAST)[1] is True
    assert transcode.convert(old_clips / "b.wmv", FAST)[1] is True
    (old_clips / "b.mp4").unlink(missing_ok=True)


def test_cli_converts_only_the_movs_that_need_it(tmp_path, mov_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in PLAYABLE_MOVS + UNPLAYABLE_MOVS:
        shutil.copy(mov_clips / name, lib / name)
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(lib)).exit_code == 0

    r = run(db_path, "transcode", "--dry-run")
    assert "5 video(s) to convert" in r.output
    assert r.output.count("would be skipped") == 2                # the two that already play

    r = run(db_path, "transcode", "--preset", "ultrafast", "--originals", "keep")
    assert r.exit_code == 0, r.output
    assert "Converted 3 file(s)" in r.output and "2 already play in browsers" in r.output
    assert sorted(p.name for p in lib.glob("*.mp4")) == ["mjpeg.mp4", "pcm_audio.mp4", "yuv422.mp4"]
    assert sorted(p.name for p in lib.glob("*.mov")) == sorted(PLAYABLE_MOVS + UNPLAYABLE_MOVS)  # all originals kept

    r = run(db_path, "transcode", "--preset", "ultrafast", "--originals", "keep", "--include-playable")
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in lib.glob("*.mp4")) == [
        "mjpeg.mp4", "pcm_audio.mp4", "playable.mp4", "silent.mp4", "yuv422.mp4"]


def test_cli_with_only_playable_movs_says_so_and_changes_nothing(tmp_path, mov_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(mov_clips / "playable.mov", lib / "playable.mov")
    db_path = tmp_path / "c.db"
    run(db_path, "scan", str(lib))
    r = run(db_path, "transcode", "--originals", "trash")
    assert r.exit_code == 0 and "Nothing converted, 1 already play in browsers" in r.output
    assert [p.name for p in lib.iterdir()] == ["playable.mov"]


# --------------------------------------------------------------------------- lossless copy when only part needs work

def frame_hashes(path):
    """Per-frame checksums of the decoded picture: identical iff the picture is bit-for-bit the same."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [line.split(",")[-1].strip() for line in out.splitlines() if line and not line.startswith("#")]


def spy_commands(monkeypatch):
    commands = []
    real = transcode.build_command
    monkeypatch.setattr(transcode, "build_command", lambda *a, **k: commands.append(real(*a, **k)) or commands[-1])
    return commands


def test_mov_with_good_picture_but_pcm_audio_keeps_its_picture_untouched(tmp_path, mov_clips, monkeypatch):
    src = tmp_path / "pcm_audio.mov"
    shutil.copy(mov_clips / "pcm_audio.mov", src)
    commands = spy_commands(monkeypatch)
    dst, encoded = transcode.convert(src, FAST)

    assert encoded and "copy" in commands[0] and "libx264" not in commands[0]
    assert frame_hashes(dst) == frame_hashes(src)                   # the picture is bit-for-bit the original
    s, duration, _ = streams(dst)
    assert s["audio"]["codec_name"] == "aac" and abs(duration - 1.0) < 0.3   # only the audio was converted
    assert transcode.browser_compatible(media.probe(dst))


def test_pictures_that_need_work_are_still_reencoded(tmp_path, mov_clips, monkeypatch):
    for name in ("mjpeg.mov", "yuv422.mov"):
        src = tmp_path / name
        shutil.copy(mov_clips / name, src)
        commands = spy_commands(monkeypatch)
        transcode.convert(src, FAST)
        assert "libx264" in commands[-1] and "copy" not in commands[-1][commands[-1].index("-c:v") + 1]


def test_interlaced_picture_is_never_copied(tmp_path, interlaced_mov, monkeypatch):
    src = tmp_path / "interlaced.mov"
    shutil.copy(interlaced_mov / "interlaced.mov", src)
    commands = spy_commands(monkeypatch)
    dst, _ = transcode.convert(src, FAST)
    joined = " ".join(commands[0])
    assert "yadif" in joined and "libx264" in joined                 # deinterlaced, so it had to be re-encoded
    assert frame_hashes(dst) != frame_hashes(src)


def test_hevc_request_never_copies_h264(tmp_path, mov_clips, monkeypatch):
    src = tmp_path / "pcm_audio.mov"
    shutil.copy(mov_clips / "pcm_audio.mov", src)
    commands = spy_commands(monkeypatch)
    dst, _ = transcode.convert(src, Settings(codec="hevc", preset="ultrafast"))
    assert "libx265" in commands[0]
    assert streams(dst)[0]["video"]["codec_name"] == "hevc"


def test_forcing_a_playable_mov_is_a_lossless_rewrap(tmp_path, mov_clips, monkeypatch):
    src = tmp_path / "playable.mov"
    shutil.copy(mov_clips / "playable.mov", src)
    commands = spy_commands(monkeypatch)
    dst, _ = transcode.convert(src, FAST, include_playable=True)
    cmd = commands[0]
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "copy"
    assert frame_hashes(dst) == frame_hashes(src)
    assert streams(dst)[0]["audio"]["codec_name"] == "aac"


# --------------------------------------------------------------------------- M4V and 3GP

def test_m4v_and_3gp_are_converted_by_default():
    assert "m4v" in transcode.DEFAULT_EXTS and "3gp" in transcode.DEFAULT_EXTS


@pytest.mark.parametrize("name", ["h263.3gp", "mpeg4.3gp", "h264.3gp"])
def test_3gp_is_always_converted_even_when_the_codecs_are_fine(tmp_path, phone_clips, name):
    src = tmp_path / name
    shutil.copy(phone_clips / name, src)
    dst, encoded = transcode.convert(src, FAST)          # never AlreadyPlayable: browsers can't open .3gp
    s, duration, fmt = streams(dst)
    assert encoded and dst.suffix == ".mp4"
    assert s["video"]["codec_name"] == "h264" and s["video"]["pix_fmt"] == "yuv420p" and s["audio"]["codec_name"] == "aac"
    assert abs(duration - 2.0) < 0.4 and transcode.browser_compatible(media.probe(dst))


def test_m4v_that_already_plays_is_left_alone(tmp_path, phone_clips):
    src = tmp_path / "playable.m4v"
    shutil.copy(phone_clips / "playable.m4v", src)
    with pytest.raises(transcode.AlreadyPlayable):
        transcode.convert(src, FAST)
    assert [p.name for p in tmp_path.iterdir()] == ["playable.m4v"]


def test_m4v_with_ac3_audio_keeps_its_picture_and_fixes_the_audio(tmp_path, phone_clips, monkeypatch):
    src = tmp_path / "ac3.m4v"
    shutil.copy(phone_clips / "ac3.m4v", src)
    commands = spy_commands(monkeypatch)
    dst, encoded = transcode.convert(src, FAST)
    assert encoded and "libx264" not in commands[0]
    assert frame_hashes(dst) == frame_hashes(src)                       # picture untouched
    assert streams(dst)[0]["audio"]["codec_name"] == "aac"              # AC-3 isn't playable everywhere


def test_a_truncated_m4v_is_reported_not_crashed_on(tmp_path, phone_clips):
    src = tmp_path / "truncated.m4v"
    shutil.copy(phone_clips / "truncated.m4v", src)
    with pytest.raises(TranscodeError, match="can't read"):
        transcode.convert(src, FAST)
    assert [p.name for p in tmp_path.iterdir()] == ["truncated.m4v"]    # nothing written, nothing left behind


def test_cli_handles_a_mixed_folder_of_phone_and_m4v_files(tmp_path, phone_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("mpeg4.3gp", "h264.3gp", "playable.m4v", "ac3.m4v", "truncated.m4v"):
        shutil.copy(phone_clips / name, lib / name)
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(lib)).exit_code == 0

    r = run(db_path, "transcode", "--dry-run")
    assert "5 video(s) to convert" in r.output and r.output.count("would be skipped") == 1

    r = run(db_path, "transcode", "--preset", "ultrafast", "--originals", "keep")
    assert r.exit_code == 0, r.output
    assert "Converted 3 file(s)" in r.output and "1 skipped" in r.output and "1 already play in browsers" in r.output
    assert sorted(p.name for p in lib.glob("*.mp4")) == ["ac3.mp4", "h264.mp4", "mpeg4.mp4"]
    assert sorted(p.name for p in lib.glob("*.m4v")) == ["ac3.m4v", "playable.m4v", "truncated.m4v"]   # originals kept
