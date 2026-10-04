"""Command-line interface: scan, dupes, names, places, tag, ls, serve."""
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeRemainingColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import (
    config, disposal, duplicates, media, names as names_mod, places as places_mod, queries, scanner, tags as tags_mod,
    transcode as transcode_mod, vision,
)
from .db import connect

app = typer.Typer(help="Manage a home video archive: scan, dedupe, rename, tag, browse.", no_args_is_help=True)
tag_app = typer.Typer(help="Add, remove and list tags.", no_args_is_help=True)
app.add_typer(tag_app, name="tag")
console = Console()


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Optional[Path], typer.Option("--db", envvar="VIDCAT_DB", help="Catalog database file.")] = None,
):
    ctx.obj = (db or config.db_path()).expanduser()


def open_db(ctx: typer.Context):
    return connect(ctx.obj)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_duration(s: float | None) -> str:
    if not s:
        return "-"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def human_date(ts: int, source: str | None = None) -> str:
    """Format a capture date; dates that are only the file's modified time are marked with '~'."""
    if source == "filename":
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    text = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    return f"~{text}" if source == "mtime" else text


def open_in_viewer(path: str) -> None:
    subprocess.run(["open", path], check=False)


def progress_reporter():
    """Build a (progress_context, callback) pair that renders scan/hash stages as rich progress bars."""
    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
        console=console, transient=True,
    )
    tasks: dict[str, int] = {}

    def callback(stage: str, done: int, total: int):
        if stage not in tasks:
            tasks[stage] = progress.add_task(stage, total=total or None)
        progress.update(tasks[stage], completed=done, total=total or None)

    return progress, callback


# --------------------------------------------------------------------------- scan

@app.command()
def scan(
    ctx: typer.Context,
    roots: Annotated[list[Path], typer.Argument(exists=True, file_okay=False, help="Folders to scan.")],
    no_thumbs: Annotated[bool, typer.Option("--no-thumbs", help="Skip thumbnail generation.")] = False,
    workers: Annotated[int, typer.Option(help="Parallel ffprobe/ffmpeg workers.")] = 4,
):
    """Add new/changed videos under ROOTS to the catalog (unchanged files are skipped)."""
    conn = open_db(ctx)
    progress, callback = progress_reporter()
    try:
        with progress:
            stats = scanner.scan(
                conn, roots, config.thumbs_dir(ctx.obj), workers=workers,
                make_thumbs=not no_thumbs, on_progress=callback,
            )
    except media.MediaToolError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    total = conn.execute("SELECT COUNT(*) FROM videos WHERE missing = 0").fetchone()[0]
    console.print(
        f"[green]Done.[/green] {stats.added} added, {stats.updated} updated, {stats.unchanged} unchanged, "
        f"{stats.moved} moved, {stats.missing} now missing, {stats.skipped} skipped (not video). "
        f"Catalog has {total} videos."
    )


# --------------------------------------------------------------------------- dupes

