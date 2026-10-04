"""Thin wrappers around ffprobe/ffmpeg, plus capture-date detection."""
import json
import re
import shutil
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path


class MediaToolError(RuntimeError):
    pass


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise MediaToolError(f"{', '.join(missing)} not found on PATH. Install with: brew install ffmpeg")


def probe(path: Path | str) -> dict | None:
    """Run ffprobe. Returns parsed JSON, or None if the file couldn't be probed."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=60, check=True, stdin=subprocess.DEVNULL,
        )
        return json.loads(out.stdout)
    except FileNotFoundError as e:
        raise MediaToolError("ffprobe not found on PATH. Install with: brew install ffmpeg") from e
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _parse_date(value: str) -> int | None:
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    ts = int(dt.timestamp())  # naive values are interpreted as local time
    # Cameras with unset clocks produce 1970/2000-ish dates; reject obviously bogus ones.
    if dt.year < 1995 or ts > time.time() + 86400:
        return None
    return ts


_NAME_DATE_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
# 02_19_04, 2-19-2004, 02.19.04: separators are required so bare digit runs aren't misread as dates.
_NAME_DATE_MDY = re.compile(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](\d{4}|\d{2})(?!\d)")


def _valid_name_date(year: int, month: int, day: int) -> int | None:
    try:
        dt = datetime(year, month, day)
    except ValueError:
        return None
    return int(dt.timestamp()) if 1950 <= year and dt.timestamp() <= time.time() + 86400 else None


def date_from_filename(name: str) -> int | None:
    """Date embedded in a file name: YYYYMMDD / YYYY-MM-DD, or month-first M_D_YY / M_D_YYYY (US style).

    The day-first reading is used only when month-first is impossible (e.g. 25_12_04).
    """
    if m := _NAME_DATE_YMD.search(name):
        if ts := _valid_name_date(int(m[1]), int(m[2]), int(m[3])):
            return ts
    for m in _NAME_DATE_MDY.finditer(name):
        a, b, y = int(m[1]), int(m[2]), m[3]
        month, day = (a, b) if a <= 12 else (b, a)
        if len(y) == 2:
            year = 2000 + int(y) if int(y) <= datetime.now().year % 100 else 1900 + int(y)
        else:
            year = int(y)
        if ts := _valid_name_date(year, month, day):
            return ts
    return None


# ISO 6709 in decimal degrees, as phones write it: "+37.6498-121.7799+153.234/" (altitude optional).
_ISO6709 = re.compile(r"^\s*([+-]\d{1,2}(?:\.\d+)?)([+-]\d{1,3}(?:\.\d+)?)")


def parse_iso6709(value: str) -> tuple[float, float] | None:
    """(latitude, longitude) from an ISO 6709 location string, or None if it isn't a usable position."""
    m = _ISO6709.match(value or "")
    if not m:
        return None
    return _position(float(m[1]), float(m[2]))


def _position(lat: float, lon: float) -> tuple[float, float] | None:
    # Some cameras write 0,0 when they had no GPS fix.
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def location_tag(data: dict | None) -> str | None:
    """The raw ISO 6709 location recorded in the file: iPhones use the QuickTime key, Android phones and
    ffmpeg the MP4 `location` (©xyz) tag."""
    if not data:
        return None
    sources = [data.get("format", {}).get("tags", {})] + [s.get("tags", {}) for s in data.get("streams", [])]
    for tags in sources:
        tags = {k.lower(): v for k, v in tags.items()}
        for key in ("com.apple.quicktime.location.iso6709", "location", "location-eng"):
            if parse_iso6709(tags.get(key, "")):
                return tags[key]
    return None


# GoPro telemetry (GPMF) value types we need, by their type character.
_GPMF_TYPES = {"l": ">i", "L": ">I", "s": ">h", "S": ">H"}
_GOOD_DOP = 500   # GPSP is dilution of precision x 100; under 5 is a decent fix


