"""CLI entry point — all dsync subcommands live here."""

from __future__ import annotations

import difflib
import io
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import click
import paramiko
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from .config import list_profiles, load_config
from .log import append_log, read_log
from .ssh import SSHManager
from .state import StateManager, _matches_ignore
from .sync import (
    SyncError,
    backup_remote_files,
    create_full_backup,
    delete_backups,
    file_to_url,
    find_old_backups,
    is_safe_rel_path,
    push_single_file,
    rsync_pull,
    rsync_push_all,
    rsync_push_directory,
    rsync_push_dry_run,
    rsync_status,
    run_hook,
)
from .watcher import FileWatcher

console = Console()


def _to_rel_path(config, path: str) -> str:
    """Map a CLI path argument to a path relative to local_root.

    Accepts a path relative to local_root, or any path (absolute or relative
    to the cwd) that resolves to somewhere under local_root.
    """
    root = config.local_root.resolve()
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if not resolved.exists() and not resolved.is_relative_to(root):
            resolved = (root / path.lstrip("/")).resolve()
    else:
        resolved = (root / candidate).resolve()
        cwd_path = candidate.resolve()
        if not resolved.exists() and cwd_path.exists():
            resolved = cwd_path
    try:
        rel = resolved.relative_to(root).as_posix()
    except ValueError:
        raise SyncError(f"Path is outside the site root: {path}") from None
    return "" if rel == "." else rel


@click.group()
@click.option(
    "--profile", "-p", default=None, metavar="NAME", help="Config profile name."
)
@click.pass_context
def cli(ctx: click.Context, profile: str | None) -> None:
    """dsync — SSH deploy tool for dylansparks.com."""
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
def pull(ctx: click.Context) -> None:
    """Pull the full site from the server to local."""
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)
    state = StateManager(profile=profile)

    if not state.is_empty():
        console.print(
            "[yellow]⚠[/] You have a local sync state. "
            "Pulling may overwrite local edits that haven't been pushed."
        )
        if not click.confirm("Continue?", default=True):
            return

    if not run_hook(config, "pre_pull"):
        return

    t0 = time.monotonic()
    try:
        with SSHManager(config, profile=profile) as ssh:  # noqa: F841  (establishes connection + verifies auth)
            pulled = rsync_pull(config, state)
    except (SyncError, paramiko.SSHException, OSError) as exc:
        _log_failure("pull", [], t0, profile)
        raise click.ClickException(str(exc)) from exc
    append_log(
        "pull",
        pulled,
        ok=True,
        duration_ms=int((time.monotonic() - t0) * 1000),
        profile=profile,
    )
    run_hook(config, "post_pull")


def _log_failure(action: str, files: list[str], t0: float, profile: str | None) -> None:
    append_log(
        action,
        files,
        ok=False,
        duration_ms=int((time.monotonic() - t0) * 1000),
        profile=profile,
    )


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path", required=False)
@click.option(
    "--diff", "show_diff", is_flag=True, help="Show file diffs before pushing."
)
@click.pass_context
def push(ctx: click.Context, path: str | None, show_diff: bool) -> None:
    """
    Push local changes to the server.

    With no argument, diffs local vs remote, shows the list of changed
    files, prompts for confirmation, then syncs.  With PATH, pushes only
    that specific file or directory.
    """
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)
    state = StateManager(profile=profile)

    if not run_hook(config, "pre_push"):
        return

    t0 = time.monotonic()
    try:
        if path:
            _to_rel_path(config, path)
        with SSHManager(config, profile=profile) as ssh:
            if path:
                transferred = _push_path(ssh, config, state, path)
            else:
                transferred = _push_all_interactive(
                    ssh, config, state, show_diff=show_diff
                )
    except (SyncError, paramiko.SSHException, OSError) as exc:
        _log_failure("push", [path] if path else [], t0, profile)
        raise click.ClickException(str(exc)) from exc

    # A run with nothing to push (or a declined prompt) is not a failure, but
    # it is not worth an audit-log entry or a post_push hook either.
    if transferred:
        append_log(
            "push",
            transferred,
            ok=True,
            duration_ms=int((time.monotonic() - t0) * 1000),
            profile=profile,
        )
        run_hook(config, "post_push")