@app.command()
def dupes(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be removed; change nothing.")] = False,
):
    """Find identical files and interactively choose which single copy to keep (others go to the Trash)."""
    conn = open_db(ctx)
    progress, callback = progress_reporter()
    with progress:
        groups = duplicates.find_duplicate_groups(conn, callback)
    if not groups:
        console.print("[green]No duplicate videos found.[/green]")
        return
    reclaimable = sum(g[0]["size"] * (len(g) - 1) for g in groups)
    console.print(f"Found [bold]{len(groups)}[/bold] sets of identical files; removing extras would free "
                  f"[bold]{human_size(reclaimable)}[/bold].")
    mode = "trash"
    if dry_run:
        console.print("[yellow]Dry run: nothing will be removed.[/yellow]")
    else:
        off = volumes_without_trash([r["path"] for g in groups for r in g])
        if off:
            mode = choose_duplicate_mode(off)
            if mode is None:
                console.print("Nothing was removed.")
                return

    freed = removed = 0
    outcomes: Counter = Counter()
    for n, group in enumerate(groups, 1):
        default = duplicates.pick_default_keep(group)
        while True:
            table = Table(title=f"Set {n} of {len(groups)}: {human_size(group[0]['size'])} each", title_justify="left")
            for col in ("#", "Path", "Captured", "Length", "Size"):
                table.add_column(col, overflow="fold")
            for i, r in enumerate(group):
                mark = " [green](suggested)[/green]" if i == default else ""
                table.add_row(str(i + 1), r["path"] + mark, human_date(r["created_at"], r["date_source"]),
                              human_duration(r["duration"]), human_size(r["size"]))
            console.print(table)
            answer = Prompt.ask(
                f"Keep which copy? [1-{len(group)}], [bold]s[/bold]kip, [bold]o[/bold]pen N, [bold]q[/bold]uit",
                default=str(default + 1),
            ).strip().lower()
            if answer in ("q", "quit"):
                console.print(f"Removed {removed} file(s), freed {human_size(freed)}.")
                print_disposal_summary(outcomes, config.DUPLICATES_DIR)
                return
            if answer in ("s", "skip"):
                break
            if answer.startswith("o"):
                arg = answer[1:].strip()
                idx = int(arg) - 1 if arg.isdigit() else default
                if 0 <= idx < len(group):
                    open_in_viewer(group[idx]["path"])
                continue
            if answer.isdigit() and 1 <= int(answer) <= len(group):
                keep = group[int(answer) - 1]
                drop = [r for r in group if r["id"] != keep["id"]]
                if dry_run:
                    for r in drop:
                        console.print(f"  [yellow]would trash[/yellow] {r['path']}")
                    break
                try:
                    results = duplicates.remove_copies(conn, keep["id"], [r["id"] for r in drop], mode)
                except Exception as e:  # report and continue with the next set
                    console.print(f"[red]Could not remove: {e}[/red]")
                    break
                removed += len(results)
                freed += keep["size"] * sum(d.action != "archived" for d in results)  # archived files still use space
                for d in results:
                    outcomes[d.action] += 1
                    where = f" → {d.dest}" if d.dest else ""
                    console.print(f"  [red]{d.action}[/red] {d.path}{where}")
                break
            console.print("[red]Please enter a number from the list, s, o N, or q.[/red]")
    console.print(f"[green]Finished.[/green] Removed {removed} file(s), freed {human_size(freed)}.")
    print_disposal_summary(outcomes, config.DUPLICATES_DIR)


# --------------------------------------------------------------------------- names

def input_prefilled(prompt: str, text: str) -> str:
    """Line input with editable pre-filled text (falls back to a plain prompt if readline is unavailable)."""
    try:
        import readline
        readline.set_startup_hook(lambda: readline.insert_text(text))
        try:
            return input(prompt)
        finally:
            readline.set_startup_hook()
    except (ImportError, AttributeError):
        return Prompt.ask(prompt, default=text)


