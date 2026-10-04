"""Convert old camcorder-era formats (MPEG-1/2, WMV, ASF, AVI incl. DV, QuickTime, M4V, 3GP) to modern MP4."""
import os
import re
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, media

DEFAULT_EXTS = ("mpg", "mpeg", "mpe", "wmv", "asf", "avi", "mov", "m4v", "3gp")
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
CODECS = {
    # name: (ffmpeg encoder, default CRF, extra args)
    "h264": ("libx264", 20, []),
    "hevc": ("libx265", 24, ["-tag:v", "hvc1", "-x265-params", "log-level=error"]),  # hvc1 tag: needed by Apple players
}


# Containers browsers open natively, so a file in one only needs converting if what's *inside* won't play.
BROWSER_CONTAINERS = (".mov", ".m4v")
BROWSER_PIX_FMTS = ("yuv420p", "yuvj420p")   # 8-bit 4:2:0; browsers can't show 10-bit or 4:2:2 H.264
BROWSER_AUDIO = ("aac", "mp3")


class TranscodeError(RuntimeError):
    pass


class AlreadyPlayable(TranscodeError):
    """The file is already in a form browsers can play; there's nothing to convert."""


@dataclass
class Settings:
    codec: str = "h264"          # h264 | hevc
    crf: int | None = None       # None = codec default
    preset: str = "medium"
    deinterlace: str = "auto"    # auto | always | never


_IDET = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)")


def detect_interlaced(src: Path | str, frames: int = 300) -> bool:
    """Ask ffmpeg's idet filter whether most of the first frames are interlaced (typical for camcorder tapes)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(src), "-vf", "idet",
             "-frames:v", str(frames), "-an", "-f", "null", "-"],
            capture_output=True, text=True, errors="replace", timeout=180, stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    # ffmpeg prints the summary more than once (an empty one from a probing pass comes first); the last is real.
    matches = _IDET.findall(out.stderr)
    if not matches:
        return False
    tff, bff, prog = map(int, matches[-1])
    total = tff + bff + prog
    return total > 0 and (tff + bff) / total > 0.5


def build_command(
    src: Path, dst: Path, settings: Settings, deinterlace: bool, creation_time: int | None,
    copy_video: bool = False, copy_audio: bool = False, location: str | None = None,
) -> list[str]:
    """ffmpeg command line. `copy_video` / `copy_audio` copy that part untouched (lossless) instead of encoding it.
    `location` is an ISO 6709 string to record as where the video was filmed."""
    encoder, default_crf, extra = CODECS[settings.codec]
    filters = ["yadif=mode=0:parity=-1:deint=0"] if deinterlace else []
    # yuv420p needs even dimensions; out_range=tv makes full-range sources (e.g. MJPEG) standard limited-range
    # H.264 instead of the less compatible yuvj420p.
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2:out_range=tv")
    video_args = ["-c:v", "copy"] if copy_video else [
        "-vf", ",".join(filters),
        "-c:v", encoder, "-preset", settings.preset, "-crf", str(settings.crf or default_crf),
        "-pix_fmt", "yuv420p", *extra,
    ]
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        *video_args,
        *(["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "160k"]),
        "-map_metadata", "0", "-movflags", "+faststart",
    ]
    if creation_time:
        stamp = datetime.fromtimestamp(creation_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        cmd += ["-metadata", f"creation_time={stamp}"]
    if location:  # -map_metadata doesn't carry an iPhone's QuickTime location key into an MP4
        cmd += ["-metadata", f"location={location}"]
    return cmd + ["-progress", "pipe:1", "-nostats", str(dst)]


def encode(cmd: list[str], duration: float | None, on_progress: Callable[[float], None] | None = None) -> None:
    """Run ffmpeg, reporting progress as a 0..1 fraction. Raises TranscodeError with ffmpeg's message on failure."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except FileNotFoundError as e:
        raise media.MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
    tail: deque[str] = deque(maxlen=15)
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                value = line.split("=", 1)[1]
                if on_progress and duration and value.isdigit():
                    on_progress(min(int(value) / 1e6 / duration, 1.0))
            elif line and not re.match(r"^[a-z_0-9]+=", line):
                tail.append(line)  # anything that isn't a progress key=value is an error message
        code = proc.wait()
    except BaseException:  # includes Ctrl+C: don't leave ffmpeg running
        proc.kill()
        proc.wait()
        raise
    if code != 0:
        raise TranscodeError("\n".join(tail) or f"ffmpeg exited with status {code}")


