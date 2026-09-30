"""Filename quality scoring, name suggestions, and safe renaming."""
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import config

# Words that carry no information about what a video is about.
GENERIC_TOKENS = {
    "video", "videos", "vid", "img", "image", "images", "mov", "movie", "movies", "clip", "clips",
    "dsc", "dscn", "dscf", "mvi", "mah", "gopr", "gopro", "pxl", "sam", "hvc", "vts",
    "pict", "cimg", "hpim", "imag", "sany", "sdc", "mvc", "dji",
    "untitled", "new", "copy", "final", "export", "exported", "output", "recording", "record",
    "capture", "screen", "screenshot", "file", "footage", "sample", "test", "temp", "tmp",
    "camera", "cam", "camcorder", "mp4", "mts", "avi", "mpg", "mpeg", "wmv", "mkv", "m4v", "m2ts",
    "imovie", "project", "raw", "original", "orig", "edit", "edited", "render", "rendered",
    "trim", "trimmed", "mmexport", "wechat", "whatsapp", "snapchat", "iphone", "android",
    "canon", "sony", "nikon", "panasonic", "samsung", "dcim", "misc", "stuff", "other",
}

# Folder names that don't describe content (used when suggesting a name from the parent folder).
GENERIC_FOLDERS = {
    "videos", "video", "movies", "movie", "dcim", "camera", "camera uploads", "downloads",
    "desktop", "documents", "users", "home", "volumes", "tmp", "temp", "misc", "new folder",
    "untitled folder", "backup", "backups", "archive", "iphone", "photos", "pictures", "clips",
    "private", "avchd", "stream", "bdmv", "mp_root", "prgxx", "misc", "originals", "masters",
}

# A folder made up only of these words tells you nothing about the videos in it ("Home Videos", "My Family Movies").
GENERIC_FOLDER_WORDS = (
    {w for f in GENERIC_FOLDERS for w in re.findall(r"[^\W\d_]+", f)} | GENERIC_TOKENS
    | {"home", "family", "my", "our", "all", "old", "archive", "archives", "movies", "films", "tape", "tapes", "digital"}
)

_HEX_ID = re.compile(r"^[0-9a-f]{8}(?:[-_]?[0-9a-f]{4}){3}[-_]?[0-9a-f]{12}$")
_LONG_HEX = re.compile(r"^(?=.*\d)[0-9a-f]{12,}$")
_CAMERA_DIR = re.compile(r"^\d{3}[a-z_]{3,}$|^\d{6,8}_?\d{0,4}$|^mp_root$|^\d{4}$")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def name_quality(stem: str) -> int:
    """Score 0-100 for how informative a file name (without extension) is. < BAD_NAME_THRESHOLD is 'bad'."""
    s = stem.strip().lower()
    if not s:
        return 0
    compact = re.sub(r"[\s_.-]", "", s)
    if _HEX_ID.match(s) or _LONG_HEX.match(compact):
        return 5
    tokens = re.findall(r"[^\W\d_]+", s)  # runs of letters (any script)
    meaningful = [t for t in tokens if t not in GENERIC_TOKENS and len(t) >= (3 if t.isascii() else 1)]
    if not meaningful:
        return 10
    score = 60 if len(meaningful) == 1 else 90
    if _YEAR.search(s):
        score += 10
    return min(score, 100)


def is_bad_name(stem: str) -> bool:
    return name_quality(stem) < config.BAD_NAME_THRESHOLD


def sanitize_stem(text: str, max_len: int = 150) -> str:
    """Make text safe to use as a file name on macOS and on portable (exFAT/NTFS/NAS) volumes."""
    text = _BAD_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:max_len].rstrip(" .")


def is_generic_folder(name: str) -> bool:
    """True for folder names that say nothing about their contents: hidden, camera-card or year folders, and
    names made only of filler words such as "Home Videos"."""
    lower = name.lower()
    if name.startswith(".") or lower in GENERIC_FOLDERS or _CAMERA_DIR.match(lower):
        return True
    words = re.findall(r"[^\W\d_]+", lower)
    return all(w in GENERIC_FOLDER_WORDS for w in words)  # also true when there are no letters at all