@app.command()
def names(
    ctx: typer.Context,
    ai: Annotated[bool, typer.Option("--ai", help="Also describe each video with a local vision model (Ollama).")] = False,
    model: Annotated[str, typer.Option(help="Ollama vision model.")] = config.vision_model(),
    all_files: Annotated[bool, typer.Option("--all", help="Review every video, not just poorly named ones.")] = False,
    limit: Annotated[Optional[int], typer.Option(help="Stop after this many files.")] = None,
    folder: Annotated[Optional[Path], typer.Option(help="Only videos under this folder.")] = None,
    accept_all: Annotated[bool, typer.Option(
        "--accept-all",
        help="Rename every poorly named video to its suggestion without asking. With --ai, a video whose AI "
             "description fails is skipped, never renamed without one. Undo with `vidcat undo-rename --count N`.")] = False,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="With --accept-all: show the new names but rename nothing.")] = False,
):
    """Find videos without useful names and interactively rename them, with suggestions.

    While reviewing, press `a` to accept the current suggestion and all the remaining ones.
    """
    if accept_all and all_files:
        raise typer.BadParameter("--accept-all only applies to poorly named videos; it can't be combined with --all, "
                                 "which would also rename names that are already fine.")
    if dry_run and not accept_all:
        raise typer.BadParameter("--dry-run only applies together with --accept-all.")

    conn = open_db(ctx)
    sql, params = "SELECT * FROM videos WHERE missing = 0", []
    if not all_files:
        sql += " AND name_score < ?"
        params.append(config.BAD_NAME_THRESHOLD)
    if folder:
        sql += " AND (dir = ? OR path LIKE ?)"
        root = str(folder.expanduser().resolve())
        params += [root, root.rstrip("/") + "/%"]
    rows = conn.execute(sql + " ORDER BY dir, name", params).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("[green]No poorly named videos found.[/green]")
        return
    console.print(f"[bold]{len(rows)}[/bold] video(s) to " + ("rename." if accept_all else "review."))

    ai_requested = ai
    if ai:
        try:
            vision.check_ollama(model)
        except vision.VisionUnavailable as e:
            if accept_all:  # never quietly fall back to plain names for a whole batch
                console.print(f"[red]{e}[/red]\nNothing was renamed.")
                raise typer.Exit(1)
            console.print(f"[yellow]{e}\nContinuing without AI suggestions.[/yellow]")
            ai = False

    accept_rest = accept_all      # once set, remaining suggestions are applied without prompting
    renamed = skipped = 0

    def summary() -> None:
        verb = "Would rename" if dry_run else "Renamed"
        console.print(f"[green]Done.[/green] {verb} {renamed} file(s)" + (f", skipped {skipped}" if skipped else "") + ".")
        if renamed > 1 and not dry_run and (accept_all or accept_rest):
            console.print(f"[dim]To revert this batch: vidcat undo-rename --count {renamed}[/dim]")

    try:
        for n, r in enumerate(rows, 1):
            if not os.path.exists(r["path"]):
                continue
            caption = r["caption"]

            def get_caption(force: bool = False):
                nonlocal caption, ai
                if caption and not force:
                    return
                try:
                    with console.status(f"Asking {model} to describe the video..."):
                        caption = vision.caption_video(r["path"], r["duration"], model)
                    with conn:
                        conn.execute("UPDATE videos SET caption = ? WHERE id = ?", (caption, r["id"]))
                except (vision.VisionUnavailable, media.MediaToolError) as e:
                    console.print(f"[yellow]{e}[/yellow]")
                    if not accept_rest:
                        ai = False   # interactive: carry on without AI. Batch: this file just gets skipped.

            if ai:
                get_caption()
            while True:
                if accept_rest:
                    if ai_requested and not caption:   # the AI failed for this file: don't rename it without one
                        console.print(f"[{n}/{len(rows)}] [yellow]skipped[/yellow] {r['name']} (no AI description)")
                        skipped += 1
                        break
                    suggestion = names_mod.suggest_name(r, caption) + Path(r["name"]).suffix
                    if suggestion == r["name"]:
                        break
                    if dry_run:
                        console.print(f"[{n}/{len(rows)}] would rename {r['name']} → [green]{suggestion}[/green]")
                        renamed += 1
                        break
                    try:
                        new_path = names_mod.rename_video(conn, r["id"], suggestion)
                    except (ValueError, OSError) as e:
                        console.print(f"[{n}/{len(rows)}] [red]skipped[/red] {r['name']}: {e}")
                        skipped += 1
                        break
                    console.print(f"[{n}/{len(rows)}] {r['name']} → [green]{os.path.basename(new_path)}[/green]")
                    renamed += 1
                    break

                suggestion = names_mod.suggest_name(r, caption)
                info = (f"[bold]{r['name']}[/bold]\n{r['dir']}\n"
                        f"{human_date(r['created_at'], r['date_source'])} · {human_duration(r['duration'])} · {human_size(r['size'])}")
                if caption:
                    info += f"\nAI description: {caption}"
                console.print(Panel(info, title=f"{n}/{len(rows)}", title_align="left"))
                console.print(f"Suggested: [green]{suggestion}{Path(r['name']).suffix}[/green]")
                options = ["[bold]Enter[/bold] accept", "[bold]a[/bold]ccept this and all remaining",
                           "[bold]e[/bold]dit", "[bold]o[/bold]pen video"]
                if ai:
                    options.append("[bold]r[/bold]egenerate AI")
                options += ["[bold]k[/bold]eep name forever", "[bold]s[/bold]kip", "[bold]q[/bold]uit", "or type a new name"]
                answer = Prompt.ask(", ".join(options), default="", show_default=False).strip()
                low = answer.lower()
                if low == "q":
                    summary()
                    return
                if low == "s":
                    break
                if low == "o":
                    open_in_viewer(r["path"])
                    continue
                if low == "r" and ai:
                    get_caption(force=True)
                    continue
                if low == "k":
                    with conn:
                        conn.execute("UPDATE videos SET name_score = 100 WHERE id = ?", (r["id"],))
                    break
                if low == "a":
                    if ai_requested and not ai:
                        console.print("[yellow]AI descriptions are unavailable, so 'accept all' is off. "
                                      "Fix that first (or accept names one at a time).[/yellow]")
                        continue
                    accept_rest = True
                    answer = ""   # accept this one now; the rest follow automatically
                if low == "e":
                    answer = input_prefilled("Name: ", suggestion).strip()
                    if not answer:
                        continue
                new_name = answer or suggestion
                try:
                    new_path = names_mod.rename_video(conn, r["id"], new_name)
                except (ValueError, OSError) as e:
                    console.print(f"[red]{e}[/red]")
                    continue
                console.print(f"  [green]renamed[/green] → {os.path.basename(new_path)}")
                renamed += 1
                break
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Files renamed so far are kept.[/yellow]")
    summary()


