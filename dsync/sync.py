"""Sync operations: rsync wrappers and single-file SFTP transfers."""

from __future__ import annotations

import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import Config
from .ssh import SSHManager, get_rsync_env
from .state import StateManager

console = Console()


# ---------------------------------------------------------------------------
# SSH / rsync command helpers
# ---------------------------------------------------------------------------


def _ssh_cmd(config: Config) -> str:
    """Build the SSH command string used by rsync's -e flag."""
    return (
        f"ssh -p {config.port} "
        f"-i {shlex.quote(str(config.key_path))} "
        f"-o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-o LogLevel=ERROR"
    )


def _exclude_flags(patterns: list[str]) -> list[str]:
    """Convert a list of ignore patterns to rsync --exclude arguments."""
    args: list[str] = []
    for p in patterns:
        args.extend(["--exclude", p])
    return args


def _run_rsync(
    config: Config,
    src: str,
    dst: str,
    extra_flags: Optional[list[str]] = None,
    dry_run: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run an rsync command, returning the CompletedProcess result."""
    cmd = (
        ["rsync", "-az", "--checksum", "--itemize-changes"]
        + (["--dry-run"] if dry_run else [])
        + (extra_flags or [])
        + ["-e", _ssh_cmd(config)]
        + _exclude_flags(config.ignore_patterns)
        + [src, dst]
    )
    env = get_rsync_env(config.key_path, config=config)
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Itemize output parsing
# ---------------------------------------------------------------------------


def _parse_itemize(output: str) -> tuple[list[str], list[str]]:
    """
    Parse rsync --itemize-changes output.

    Returns (transfers, deletions) where:
    - transfers: relative paths of files that were/would be transferred.
    - deletions: relative paths of files that were/would be deleted.
    """
    transfers: list[str] = []
    deletions: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        if line.startswith("*deleting"):
            parts = line.split(None, 1)
            path = parts[1].strip() if len(parts) == 2 else ""
            if path:
                deletions.append(path)
        elif len(line) > 12 and line[0] in ("<", ">", "c", "h") and line[1] != "d":
            path = line[12:].strip()
            if path and path != "./":
                transfers.append(path)
    return transfers, deletions


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def rsync_pull(config: Config, state: StateManager) -> None:
    """Pull the full site from the server to local using rsync."""
    remote_src = f"{config.user}@{config.host}:{config.remote_root}"
    local_dst = str(config.local_root) + "/"

    console.print("[blue]ℹ[/] Pulling site...")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task("Syncing from server...", total=None)
        result = _run_rsync(config, remote_src, local_dst)

    if result.returncode != 0:
        console.print(f"[red]✗[/] rsync failed:\n{result.stderr}")
        return

    transfers, deletions = _parse_itemize(result.stdout)
    for f in transfers:
        console.print(f"  [green]↓[/] {f}")
    for f in deletions:
        console.print(f"  [red]✗[/] deleted locally: {f}")
    console.print(f"\n[green]✓[/] {len(transfers)} updated, {len(deletions)} deleted")

    state.scan_directory(config.local_root, config.ignore_patterns)
    state.save()


# ---------------------------------------------------------------------------
# Push (bulk)
# ---------------------------------------------------------------------------


def rsync_push_dry_run(config: Config) -> list[str]:
    """
    Dry-run rsync from local to remote.
    Returns the list of file paths that would be transferred.
    """
    local_src = str(config.local_root) + "/"
    remote_dst = f"{config.user}@{config.host}:{config.remote_root}"
    result = _run_rsync(config, local_src, remote_dst, dry_run=True)
    if result.returncode != 0:
        console.print(f"[red]✗[/] rsync dry-run failed:\n{result.stderr}")
        return []
    transfers, _ = _parse_itemize(result.stdout)
    return transfers


def rsync_push_all(config: Config, state: StateManager) -> list[str]:
    """
    Push all local changes to the server.
    Returns the list of files that were transferred.
    """
    local_src = str(config.local_root) + "/"
    remote_dst = f"{config.user}@{config.host}:{config.remote_root}"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task("Syncing to server...", total=None)
        result = _run_rsync(config, local_src, remote_dst)

    if result.returncode != 0:
        console.print(f"[red]✗[/] rsync failed:\n{result.stderr}")
        return []

    transfers, _ = _parse_itemize(result.stdout)
    state.scan_directory(config.local_root, config.ignore_patterns)
    state.save()
    return transfers


def rsync_push_directory(
    config: Config, state: StateManager, rel_dir: str
) -> list[str]:
    """
    Push a local subdirectory to the server.
    Returns the list of files transferred.
    """
    local_src = str(config.local_root / rel_dir) + "/"
    remote_dst = f"{config.user}@{config.host}:{config.remote_root}{rel_dir}/"
    result = _run_rsync(config, local_src, remote_dst)
    if result.returncode != 0:
        console.print(f"[red]✗[/] rsync failed:\n{result.stderr}")
        return []
    transfers, _ = _parse_itemize(result.stdout)
    for local_file in (config.local_root / rel_dir).rglob("*"):
        if local_file.is_file():
            rel = str(local_file.relative_to(config.local_root))
            state.update(rel, local_file)
    state.save()
    return [f"{rel_dir}/{f}" for f in transfers]


# ---------------------------------------------------------------------------
# Single-file push via SFTP
# ---------------------------------------------------------------------------


def push_single_file(
    ssh: SSHManager,
    config: Config,
    state: StateManager,
    rel_path: str,
) -> bool:
    """
    Upload a single local file to the server via SFTP.

    Creates a remote backup first. Returns True on success.
    """
    local_path = config.local_root / rel_path
    if not local_path.exists():
        console.print(f"[red]✗[/] Local file not found: {local_path}")
        return False

    remote_path = config.remote_root + rel_path

    # Backup the existing remote file.
    try:
        _backup_remote_file(ssh, config, rel_path)
    except Exception as e:
        console.print(f"[yellow]⚠[/] Backup skipped: {e}")

    # Ensure the remote directory exists.
    remote_dir = str(Path(remote_path).parent)
    try:
        ssh.run(f"mkdir -p {shlex.quote(remote_dir)}")
    except Exception as e:
        console.print(f"[red]✗[/] Could not create remote directory: {e}")
        return False

    # Upload via SFTP.
    try:
        ssh.sftp.put(str(local_path), remote_path)
        state.update(rel_path, local_path)
        state.save()
        return True
    except Exception as e:
        console.print(f"[red]✗[/] Upload failed for {rel_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def rsync_status(config: Config) -> dict[str, list[str]]:
    """
    Compare local and remote, grouping files by sync status.

    Returns a dict with keys:
    - local_newer:  local file differs from / is newer than remote.
    - remote_newer: remote file differs from / is newer than local.
    - local_only:   exists locally, not on remote.
    - remote_only:  exists on remote, not locally.
    """
    local_src = str(config.local_root) + "/"
    remote = f"{config.user}@{config.host}:{config.remote_root}"

    # push dry-run (local → remote, with --delete)
    push_result = _run_rsync(
        config, local_src, remote, extra_flags=["--delete"], dry_run=True
    )
    # pull dry-run (remote → local, with --delete)
    pull_result = _run_rsync(
        config, remote, local_src, extra_flags=["--delete"], dry_run=True
    )

    push_transfers, push_deletions = _parse_itemize(
        push_result.stdout if push_result.returncode == 0 else ""
    )
    pull_transfers, pull_deletions = _parse_itemize(
        pull_result.stdout if pull_result.returncode == 0 else ""
    )

    push_set = set(push_transfers)
    pull_set = set(pull_transfers)

    # *deleting in push dry-run = remote has file, local doesn't → remote_only
    # *deleting in pull dry-run = local has file, remote doesn't → local_only
    return {
        "local_newer": sorted(push_set - pull_set),
        "remote_newer": sorted(pull_set - push_set),
        "local_only": sorted(pull_deletions),
        "remote_only": sorted(push_deletions),
    }


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------


def _remote_backup_base(config: Config) -> str:
    """Expand the remote backup dir to an absolute path."""
    return config.backup_dir.replace("~", f"/home/{config.user}")


def _backup_remote_file(ssh: SSHManager, config: Config, rel_path: str) -> None:
    """Copy a single remote file to the timestamped backup directory."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_base = _remote_backup_base(config)
    safe_name = rel_path.replace("/", "_")
    backup_path = f"{backup_base}/{timestamp}_{safe_name}"
    remote_file = config.remote_root + rel_path
    ssh.run(f"mkdir -p {shlex.quote(backup_base)}")
    ssh.run(
        f"cp {shlex.quote(remote_file)} {shlex.quote(backup_path)} 2>/dev/null || true",
        check=False,
    )


def backup_remote_files(ssh: SSHManager, config: Config, rel_paths: list[str]) -> str:
    """
    Back up a specific set of remote files before overwriting them.

    Creates a timestamped directory under the remote backup base and
    copies each file there. Returns the backup directory path.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_base = _remote_backup_base(config)
    backup_dir = f"{backup_base}/{timestamp}"
    ssh.run(f"mkdir -p {shlex.quote(backup_dir)}")
    for rel_path in rel_paths:
        remote_file = config.remote_root + rel_path
        safe_name = rel_path.replace("/", "_")
        dest = f"{backup_dir}/{safe_name}"
        ssh.run(
            f"cp {shlex.quote(remote_file)} {shlex.quote(dest)} 2>/dev/null || true",
            check=False,
        )
    return backup_dir


def create_full_backup(ssh: SSHManager, config: Config) -> str:
    """
    Create a full tar.gz backup of the remote site.

    Stores the archive under the remote backup directory with a
    timestamp in the filename. Returns the remote path of the archive.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_base = _remote_backup_base(config)
    backup_path = f"{backup_base}/{timestamp}.tar.gz"
    ssh.run(f"mkdir -p {shlex.quote(backup_base)}")
    console.print("[blue]ℹ[/] Archiving remote site (this may take a moment)...")
    ssh.run(
        f"tar -czf {shlex.quote(backup_path)} -C {shlex.quote(config.remote_root)} ."
    )
    return backup_path


# ---------------------------------------------------------------------------
# Hook runner
# ---------------------------------------------------------------------------


def run_hook(config: Config, hook: str) -> bool:
    """
    Run a named hook command from config (e.g. 'pre_push', 'post_push').

    The command is executed in a shell with the local_root as the working
    directory. Returns True if the hook succeeded (or was not configured).
    """
    cmd = config.hooks.get(hook)
    if not cmd:
        return True

    console.print(f"[blue]ℹ[/] Running hook [bold]{hook}[/]: {cmd}")
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=str(config.local_root),
        text=True,
    )
    if result.returncode != 0:
        console.print(
            f"[red]✗[/] Hook [bold]{hook}[/] failed (exit {result.returncode})"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------


def file_to_url(config: Config, rel_path: str) -> str:
    """
    Translate a relative file path to its live URL.

    Examples:
        index.html          → https://dylansparks.com/
        aboutme/index.html  → https://dylansparks.com/aboutme/
        css/style.css       → https://dylansparks.com/css/style.css
    """
    if rel_path == "index.html":
        url_path = "/"
    elif rel_path.endswith("/index.html"):
        url_path = rel_path[: -len("index.html")]
    else:
        url_path = rel_path
    return f"{config.site_url}/{url_path.lstrip('/')}"