def verify_output(path: Path, src_duration: float | None, need_audio: bool) -> str | None:
    """Sanity-check a converted file. Returns a problem description, or None if it looks right."""
    data = media.probe(path)
    if not data:
        return "the output can't be read"
    info = media.parse_probe(data, path.name, 0)
    if not info["has_video"]:
        return "the output has no video"
    if need_audio and not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        return "the audio track is missing from the output"
    duration = info["duration"]
    if not duration:
        return "the output has no length"
    if src_duration and abs(duration - src_duration) > max(1.0, 0.03 * src_duration):
        return f"length differs (original {src_duration:.1f}s, output {duration:.1f}s)"
    return None


def browser_video(data: dict) -> bool:
    """True if the first video stream is H.264 in 8-bit 4:2:0, the one picture format every browser plays."""
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    return bool(video) and video.get("codec_name") == "h264" and video.get("pix_fmt") in BROWSER_PIX_FMTS


def browser_compatible(data: dict) -> bool:
    """True if a file's streams are what every browser plays: browser_video plus AAC/MP3 audio (or none)."""
    return browser_video(data) and all(
        s.get("codec_name") in BROWSER_AUDIO for s in data.get("streams", []) if s.get("codec_type") == "audio"
    )


def output_path(src: Path) -> Path:
    """Where `src` is converted to: `name.mp4`, or `name (ext).mp4` when another video in the same folder has
    the same name in a different format (say clip.mpg and clip.wmv). Otherwise the two would fight over one
    output, and the second would be mistaken for an "already converted" copy of the first."""
    stem = src.stem.lower()
    try:
        clash = any(
            p.name != src.name and p.stem.lower() == stem
            and p.suffix.lower() in config.VIDEO_EXTS and p.suffix.lower() != ".mp4"
            for p in src.parent.iterdir()
        )
    except OSError:
        clash = False
    if clash:
        return src.with_name(f"{src.stem} ({src.suffix.lstrip('.').lower()}).mp4")
    return src.with_suffix(".mp4")


def convert(
    src: Path,
    settings: Settings,
    *,
    creation_time: int | None = None,
    on_progress: Callable[[float], None] | None = None,
    include_playable: bool = False,
) -> tuple[Path, bool]:
    """Convert `src` to a sibling .mp4 next to it. Returns (output_path, encoded).

    `encoded` is False when a matching .mp4 already exists (e.g. from an earlier run) and looks like a
    correct conversion; it is then reused as-is. The output is written to a hidden temp file, verified,
    and only then moved into place, so a failed or interrupted run never leaves a half-written .mp4.
    The original is never touched. `creation_time` and any recorded location are stored in the output's metadata, and the output's
    modified time is set to the original's. A .mov/.m4v that browsers can already play raises
    `AlreadyPlayable` (re-encoding it would only lose quality) unless `include_playable` is set.
    """
    src_data = media.probe(src)
    if not src_data:
        raise TranscodeError("ffprobe can't read the original")
    src_info = media.parse_probe(src_data, src.name, 0)
    if not src_info["has_video"]:
        raise TranscodeError("the original has no video stream")
    if not include_playable and src.suffix.lower() in BROWSER_CONTAINERS and browser_compatible(src_data):
        raise AlreadyPlayable("already plays in browsers")
    need_audio = any(s.get("codec_type") == "audio" for s in src_data.get("streams", []))
    duration = src_info["duration"]

    dst = output_path(src)
    if dst.exists():
        problem = verify_output(dst, duration, need_audio)
        if problem:
            raise TranscodeError(f"{dst.name} already exists but doesn't match this file ({problem}); left alone")
        return dst, False

    tmp = dst.with_name(f".{dst.stem}.vidcat-tmp.mp4")  # hidden, so scans ignore it
    tmp.unlink(missing_ok=True)
    try:
        if settings.deinterlace == "always":
            deinterlace = True
        elif settings.deinterlace == "never":
            deinterlace = False
        else:
            deinterlace = detect_interlaced(src)
        # A QuickTime file whose picture is already browser-ready (only the audio, say PCM, was the problem) keeps
        # its picture bit-for-bit: no generation loss, and far faster. Never when it needs deinterlacing.
        copy_video = (src.suffix.lower() in BROWSER_CONTAINERS and browser_video(src_data)
                      and not deinterlace and settings.codec == "h264")
        copy_audio = copy_video and all(
            s.get("codec_name") in BROWSER_AUDIO for s in src_data.get("streams", []) if s.get("codec_type") == "audio")
        cmd = build_command(src, tmp, settings, deinterlace, creation_time, copy_video, copy_audio,
                            location=media.location_tag(src_data))
        encode(cmd, duration, on_progress)
        problem = verify_output(tmp, duration, need_audio)
        if problem:
            raise TranscodeError(f"verification failed: {problem}")
        st = src.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime))
        if dst.exists():  # appeared while we were encoding; never overwrite
            raise TranscodeError(f"{dst.name} appeared during conversion; left alone")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst, True