@app.command("undo-rename")
def undo_rename(
    ctx: typer.Context,
    count: Annotated[int, typer.Option("--count", "-n", min=1, help="Revert this many of the most recent renames.")] = 1,
):
    """Revert the most recent rename (or the last N with --count)."""
    conn = open_db(ctx)
    restored = 0
    for _ in range(count):
        try:
            result = names_mod.undo_last_rename(conn)
        except OSError as e:
            console.print(f"[red]Stopped: {e}[/red]")
            break
        if result is None:
            console.print("Nothing more to undo." if restored else "Nothing to undo.")
            break
        restored += 1
        console.print(f"Restored {os.path.basename(result[1])}")
    if restored > 1:
        console.print(f"[green]Reverted {restored} renames.[/green]")


# --------------------------------------------------------------------------- places

@app.command()
def places(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only count the spots that would be looked up.")] = False,
    language: Annotated[str, typer.Option(help="Language for place names, e.g. en, fr, es.")] = "en",
):
    """Look up place names for every spot videos were filmed at, so the web UI never waits for one."""
    conn = open_db(ctx)
    spots = places_mod.unnamed_spots(conn)
    if not spots:
        console.print("Every video with a location already has its place name.")
        return
    minutes = len(spots) / 60
    console.print(f"{len(spots)} spot{'' if len(spots) == 1 else 's'} to look up "
                  f"(OpenStreetMap allows one a second, so about {max(1, round(minutes))} min).")
    if dry_run:
        return
    found = nothing = failures = 0
    progress = Progress(SpinnerColumn(), TextColumn("Looking up places"), BarColumn(), MofNCompleteColumn(),
                        TimeRemainingColumn(), console=console, transient=True)
    try:
        with progress:
            task = progress.add_task("", total=len(spots))
            for lat, lon in spots:
                try:
                    name = places_mod.place_name(conn, lat, lon, language)
                except places_mod.PlaceUnavailable as e:
                    failures += 1
                    if failures >= 3:  # three in a row: offline, or the service is down
                        progress.stop()
                        console.print(f"[red]Stopped: {e}[/red]")
                        break
                    progress.advance(task)
                    continue
                failures = 0
                found += bool(name)
                nothing += not name
                progress.advance(task)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Place names found so far are kept.[/yellow]")
    left = len(places_mod.unnamed_spots(conn))
    console.print(f"[green]Done.[/green] {found} place name{'' if found == 1 else 's'} saved"
                  f"{f', {nothing} with nothing there (open water)' if nothing else ''}"
                  f"{f'; {left} still to look up, run it again later' if left else ''}.")


# --------------------------------------------------------------------------- transcode