def _push_path(ssh: SSHManager, config, state: StateManager, path: str) -> list[str]:
    """Push a specific file or directory. Returns the files deployed.

    Raises SyncError if the path is invalid or the transfer fails.
    """
    rel_path = _to_rel_path(config, path)
    if rel_path == "":
        raise SyncError("That is the site root — run `dsync push` with no path.")
    if not is_safe_rel_path(rel_path):
        raise SyncError(f"Path is outside the site root: {path}")

    local_path = config.local_root / rel_path
    if not local_path.exists():
        raise SyncError(f"Path not found: {local_path}")

    if local_path.is_file():
        console.print("[blue]ℹ[/] Backing up remote file...")
        if not push_single_file(ssh, config, state, rel_path):
            raise SyncError(f"Upload of {rel_path} failed")
        url = file_to_url(config, rel_path)
        console.print(f"[green]✓[/] Done — {url}")
        return [rel_path]

    # Directory — use rsync for the subtree.
    console.print(f"[blue]ℹ[/] Pushing directory: {rel_path}/")
    transferred = rsync_push_directory(config, state, rel_path)
    for f in transferred:
        url = file_to_url(config, f)
        console.print(f"  [green]✓[/] {f} → {url}")
    console.print(f"\n[green]✓[/] {len(transferred)} file(s) pushed.")
    return transferred


def _push_all_interactive(
    ssh: SSHManager, config, state: StateManager, show_diff: bool = False
) -> list[str]:
    """Full push: diff → [show diff] → confirm → backup → sync. Returns transferred files."""
    console.print("[blue]ℹ[/] Computing changes...")
    changed = rsync_push_dry_run(config)

    if not changed:
        console.print("[green]✓[/] Everything is in sync.")
        return []

    console.print(f"\n[bold]Files to push[/] ({len(changed)}):")
    for f in changed:
        console.print(f"  [cyan]{f}[/]")

    if show_diff:
        _show_push_diffs(ssh, config, changed)

    if not click.confirm(f"\nPush {len(changed)} file(s)?", default=True):
        return []

    console.print("[blue]ℹ[/] Creating server backup of changed files...")
    try:
        backup_dir, failed = backup_remote_files(ssh, config, changed)
    except Exception as e:
        console.print(f"[yellow]⚠[/] Backup failed: {e}")
        if not click.confirm("Push without a backup?", default=False):
            return []
    else:
        if failed:
            console.print(
                f"[yellow]⚠[/] Backup at {backup_dir} is missing "
                f"{len(failed)} file(s) that could not be copied:"
            )
            for f in failed:
                console.print(f"  [yellow]{f}[/]")
            if not click.confirm("Push anyway?", default=False):
                return []
        else:
            console.print(f"[green]✓[/] Backup at {backup_dir}")

    transferred = rsync_push_all(config, state)
    console.print()
    for f in transferred:
        url = file_to_url(config, f)
        console.print(f"  [green]✓[/] {f} → {url}")
    console.print(f"\n[green]✓[/] {len(transferred)} file(s) pushed.")
    return transferred


