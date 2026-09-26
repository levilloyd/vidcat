"""Copy a set of videos into a folder the user picks: the web UI's "Save all…".

Copies run in a background thread so the page can show progress and cancel. Nothing is ever overwritten
(a clashing name gets " (2)"), each file is written under a hidden temporary name and only renamed once it
is complete, so an interrupted save never leaves a half-copied video that looks finished, and the originals
are only read.
"""
import errno
import itertools
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

CHUNK = 8 * 1024 * 1024


class ExportError(Exception):
    """The save can't start (no room, folder not writable, …); the message is shown to the user."""


def choose_folder(prompt: str) -> str | None:
    """Ask for a destination folder with the macOS folder picker. Returns its path, or None if cancelled."""
    script = ["-e", "activate", "-e", f"POSIX path of (choose folder with prompt {_applescript_str(prompt)})"]
    try:
        r = subprocess.run(["osascript", *script], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise ExportError("Choosing a folder needs macOS (osascript wasn't found).") from e
    if r.returncode != 0:
        if "-128" in r.stderr:  # "User canceled."
            return None
        raise ExportError(f"Couldn't show the folder picker: {r.stderr.strip() or 'unknown error'}")
    return r.stdout.strip() or None


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def free_name(folder: Path, name: str, taken: set[str]) -> Path:
    """`folder/name`, or `name (2)`, `name (3)`… if that exists or another file in this save claimed it."""
    stem, ext = os.path.splitext(name)
    for n in itertools.count(1):
        candidate = name if n == 1 else f"{stem} ({n}){ext}"
        if candidate.casefold() not in taken and not (folder / candidate).exists():
            taken.add(candidate.casefold())
            return folder / candidate
    raise AssertionError("unreachable")


@dataclass
class ExportJob:
    dest: Path
    files: list[tuple[str, str, int]]  # (source path, file name, size)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "running"             # running | done | cancelled | failed
    copied: int = 0
    done_bytes: int = 0
    current: str | None = None
    skipped: list[dict] = field(default_factory=list)  # {"name", "reason"}
    error: str | None = None
    started: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def total_bytes(self) -> int:
        return sum(size for _, _, size in self.files)

    def snapshot(self) -> dict:
        return {
            "id": self.id, "state": self.state, "dest": str(self.dest), "total": len(self.files),
            "copied": self.copied, "total_bytes": self.total_bytes, "done_bytes": self.done_bytes,
            "current": self.current, "skipped": self.skipped, "error": self.error,
        }


def start(dest: str | Path, files: list[tuple[str, str, int]]) -> ExportJob:
    """Check the destination and start copying in a background thread."""
    dest = Path(dest)
    if not dest.is_dir():
        raise ExportError(f"{dest} isn't a folder.")
    if not os.access(dest, os.W_OK):
        raise ExportError(f"Can't write to {dest}.")
    job = ExportJob(dest=dest, files=files)
    free = shutil.disk_usage(dest).free
    if job.total_bytes > free:
        raise ExportError(f"Not enough space in {dest}: the videos need {human_size(job.total_bytes)}, "
                          f"and only {human_size(free)} is free.")
    threading.Thread(target=run, args=(job,), daemon=True, name=f"export-{job.id}").start()
    return job


def run(job: ExportJob) -> None:
    taken: set[str] = set()
    try:
        for src, name, size in job.files:
            if job.cancel_event.is_set():
                break
            job.current = name
            if not os.path.exists(src):
                job.skipped.append({"name": name, "reason": "file is missing"})
                job.done_bytes += size
                continue
            dst = free_name(job.dest, name, taken)
            before = job.done_bytes
            try:
                if _copy_one(job, src, dst):
                    job.copied += 1
            except OSError as e:
                if e.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise
                job.skipped.append({"name": name, "reason": e.strerror or str(e)})
                job.done_bytes = before + size
        job.state = "cancelled" if job.cancel_event.is_set() else "done"
    except OSError as e:
        job.state, job.error = "failed", f"Stopped: {e.strerror or e}"
    except Exception as e:  # never leave the page polling a job that silently died
        job.state, job.error = "failed", f"Stopped: {e}"
    finally:
        job.current = None


def _copy_one(job: ExportJob, src: str, dst: Path) -> bool:
    """Copy src to dst via a hidden temp file. Returns False if cancelled part-way (nothing is left behind)."""
    tmp = dst.with_name(f".{dst.name}.vidcat-part")
    start_bytes = job.done_bytes
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while chunk := fin.read(CHUNK):
                if job.cancel_event.is_set():
                    job.done_bytes = start_bytes
                    return False
                fout.write(chunk)
                job.done_bytes += len(chunk)
        shutil.copystat(src, tmp)  # keep the modified date, which is often the only capture date
        if dst.exists():  # appeared while copying; never overwrite
            raise FileExistsError(errno.EEXIST, f"{dst.name} appeared while copying")
        os.replace(tmp, dst)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def human_size(n: float) -> str:
    """Like cli.human_size (same format), for use without importing the CLI."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")
