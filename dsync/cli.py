"""CLI entry point — all dsync subcommands live here."""

from __future__ import annotations

import webbrowser
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import load_config
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
)
from .watcher import FileWatcher

console = Console()


@click.group()
def cli() -> None:
    """dsync — SSH deploy tool for dylansparks.com."""


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


@cli.command()
def pull() -> None:
    """Pull the full site from the server to local."""
    config = load_config()
    state = StateManager()

    if not state.is_empty():
        console.print(
            "[yellow]⚠[/] You have a local sync state. "
            "Pulling may overwrite local edits that haven't been pushed."
        )
        if not click.confirm("Continue?", default=True):
            return

    with SSHManager(config) as ssh:  # noqa: F841  (establishes connection + verifies auth)
        rsync_pull(config, state)


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path", required=False)
def push(path: str | None) -> None:
    """
    Push local changes to the server.

    With no argument, diffs local vs remote, shows the list of changed
    files, prompts for confirmation, then syncs.  With PATH, pushes only
    that specific file or directory.
    """
    config = load_config()
    state = StateManager()

    with SSHManager(config) as ssh:
        if path:
            _push_path(ssh, config, state, path)
        else:
            _push_all_interactive(ssh, config, state)


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
) -> None:
    """Full push: diff → confirm → backup → sync."""
    console.print("[blue]ℹ[/] Computing changes...")
    changed = rsync_push_dry_run(config)

    if not changed:
        console.print("[green]✓[/] Everything is in sync.")
        return

    console.print(f"\n[bold]Files to push[/] ({len(changed)}):")
    for f in changed:
        console.print(f"  [cyan]{f}[/]")

    if not click.confirm(f"\nPush {len(changed)} file(s)?", default=True):
        return

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


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@cli.command()
def watch() -> None:
    """Watch local files and auto-push on save."""
    config = load_config()
    state = StateManager()
    ssh = SSHManager(config)
    ssh.connect()

    failures: list[str] = []

    def on_change(rel_path: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        local_path = config.local_root / rel_path
        if not local_path.exists():
            return
        success = push_single_file(ssh, config, state, rel_path)
        if success:
            url = file_to_url(config, rel_path)
            console.print(
                f"[dim][{timestamp}][/] [green]✓[/] pushed {rel_path} → {url}"
            )
        else:
            console.print(f"[dim][{timestamp}][/] [red]✗[/] failed  {rel_path}")
            failures.append(rel_path)

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
def status() -> None:
    """Show diff between local and remote — which files are out of sync."""
    config = load_config()
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
def backup() -> None:
    """Trigger a full server-side backup to ~/backups/dsync/."""
    config = load_config()
    with SSHManager(config) as ssh:
        backup_path = create_full_backup(ssh, config)
        console.print(f"[green]✓[/] Backup created: {backup_path}")


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


@cli.command("open")
@click.argument("path", required=False)
def open_url(path: str | None) -> None:
    """Open the live URL for a local file path in the default browser."""
    config = load_config()

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