def _show_push_diffs(ssh: SSHManager, config, changed: list[str]) -> None:
    """Fetch remote versions and display unified diffs for changed text files."""
    console.print("\n[bold]Diffs[/] (remote → local):")
    for rel_path in changed:
        local_path = config.local_root / rel_path
        try:
            local_text = local_path.read_text(errors="replace")
        except Exception:
            continue
        if "\x00" in local_text:
            console.print(f"\n  [dim]{rel_path} — binary file, skipped[/]")
            continue
        try:
            buf = io.BytesIO()
            ssh.sftp.getfo(config.remote_root + rel_path, buf)
            remote_text = buf.getvalue().decode("utf-8", errors="replace")
        except Exception:
            remote_text = ""  # new file — show full content as addition
        diff_lines = list(
            difflib.unified_diff(
                remote_text.splitlines(keepends=True),
                local_text.splitlines(keepends=True),
                fromfile=f"remote/{rel_path}",
                tofile=f"local/{rel_path}",
            )
        )
        if not diff_lines:
            console.print(f"\n  [dim]{rel_path} — no text diff (metadata only)[/]")
            continue
        console.print(f"\n[bold dim]{rel_path}[/]")
        console.print(Syntax("".join(diff_lines), "diff", theme="monokai"))


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
def watch(ctx: click.Context) -> None:
    """Watch local files and auto-push on save."""
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)
    state = StateManager(profile=profile)
    ssh = SSHManager(config, profile=profile)
    ssh.connect()

    MAX_RETRIES = 3
    failures: list[str] = []
    _retry_state: dict[str, dict] = {}
    # Guards _retry_state and failures (touched from watcher + retry-timer threads).
    _retry_lock = threading.Lock()
    # Serializes the actual upload: paramiko's SFTP channel and the StateManager
    # are not thread-safe, and retry timers can fire concurrently with new events.
    _upload_lock = threading.Lock()

    def _attempt_upload(rel_path: str) -> None:
        local_path = config.local_root / rel_path
        if not local_path.exists():
            return
        t0 = time.monotonic()
        with _upload_lock:
            success = push_single_file(ssh, config, state, rel_path)
        duration_ms = int((time.monotonic() - t0) * 1000)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        if success:
            with _retry_lock:
                _retry_state.pop(rel_path, None)
            url = file_to_url(config, rel_path)
            console.print(
                f"[dim][{timestamp}][/] [green]✓[/] pushed {rel_path} → {url}"
            )
            append_log(
                "watch_push",
                [rel_path],
                ok=True,
                duration_ms=duration_ms,
                profile=profile,
            )
        else:
            with _retry_lock:
                entry = _retry_state.get(rel_path, {"count": 0})
                count = entry["count"] + 1
                retrying = count < MAX_RETRIES
                if retrying:
                    delay = 2**count  # 2s, 4s
                    timer = threading.Timer(delay, _attempt_upload, args=[rel_path])
                    _retry_state[rel_path] = {"count": count, "timer": timer}
                else:
                    _retry_state.pop(rel_path, None)
                    failures.append(rel_path)
            if retrying:
                console.print(
                    f"[dim][{timestamp}][/] [yellow]⚠[/] failed {rel_path} "
                    f"(retry {count}/{MAX_RETRIES - 1} in {delay}s)"
                )
                timer.start()
            else:
                console.print(
                    f"[dim][{timestamp}][/] [red]✗[/] gave up on {rel_path} "
                    f"after {MAX_RETRIES} attempts"
                )
                append_log(
                    "watch_push",
                    [rel_path],
                    ok=False,
                    duration_ms=duration_ms,
                    profile=profile,
                )

    def on_change(rel_path: str) -> None:
        # Cancel any pending retry before attempting a fresh upload.
        with _retry_lock:
            existing = _retry_state.pop(rel_path, None)
        if existing:
            timer = existing.get("timer")
            if timer:
                timer.cancel()
        _attempt_upload(rel_path)

    watcher = FileWatcher(config, on_change)
    try:
        watcher.run()
    finally:
        # Cancel any pending retry timers before closing the SSH connection.
        with _retry_lock:
            pending = list(_retry_state.values())
            _retry_state.clear()
        for entry in pending:
            t = entry.get("timer")
            if t:
                t.cancel()
        ssh.close()
        if failures:
            console.print(
                f"\n[red]Failed uploads ({len(failures)}):[/] " + ", ".join(failures)
            )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--local",
    "local_only",
    is_flag=True,
    help="Fast local-only check against last sync state (no network).",
)
@click.pass_context
def status(ctx: click.Context, local_only: bool) -> None:
    """Show diff between local and remote — which files are out of sync."""
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)

    if local_only:
        state = StateManager(profile=profile)
        _status_local(config, state)
        return

    console.print("[blue]ℹ[/] Comparing local vs remote (this may take a moment)...")

    try:
        with SSHManager(config, profile=profile) as ssh:
            groups = rsync_status(config, ssh)
    except (SyncError, paramiko.SSHException, OSError) as exc:
        raise click.ClickException(str(exc)) from exc

    total = sum(len(v) for v in groups.values())
    if total == 0:
        console.print("[green]✓[/] Everything is in sync.")
        return

    table = Table(title="Sync Status", show_header=True, header_style="bold")
    table.add_column("Status", min_width=14)
    table.add_column("File")

    for f in groups.get("local_newer", []):
        table.add_row("[green]local newer[/]", f)
    for f in groups.get("remote_newer", []):
        table.add_row("[red]remote newer[/]", f)
    for f in groups.get("local_only", []):
        table.add_row("[cyan]local only[/]", f)
    for f in groups.get("remote_only", []):
        table.add_row("[magenta]remote only[/]", f)

    console.print(table)


def _status_local(config, state: StateManager) -> None:
    """Fast local-only status: compare local files against the sync state manifest."""
    if state.is_empty():
        console.print(
            "[yellow]⚠[/] No sync state found. "
            "Run [bold]dsync pull[/] or [bold]dsync push[/] first."
        )
        return

    modified: list[str] = []
    deleted: list[str] = []
    seen: set[str] = set()

    for rel_path, file_state in state.items():
        seen.add(rel_path)
        local_path = config.local_root / rel_path
        if not local_path.exists():
            deleted.append(rel_path)
        elif local_path.stat().st_mtime != file_state.mtime:
            modified.append(rel_path)

    new_files: list[str] = []
    for local_file in config.local_root.rglob("*"):
        if not local_file.is_file():
            continue
        rel = str(local_file.relative_to(config.local_root))
        if _matches_ignore(rel, config.ignore_patterns):
            continue
        if rel not in seen:
            new_files.append(rel)

    total = len(modified) + len(new_files) + len(deleted)
    if total == 0:
        console.print("[green]✓[/] No local changes since last sync.")
        return

    table = Table(
        title="Local Status (vs last sync)", show_header=True, header_style="bold"
    )
    table.add_column("Status", min_width=12)
    table.add_column("File")

    for f in sorted(modified):
        table.add_row("[yellow]modified[/]", f)
    for f in sorted(new_files):
        table.add_row("[cyan]new[/]", f)
    for f in sorted(deleted):
        table.add_row("[red]deleted[/]", f)

    console.print(table)
    console.print(
        "\n[dim]Local check only — run [bold]dsync status[/] to compare with remote.[/]"
    )


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
def backup(ctx: click.Context) -> None:
    """Create a full tar.gz backup of the site in the remote backup_dir."""
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)
    t0 = time.monotonic()
    try:
        with SSHManager(config, profile=profile) as ssh:
            backup_path = create_full_backup(ssh, config)
            console.print(f"[green]✓[/] Backup created: {backup_path}")
    except (RuntimeError, paramiko.SSHException, OSError) as exc:
        _log_failure("backup", [], t0, profile)
        raise click.ClickException(str(exc)) from exc
    append_log(
        "backup",
        [],
        ok=True,
        duration_ms=int((time.monotonic() - t0) * 1000),
        profile=profile,
    )