@app.command("transcode")
def transcode_cmd(
    ctx: typer.Context,
    ext: Annotated[Optional[list[str]], typer.Option(
        "--ext", help="Extension to convert (repeatable). Default: mpg, mpeg, mpe, wmv, asf, avi, mov, m4v, 3gp.")] = None,
    folder: Annotated[Optional[Path], typer.Option(help="Only videos under this folder.")] = None,
    codec: Annotated[str, typer.Option(help="h264 (plays everywhere, incl. the web UI) or hevc (smaller files).")] = "h264",
    crf: Annotated[Optional[int], typer.Option(help="Quality; lower = better and bigger. Default 20 (h264) / 24 (hevc).")] = None,
    preset: Annotated[str, typer.Option(help="Encoder speed/size trade-off (ultrafast … veryslow).")] = "medium",
    deinterlace: Annotated[str, typer.Option(help="auto (detect), always, or never.")] = "auto",
    originals: Annotated[str, typer.Option(
        help="After a verified conversion: ask (default), keep, trash, archive (move into an 'Originals (vidcat)' "
             "folder beside each file), or delete (permanent; needs interactive confirmation). Volumes with no "
             "Trash, such as network shares, use the archive folder instead of the Trash.")] = "ask",
    include_playable: Annotated[bool, typer.Option(
        "--include-playable",
        help=".mov/.m4v files that browsers can already play (H.264 + AAC) are skipped; convert them anyway.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be converted; change nothing.")] = False,
    limit: Annotated[Optional[int], typer.Option(help="Convert at most this many files.")] = None,
):
    """Convert old .mpg/.wmv/.avi/.mov/.m4v/.3gp files to MP4 (H.264 + AAC) next to the originals.

    Each result is verified (readable, same length, audio present) before anything else happens.
    Re-running is safe: files that already have a good .mp4 beside them aren't re-encoded, so
    `vidcat transcode --originals trash` after checking the results will clean up the originals.

    Network shares (SMB/NFS/AFP) usually have no Trash, so originals there are never sent to it: you're offered
    a recoverable "Originals (vidcat)" folder beside each file, or permanent deletion.
    """
    for value, allowed, label in ((codec, transcode_mod.CODECS, "codec"), (preset, transcode_mod.PRESETS, "preset"),
                                  (deinterlace, ("auto", "always", "never"), "deinterlace"),
                                  (originals, ("ask", "keep", "trash", "archive", "delete"), "originals")):
        if value not in allowed:
            raise typer.BadParameter(f"--{label} must be one of: {', '.join(allowed)}")

    conn = open_db(ctx)
    exts = [e.lower().lstrip(".") for e in (ext or transcode_mod.DEFAULT_EXTS)]
    sql = f"SELECT * FROM videos WHERE missing = 0 AND ext IN ({','.join('?' * len(exts))})"
    params: list = list(exts)
    if folder:
        root = str(folder.expanduser().resolve())
        sql += " AND (dir = ? OR path LIKE ?)"
        params += [root, root.rstrip("/") + "/%"]
    rows = conn.execute(sql + " ORDER BY dir, name", params).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print(f"No cataloged .{'/.'.join(exts)} videos to convert. (Run `vidcat scan` first?)")
        return
    console.print(f"[bold]{len(rows)}[/bold] video(s) to convert, {human_size(sum(r['size'] for r in rows))} in total.")

    if dry_run:
        for r in rows:
            target = transcode_mod.output_path(Path(r["path"]))
            if not Path(r["path"]).exists():
                note = " [red](file not found; run `vidcat scan` to refresh the catalog)[/red]"
            else:
                note = " [yellow](already exists)[/yellow]" if target.exists() else ""
                if not note and not include_playable and Path(r["path"]).suffix.lower() in transcode_mod.BROWSER_CONTAINERS:
                    probed = media.probe(r["path"])
                    if probed and transcode_mod.browser_compatible(probed):
                        note = " [dim](already plays in browsers; would be skipped)[/dim]"
            console.print(f"  {r['name']} → {target.name}{note}")
        console.print("[yellow]Dry run: nothing was converted.[/yellow]")
        return

    try:
        media.require_tools()
    except media.MediaToolError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    settings = transcode_mod.Settings(codec=codec, crf=crf, preset=preset, deinterlace=deinterlace)
    done, failed, playable = [], 0, 0
    try:
        for n, r in enumerate(rows, 1):
            src = Path(r["path"])
            if not src.exists():
                continue
            console.print(f"[{n}/{len(rows)}] [bold]{r['name']}[/bold]")
            trusted = r["date_source"] in ("metadata", "filename")  # don't bake a mere copy date into the file
            progress = Progress(TextColumn("  Encoding"), BarColumn(), TaskProgressColumn(), TimeRemainingColumn(),
                                console=console, transient=True)
            with progress:
                task = progress.add_task("", total=1.0)
                try:
                    dst, encoded = transcode_mod.convert(
                        src, settings, creation_time=r["created_at"] if trusted else None,
                        on_progress=lambda f: progress.update(task, completed=f),
                        include_playable=include_playable,
                    )
                except transcode_mod.AlreadyPlayable:
                    console.print("  [dim]Already plays in browsers; left as is (use --include-playable to convert).[/dim]")
                    playable += 1
                    continue
                except transcode_mod.TranscodeError as e:
                    console.print(f"  [red]Skipped:[/red] {e}")
                    failed += 1
                    continue
            if any(d == dst for _, d in done):  # defensive: two originals must never share one output
                console.print(f"  [red]Skipped:[/red] {dst.name} already belongs to another file in this run")
                failed += 1
                continue
            note = f"{human_size(r['size'])} → {human_size(dst.stat().st_size)}" if encoded else "already converted"
            console.print(f"  [green]✓[/green] {dst.name} ({note})")
            done.append((r, dst))
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted. Finished conversions are kept; the unfinished one was discarded.[/yellow]")

    already = f", {playable} already play in browsers" if playable else ""
    if not done:
        console.print(f"Nothing converted{f', {failed} skipped' if failed else ''}{already}.")
        return

    # Catalog the new files; they inherit tags and descriptions from the originals.
    scanner.scan_files(conn, [dst for _, dst in done], config.thumbs_dir(ctx.obj))
    converted = []
    for r, dst in done:
        new = conn.execute("SELECT * FROM videos WHERE path = ?", (str(dst.resolve()),)).fetchone()
        if new is None:
            console.print(f"[yellow]Couldn't catalog {dst.name}; keeping {r['name']}.[/yellow]")
            continue
        tags_mod.merge_tags(conn, r["id"], new["id"])
        with conn:
            if r["caption"] and not new["caption"]:
                conn.execute("UPDATE videos SET caption = ? WHERE id = ?", (r["caption"], new["id"]))
            if r["rotation"]:  # same picture, so it needs the same turn to look upright
                conn.execute("UPDATE videos SET rotation = ? WHERE id = ?", (r["rotation"], new["id"]))
            if r["date_source"] == "filename":
                # The date was embedded in the new file so it survives, but we only know the day, not a time.
                conn.execute("UPDATE videos SET date_source = 'filename', created_at = ? WHERE id = ?",
                             (r["created_at"], new["id"]))
        converted.append((r, new))

    before = sum(r["size"] for r, _ in converted)
    after = sum(new["size"] for _, new in converted)
    console.print(f"[green]Converted {len(converted)} file(s)[/green]"
                  f"{f', {failed} skipped' if failed else ''}{already}. Originals {human_size(before)}, MP4s {human_size(after)}.")

    mode = None  # what to do with the originals: "trash" | "archive" | "delete" | None (keep)
    if originals == "ask":
        if sys.stdin.isatty() and converted:
            console.print("[dim]Tip: play a few of the new files first — you can also decide later by running "
                          "`vidcat transcode --originals trash`.[/dim]")
            mode = choose_originals_mode([r["path"] for r, _ in converted], before)
    elif originals == "delete":
        if sys.stdin.isatty() and confirm_permanent_delete(len(converted), "original file(s)"):
            mode = "delete"
        elif not sys.stdin.isatty():
            console.print("[yellow]Permanent deletion needs an interactive terminal to confirm; originals kept.[/yellow]")
    elif originals in ("trash", "archive"):
        mode = originals

    if mode is None:
        console.print("Originals kept. Run `vidcat transcode --originals trash` later to remove them.")
        return
    counts: Counter = Counter()
    for r, new in converted:
        if not Path(new["path"]).exists():
            continue  # never remove an original unless its replacement is on disk
        try:
            if Path(r["path"]).exists():
                counts[disposal.dispose(r["path"], mode, config.ORIGINALS_DIR).action] += 1
            with conn:
                conn.execute("DELETE FROM videos WHERE id = ?", (r["id"],))
        except OSError as e:  # includes a Trash that didn't respond; the original stays where it is
            console.print(f"[red]Couldn't remove {r['name']}: {e}[/red]")
    print_disposal_summary(counts, config.ORIGINALS_DIR)


