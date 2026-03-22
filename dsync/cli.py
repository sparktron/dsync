"""CLI entry point — all dsync subcommands live here."""

from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import load_config, list_profiles
from .log import append_log, read_log
from .ssh import SSHManager
from .state import StateManager
from .sync import (
    backup_remote_files,
    create_full_backup,
    file_to_url,
    push_single_file,
    rsync_push_all,
    rsync_push_directory,
    rsync_push_dry_run,
    rsync_pull,
    rsync_status,
    run_hook,
)
from .watcher import FileWatcher

console = Console()


@click.group()
@click.option("--profile", "-p", default=None, metavar="NAME", help="Config profile name.")
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
    with SSHManager(config) as ssh:  # noqa: F841  (establishes connection + verifies auth)
        rsync_pull(config, state)
    append_log("pull", [], ok=True, duration_ms=int((time.monotonic() - t0) * 1000), profile=profile)
    run_hook(config, "post_pull")


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path", required=False)
@click.pass_context
def push(ctx: click.Context, path: str | None) -> None:
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
    with SSHManager(config) as ssh:
        if path:
            _push_path(ssh, config, state, path)
            append_log("push", [path], ok=True, duration_ms=int((time.monotonic() - t0) * 1000), profile=profile)
        else:
            transferred = _push_all_interactive(ssh, config, state)
            append_log("push", transferred, ok=True, duration_ms=int((time.monotonic() - t0) * 1000), profile=profile)
    run_hook(config, "post_push")


def _push_path(
    ssh: SSHManager, config, state: StateManager, path: str
) -> None:
    """Push a specific file or directory."""
    rel_path = path.lstrip("/")

    # Allow both absolute local paths and paths relative to local_root.
    abs_path = Path(path).expanduser().resolve()
    if abs_path.exists():
        try:
            rel_path = str(abs_path.relative_to(config.local_root))
        except ValueError:
            pass  # not under local_root, use as-is

    local_path = config.local_root / rel_path
    if not local_path.exists():
        console.print(f"[red]✗[/] Path not found: {local_path}")
        return

    if local_path.is_file():
        console.print(f"[blue]ℹ[/] Backing up remote file...")
        success = push_single_file(ssh, config, state, rel_path)
        if success:
            url = file_to_url(config, rel_path)
            console.print(f"[green]✓[/] Done — {url}")
    else:
        # Directory — use rsync for the subtree.
        console.print(f"[blue]ℹ[/] Pushing directory: {rel_path}/")
        transferred = rsync_push_directory(config, state, rel_path)
        for f in transferred:
            url = file_to_url(config, f)
            console.print(f"  [green]✓[/] {f} → {url}")
        console.print(f"\n[green]✓[/] {len(transferred)} file(s) pushed.")


def _push_all_interactive(
    ssh: SSHManager, config, state: StateManager
) -> list[str]:
    """Full push: diff → confirm → backup → sync. Returns list of transferred files."""
    console.print("[blue]ℹ[/] Computing changes...")
    changed = rsync_push_dry_run(config)

    if not changed:
        console.print("[green]✓[/] Everything is in sync.")
        return []

    console.print(f"\n[bold]Files to push[/] ({len(changed)}):")
    for f in changed:
        console.print(f"  [cyan]{f}[/]")

    if not click.confirm(f"\nPush {len(changed)} file(s)?", default=True):
        return []

    console.print("[blue]ℹ[/] Creating server backup of changed files...")
    try:
        backup_dir = backup_remote_files(ssh, config, changed)
        console.print(f"[green]✓[/] Backup at {backup_dir}")
    except Exception as e:
        console.print(f"[yellow]⚠[/] Backup failed (continuing): {e}")

    transferred = rsync_push_all(config, state)
    console.print()
    for f in transferred:
        url = file_to_url(config, f)
        console.print(f"  [green]✓[/] {f} → {url}")
    console.print(f"\n[green]✓[/] {len(transferred)} file(s) pushed.")
    return transferred


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
    ssh = SSHManager(config)
    ssh.connect()

    MAX_RETRIES = 3
    failures: list[str] = []
    _retry_state: dict[str, dict] = {}

    def _attempt_upload(rel_path: str) -> None:
        local_path = config.local_root / rel_path
        if not local_path.exists():
            return
        t0 = time.monotonic()
        success = push_single_file(ssh, config, state, rel_path)
        duration_ms = int((time.monotonic() - t0) * 1000)
        timestamp = datetime.now().strftime("%H:%M:%S")
        if success:
            _retry_state.pop(rel_path, None)
            url = file_to_url(config, rel_path)
            console.print(f"[dim][{timestamp}][/] [green]✓[/] pushed {rel_path} → {url}")
            append_log("watch_push", [rel_path], ok=True, duration_ms=duration_ms, profile=profile)
        else:
            entry = _retry_state.get(rel_path, {"count": 0})
            count = entry["count"] + 1
            if count < MAX_RETRIES:
                delay = 2 ** count  # 2s, 4s
                console.print(
                    f"[dim][{timestamp}][/] [yellow]⚠[/] failed {rel_path} "
                    f"(retry {count}/{MAX_RETRIES - 1} in {delay}s)"
                )
                timer = threading.Timer(delay, _attempt_upload, args=[rel_path])
                _retry_state[rel_path] = {"count": count, "timer": timer}
                timer.start()
            else:
                console.print(
                    f"[dim][{timestamp}][/] [red]✗[/] gave up on {rel_path} "
                    f"after {MAX_RETRIES} attempts"
                )
                failures.append(rel_path)
                _retry_state.pop(rel_path, None)
                append_log("watch_push", [rel_path], ok=False, duration_ms=duration_ms, profile=profile)

    def on_change(rel_path: str) -> None:
        # Cancel any pending retry before attempting a fresh upload.
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
        ssh.close()
        if failures:
            console.print(
                f"\n[red]Failed uploads ({len(failures)}):[/] "
                + ", ".join(failures)
            )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show diff between local and remote — which files are out of sync."""
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)
    console.print("[blue]ℹ[/] Comparing local vs remote (this may take a moment)...")

    groups = rsync_status(config)

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


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
def backup(ctx: click.Context) -> None:
    """Trigger a full server-side backup to ~/backups/dsync/."""
    profile = ctx.obj["profile"]
    config = load_config(profile=profile)
    t0 = time.monotonic()
    with SSHManager(config) as ssh:
        backup_path = create_full_backup(ssh, config)
        console.print(f"[green]✓[/] Backup created: {backup_path}")
    append_log("backup", [], ok=True, duration_ms=int((time.monotonic() - t0) * 1000), profile=profile)


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
        rel_path = path.lstrip("/")
        # Allow absolute paths under local_root.
        abs_path = Path(path).expanduser().resolve()
        if abs_path.exists():
            try:
                rel_path = str(abs_path.relative_to(config.local_root))
            except ValueError:
                pass
        url = file_to_url(config, rel_path)
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