@cli.command("prune-backups")
@click.option(
    "--days",
    type=click.IntRange(min=1),
    default=None,
    help="Delete backups older than this many days "
    "(default: backup_keep_days from config).",
)
@click.option("--yes", "-y", is_flag=True, help="Delete without confirming.")
@click.pass_context
def prune_backups(ctx: click.Context, days: int | None, yes: bool) -> None:
    """Delete old dsync backups from the remote backup_dir.

    Only entries dsync itself created (timestamp-named directories and
    archives) are considered; anything else in that directory is left alone.
    """
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)
    if days is None:
        days = config.backup_keep_days
    if days is None:
        raise click.UsageError("Pass --days N, or set backup_keep_days in the config.")

    t0 = time.monotonic()
    try:
        with SSHManager(config, profile=profile) as ssh:
            old = find_old_backups(ssh, config, days)
            if not old:
                console.print(f"[green]✓[/] No backups older than {days} day(s).")
                return
            console.print(f"[bold]Backups older than {days} day(s)[/] ({len(old)}):")
            for p in old:
                console.print(f"  [red]{p}[/]")
            if not yes and not click.confirm(
                f"\nPermanently delete {len(old)} backup(s)?", default=False
            ):
                return
            delete_backups(ssh, old)
    except (RuntimeError, paramiko.SSHException, OSError) as exc:
        _log_failure("prune", [], t0, profile)
        raise click.ClickException(str(exc)) from exc
    console.print(f"[green]✓[/] Deleted {len(old)} backup(s).")
    append_log(
        "prune",
        old,
        ok=True,
        duration_ms=int((time.monotonic() - t0) * 1000),
        profile=profile,
    )


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


@cli.command("open")
@click.argument("path", required=False)
@click.pass_context
def open_url(ctx: click.Context, path: str | None) -> None:
    """Open the live URL for a local file path in the default browser."""
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)

    if path:
        try:
            url = file_to_url(config, _to_rel_path(config, path))
        except SyncError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        url = config.site_url

    console.print(f"[blue]ℹ[/] Opening {url}")
    webbrowser.open(url)


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


@cli.command("log")
@click.option("-n", default=20, show_default=True, help="Number of entries to show.")
def show_log(n: int) -> None:
    """Show recent sync operation history."""
    entries = read_log(n=n)
    if not entries:
        console.print("[dim]No log entries yet.[/]")
        return

    table = Table(title="Sync Log", show_header=True, header_style="bold")
    table.add_column("Time", min_width=19)
    table.add_column("Action", min_width=10)
    table.add_column("Files", min_width=5, justify="right")
    table.add_column("Duration")
    table.add_column("Status", min_width=6)
    table.add_column("Profile")

    for e in entries:
        status_str = "[green]ok[/]" if e.get("ok") else "[red]failed[/]"
        ms = e.get("ms", 0)
        duration_str = f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"
        table.add_row(
            e.get("ts", ""),
            e.get("action", ""),
            str(len(e.get("files", []))),
            duration_str,
            status_str,
            e.get("profile") or "[dim]default[/]",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


@cli.command("profiles")
def profiles_cmd() -> None:
    """List available configuration profiles."""
    names = list_profiles()
    if not names:
        console.print(
            "[dim]No profiles found. Run [bold]dsync[/] to set up a default profile.[/]"
        )
        return
    console.print("[bold]Available profiles:[/]")
    for name in names:
        console.print(f"  [cyan]{name}[/]")
    console.print(
        "\n[dim]Use [bold]dsync --profile NAME <command>[/] to target a specific profile.[/]"
    )