def print_disposal_summary(counts: Counter, folder: str) -> None:
    if counts["trashed"]:
        console.print(f"Moved {counts['trashed']} file(s) to the Trash.")
    if counts["archived"]:
        console.print(f"Moved {counts['archived']} file(s) into \"{folder}\" folders beside where they were "
                      f"(their volume has no Trash). Delete those folders when you're sure.")
    if counts["deleted"]:
        console.print(f"Permanently deleted {counts['deleted']} file(s).")


def confirm_permanent_delete(count: int, noun: str) -> bool:
    console.print(f"[bold red]This permanently deletes {count} {noun}. It cannot be undone.[/bold red]")
    return Prompt.ask("Type 'delete' to confirm", default="") == "delete"


def volumes_without_trash(paths: list[str]) -> list[str]:
    return sorted({disposal.volume_label(p) or "(unknown volume)" for p in paths if not disposal.trash_available(p)})


def choose_originals_mode(paths: list[str], total_bytes: int) -> str | None:
    """Ask what to do with originals after a conversion. Returns "trash", "archive", "delete", or None (keep)."""
    off = volumes_without_trash(paths)
    if not off:
        ok = Confirm.ask(f"Move the {len(paths)} original file(s) ({human_size(total_bytes)}) to the Trash now?",
                         default=False)
        return "trash" if ok else None
    console.print(f"[yellow]{len(paths)} original file(s), {human_size(total_bytes)}. Some are on a volume with no "
                  f"Trash: {', '.join(off)}[/yellow]")
    console.print(f"  [bold]a[/bold]rchive  move them into an \"{config.ORIGINALS_DIR}\" folder beside each file "
                  "(instant and undoable; frees no space; anything on a volume with a Trash goes there instead)")
    console.print("  [bold]d[/bold]elete   remove them permanently (frees space; cannot be undone)")
    console.print("  [bold]k[/bold]eep     leave them where they are")
    answer = Prompt.ask("Choice", choices=["a", "d", "k"], default="k")
    if answer == "a":
        return "trash"  # Trash where a volume has one, the holding folder where it doesn't
    if answer == "d" and confirm_permanent_delete(len(paths), "original file(s)"):
        return "delete"
    return None