def _folder_label(path: Path, home: Path | None = None) -> str | None:
    home = home or Path.home()
    for parent in list(path.parents)[:3]:
        name = parent.name
        # Stop at the top: the home folder, the filesystem root, or a volume root such as /Volumes/Memories,
        # whose name (a drive or share label) says nothing about the video.
        if not name or parent == home or parent.parent == Path("/Volumes"):
            break
        if is_generic_folder(name):
            continue  # keep looking further up for something meaningful
        if name_quality(name) >= config.BAD_NAME_THRESHOLD:
            return name
    return None


def suggest_name(video, caption: str | None = None) -> str:
    """Suggest a file name stem like '2019-07-04 Beach Trip - Kids Building Sandcastles'.

    `video` is a mapping/row with `path`, `created_at` and (optionally) `date_source`. Uses the capture
    date, a meaningful parent folder, and (if provided) an AI caption. A date that is only the file's
    modified time (often just the day it was copied) is left out when there's something better to say.
    """
    path = Path(video["path"])
    when = datetime.fromtimestamp(video["created_at"])
    try:
        source = video["date_source"]
    except (KeyError, IndexError):
        source = "metadata"
    folder = _folder_label(path)
    caption = sanitize_stem(caption) if caption else ""

    parts = []
    if source != "mtime" or not (folder or caption):
        parts.append(when.strftime("%Y-%m-%d"))
    if folder:
        parts.append(folder)
    base = " ".join(parts)
    if caption:
        # A dash separates a folder name from the caption ("Beach Trip - Kids Building Sandcastles"), but a
        # bare date reads better with just a space ("2019-07-04 Kids Building Sandcastles"), not "date - caption".
        if not base:
            base = caption
        elif folder:
            base = f"{base} - {caption}"
        else:
            base = f"{base} {caption}"
    elif not folder and source != "filename":
        base += when.strftime(" %H-%M-%S")  # nothing descriptive: keep names unique by time
    return sanitize_stem(base)


def _strip_ext(new_name: str, ext: str) -> str:
    if ext and new_name.lower().endswith(ext.lower()):
        return new_name[: -len(ext)]
    return new_name


def rename_video(conn: sqlite3.Connection, video_id: int, new_name: str) -> str:
    """Rename a video on disk (same directory, same extension) and update the catalog.

    Returns the new path. Raises ValueError for unusable names and FileNotFoundError/OSError for
    filesystem problems. Existing files are never overwritten; a ' (2)' suffix is added instead.
    """
    row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if row is None:
        raise ValueError(f"No video with id {video_id}")
    old = Path(row["path"])
    if not old.exists():
        raise FileNotFoundError(f"File no longer exists: {old}")

    stem = sanitize_stem(_strip_ext(new_name.strip(), old.suffix))
    if not stem:
        raise ValueError("New name is empty after removing unsupported characters")

    candidate = old.with_name(stem + old.suffix)
    n = 2
    while candidate.exists() and not old.samefile(candidate):  # samefile: case-only rename on APFS
        candidate = old.with_name(f"{stem} ({n}){old.suffix}")
        n += 1
    if str(candidate) == str(old):
        return str(old)

    os.rename(old, candidate)
    with conn:
        conn.execute(
            "UPDATE videos SET path = ?, name = ?, name_score = ? WHERE id = ?",
            (str(candidate), candidate.name, name_quality(candidate.stem), video_id),
        )
        conn.execute(
            "INSERT INTO rename_history (video_id, old_path, new_path, at) VALUES (?, ?, ?, ?)",
            (video_id, str(old), str(candidate), int(time.time())),
        )
    return str(candidate)


def undo_last_rename(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """Revert the most recent rename. Returns (current_path, restored_path) or None."""
    h = conn.execute("SELECT * FROM rename_history ORDER BY id DESC LIMIT 1").fetchone()
    if h is None:
        return None
    new, old = Path(h["new_path"]), Path(h["old_path"])
    if not new.exists():
        raise FileNotFoundError(f"Renamed file no longer exists: {new}")
    if old.exists() and not old.samefile(new):
        raise FileExistsError(f"Cannot restore: {old} already exists")
    os.rename(new, old)
    with conn:
        conn.execute(
            "UPDATE videos SET path = ?, name = ?, name_score = ? WHERE id = ?",
            (str(old), old.name, name_quality(old.stem), h["video_id"]),
        )
        conn.execute("DELETE FROM rename_history WHERE id = ?", (h["id"],))
    return str(new), str(old)