def parse_gpmf(data: bytes) -> tuple[float, float] | None:
    """First good GPS position in GoPro telemetry (the `gpmd` track): GPS5 (HERO5-HERO10) or GPS9 (HERO11+).

    Positions taken before the camera had a 2D/3D fix are skipped, and a precise one is preferred over the
    first rough one.
    """
    rough = []

    def values(typ: str, body: bytes) -> list[int]:
        fmt = _GPMF_TYPES.get(typ)
        return [v for (v,) in struct.iter_unpack(fmt, body)] if fmt and len(body) % struct.calcsize(fmt) == 0 else []

    def walk(buf: bytes) -> tuple[float, float] | None:
        scal, fix, dop = [1], None, None  # these describe the samples that follow them in the same stream
        i = 0
        while i + 8 <= len(buf):
            key, typ, size, repeat = buf[i:i + 4], chr(buf[i + 4]), buf[i + 5], struct.unpack(">H", buf[i + 6:i + 8])[0]
            body = buf[i + 8:i + 8 + size * repeat]
            i += 8 + ((size * repeat + 3) & ~3)  # entries are padded to 4 bytes
            if typ == "\0":  # nested (DEVC, STRM)
                if found := walk(body):
                    return found
            elif key == b"SCAL":
                scal = values(typ, body) or [1]
            elif key == b"GPSF":
                fix = (values(typ, body) or [None])[0]
            elif key == b"GPSP":
                dop = (values(typ, body) or [None])[0]
            elif key in (b"GPS5", b"GPS9") and size >= 8:
                for j in range(0, len(body) - size + 1, size):
                    sample = body[j:j + size]
                    lat, lon = struct.unpack(">ii", sample[:8])
                    if key == b"GPS9" and size >= 32:  # each sample carries its own DOP and fix
                        dop, fix = struct.unpack(">HH", sample[28:32])
                    if not fix or fix < 2 or 0 in scal[:2]:
                        continue
                    pos = _position(lat / scal[0], lon / (scal[1] if len(scal) > 1 else scal[0]))
                    if pos and dop is not None and dop < _GOOD_DOP:
                        return pos
                    if pos:
                        rough.append(pos)
        return None

    return walk(data) or (rough[0] if rough else None)


def gopro_location(path: Path | str, data: dict | None, seconds: int = 60) -> tuple[float, float] | None:
    """GPS position from a GoPro's telemetry track, if the file has one and the camera had a fix.

    Only the first `seconds` of telemetry are read: reading all of it means reading the whole file, which takes
    minutes for a large file on a network share.
    """
    stream = next((s for s in (data or {}).get("streams", []) if s.get("codec_tag_string") == "gpmd"), None)
    if stream is None:
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:{stream['index']}", "-c", "copy",
             "-frames:d", str(seconds), "-f", "data", "-"],
            capture_output=True, timeout=60, check=True, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
    except subprocess.SubprocessError:
        return None
    return parse_gpmf(out.stdout)


def parse_probe(data: dict | None, name: str, mtime: float) -> dict:
    """Extract catalog fields from ffprobe output (or defaults if probing failed)."""
    info = {"duration": None, "width": None, "height": None, "codec": None,
            "created_at": None, "date_source": None, "has_video": None, "latitude": None, "longitude": None}
    if data:
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        info["has_video"] = video is not None
        if video:
            info["width"] = video.get("width")
            info["height"] = video.get("height")
            info["codec"] = video.get("codec_name")
        fmt = data.get("format", {})
        try:
            info["duration"] = float(fmt["duration"])
        except (KeyError, TypeError, ValueError):
            pass
        tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
        candidates = [tags.get("com.apple.quicktime.creationdate"), tags.get("creation_time")]
        candidates += [s.get("tags", {}).get("creation_time") for s in streams]
        for c in candidates:
            if c and (ts := _parse_date(c)):
                info["created_at"], info["date_source"] = ts, "metadata"
                break
        if loc := location_tag(data):
            info["latitude"], info["longitude"] = parse_iso6709(loc)
    if info["created_at"] is None:
        if ts := date_from_filename(name):
            info["created_at"], info["date_source"] = ts, "filename"
        else:  # last resort; often just the day the file was copied
            info["created_at"], info["date_source"] = int(mtime), "mtime"
    return info


def make_thumbnail(path: Path | str, dest: Path, duration: float | None, width: int = 320) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for t in ((duration or 0) * 0.1, 0):
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(dest)],
                capture_output=True, timeout=60, check=True, stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
        except subprocess.SubprocessError:
            continue
        if dest.exists() and dest.stat().st_size > 0:
            return True
    return False


def extract_frames(path: Path | str, duration: float | None, count: int = 4, width: int = 512) -> list[bytes]:
    """Grab `count` evenly spaced JPEG frames."""
    frames = []
    for i in range(1, count + 1):
        t = (duration or 0) * i / (count + 1)
        try:
            out = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                capture_output=True, timeout=60, check=True, stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
        except subprocess.SubprocessError:
            continue
        if out.stdout:
            frames.append(out.stdout)
    return frames