def choose_duplicate_mode(off: list[str]) -> str | None:
    """Before removing duplicates from volumes with no Trash: how should 'remove' work? None means stop."""
    console.print(f"[yellow]Some of these files are on a volume with no Trash: {', '.join(off)}[/yellow]")
    console.print("Copies you remove from there can't go to the Trash. Choose what removing should do:")
    console.print(f"  [bold]a[/bold]rchive  move removed copies into a \"{config.DUPLICATES_DIR}\" folder beside them "
                  "(instant and undoable; frees no space; a volume with a Trash still uses it)")
    console.print("  [bold]d[/bold]elete   remove them permanently (frees space; cannot be undone)")
    console.print("  [bold]q[/bold]uit")
    answer = Prompt.ask("Choice", choices=["a", "d", "q"], default="a")
    if answer == "a":
        return "trash"
    if answer == "d":
        return "delete" if Prompt.ask("Type 'delete' to permanently delete every copy you remove",
                                      default="") == "delete" else None
    return None


# --------------------------------------------------------------------------- tags & listing

def resolve_video(conn, ref: str):
    """Find a video by catalog id, exact path, or unique file name."""
    if ref.isdigit():
        row = conn.execute("SELECT * FROM videos WHERE id = ? AND missing = 0", (int(ref),)).fetchone()
        if row:
            return row
    row = conn.execute("SELECT * FROM videos WHERE path = ?", (str(Path(ref).expanduser().resolve()),)).fetchone()
    if row:
        return row
    rows = conn.execute("SELECT * FROM videos WHERE name = ? AND missing = 0", (ref,)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise typer.BadParameter(f"'{ref}' matches {len(rows)} files; use the id or full path (see `vidcat ls`).")
    raise typer.BadParameter(f"No video found for '{ref}'. Use an id from `vidcat ls`, a path, or a file name.")


@tag_app.command("add")
def tag_add(ctx: typer.Context, ref: Annotated[str, typer.Argument(help="Video id, path, or file name.")],
            tags: Annotated[list[str], typer.Argument(help="Tags to add.")]):
    """Add tags to a video."""
    conn = open_db(ctx)
    video = resolve_video(conn, ref)
    tags_mod.add_tags(conn, video["id"], tags)
    console.print(f"{video['name']}: {', '.join(tags_mod.tags_for(conn, [video['id']])[video['id']])}")


@tag_app.command("remove")
def tag_remove(ctx: typer.Context, ref: Annotated[str, typer.Argument(help="Video id, path, or file name.")],
               tags: Annotated[list[str], typer.Argument(help="Tags to remove.")]):
    """Remove tags from a video."""
    conn = open_db(ctx)
    video = resolve_video(conn, ref)
    tags_mod.remove_tags(conn, video["id"], tags)
    console.print(f"{video['name']}: {', '.join(tags_mod.tags_for(conn, [video['id']])[video['id']]) or '(no tags)'}")


@tag_app.command("list")
def tag_list(ctx: typer.Context, ref: Annotated[Optional[str], typer.Argument(help="Video (omit for all tags).")] = None):
    """List all tags with counts, or the tags on one video."""
    conn = open_db(ctx)
    if ref:
        video = resolve_video(conn, ref)
        console.print(", ".join(tags_mod.tags_for(conn, [video["id"]])[video["id"]]) or "(no tags)")
        return
    for t in tags_mod.all_tags(conn):
        console.print(f"{t['name']} [dim]({t['count']})[/dim]")


@app.command()
def ls(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search words (name, path, tags, description).")] = "",
    tag: Annotated[Optional[list[str]], typer.Option(help="Require this tag (repeatable).")] = None,
    bad_names: Annotated[bool, typer.Option("--bad-names", help="Only poorly named videos.")] = False,
    limit: Annotated[int, typer.Option(help="Max rows.")] = 50,
):
    """List catalog entries (with ids for use with `vidcat tag`)."""
    conn = open_db(ctx)
    res = queries.search_videos(conn, q=query, tags=tag or [], bad_name=bad_names, sort="path",
                                order="asc", page_size=limit)
    table = Table()
    for col in ("ID", "Name", "Captured", "Length", "Tags"):
        table.add_column(col, overflow="fold")
    for v in res["items"]:
        table.add_row(str(v["id"]), v["name"], human_date(v["created_at"], v["date_source"]), human_duration(v["duration"]),
                      ", ".join(v["tags"]))
    console.print(table)
    console.print(f"[dim]Showing {len(res['items'])} of {res['total']}[/dim]")


# --------------------------------------------------------------------------- serve

@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Interface to bind. Keep the default unless you know why.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the UI in your browser.")] = False,
):
    """Start the web UI."""
    import uvicorn

    from .web.app import create_app

    local = host in ("127.0.0.1", "localhost", "::1")
    if not local:
        console.print("[yellow]Warning: the web UI can rename files and has no login. "
                      "Only expose it on a network you trust.[/yellow]")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    console.print(f"Serving on [bold]http://{host}:{port}[/bold]  (Ctrl+C to stop)")
    uvicorn.run(create_app(ctx.obj, allow_any_host=not local), host=host, port=port, log_level="warning")
