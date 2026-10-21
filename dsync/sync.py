"""Sync operations: rsync wrappers and single-file SFTP transfers."""

from __future__ import annotations

import posixpath
import re
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import Config
from .ssh import SSHManager, get_rsync_env
from .state import StateManager, _matches_ignore

console = Console()


class SyncError(RuntimeError):
    """An rsync or remote operation failed; the message is user-facing."""


# ---------------------------------------------------------------------------
# SSH / rsync command helpers
# ---------------------------------------------------------------------------


def _ssh_cmd(config: Config) -> str:
    """Build the SSH command string used by rsync's -e flag.

    Host keys are verified against ~/.ssh/known_hosts. An unknown host is
    recorded on first contact (``accept-new``); a *changed* key is refused.
    """
    return (
        f"ssh -p {config.port} "
        f"-i {shlex.quote(str(config.key_path))} "
        f"-o StrictHostKeyChecking=accept-new "
        f"-o LogLevel=ERROR"
    )


def is_safe_rel_path(rel_path: str) -> bool:
    """True if *rel_path* is relative and cannot climb out of the site root.

    Remote targets are built as ``remote_root + rel_path``, so a ``..`` or a
    leading ``/`` here would write outside the web root.
    """
    if not rel_path or rel_path.startswith("/"):
        return False
    return ".." not in rel_path.split("/")


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
        # --protect-args keeps paths with spaces intact on the remote side.
        ["rsync", "-az", "--checksum", "--itemize-changes", "--protect-args"]
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
        check=False,
    )


def _check_rsync(result: subprocess.CompletedProcess[str], what: str) -> None:
    """Raise SyncError if an rsync run failed."""
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise SyncError(
            f"{what} failed (rsync exit {result.returncode})"
            + (f":\n{detail}" if detail else "")
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


def rsync_pull(config: Config, state: StateManager) -> list[str]:
    """Pull the full site from the server to local using rsync.

    Returns the files that were updated. Raises SyncError if rsync fails.
    """
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

    _check_rsync(result, "Pull")

    transfers, deletions = _parse_itemize(result.stdout)
    for f in transfers:
        console.print(f"  [green]↓[/] {f}")
    for f in deletions:
        console.print(f"  [red]✗[/] deleted locally: {f}")
    console.print(f"\n[green]✓[/] {len(transfers)} updated, {len(deletions)} deleted")

    state.scan_directory(config.local_root, config.ignore_patterns)
    state.save()
    return transfers


# ---------------------------------------------------------------------------
# Push (bulk)
# ---------------------------------------------------------------------------


def rsync_push_dry_run(config: Config) -> list[str]:
    """
    Dry-run rsync from local to remote.
    Returns the list of file paths that would be transferred. Raises
    SyncError if rsync fails -- an empty list always means "nothing to push".
    """
    local_src = str(config.local_root) + "/"
    remote_dst = f"{config.user}@{config.host}:{config.remote_root}"
    result = _run_rsync(config, local_src, remote_dst, dry_run=True)
    _check_rsync(result, "Push dry-run")
    transfers, _ = _parse_itemize(result.stdout)
    return transfers


def rsync_push_all(config: Config, state: StateManager) -> list[str]:
    """
    Push all local changes to the server.
    Returns the list of files that were transferred. Raises SyncError on failure.
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

    _check_rsync(result, "Push")

    transfers, _ = _parse_itemize(result.stdout)
    state.scan_directory(config.local_root, config.ignore_patterns)
    state.save()
    return transfers


def rsync_push_directory(
    config: Config, state: StateManager, rel_dir: str
) -> list[str]:
    """
    Push a local subdirectory to the server.
    Returns the list of files transferred. Raises SyncError on failure.
    """
    if not is_safe_rel_path(rel_dir):
        raise SyncError(f"Refusing path outside the site root: {rel_dir}")
    local_src = str(config.local_root / rel_dir) + "/"
    remote_dst = f"{config.user}@{config.host}:{config.remote_root}{rel_dir}/"
    result = _run_rsync(config, local_src, remote_dst)
    _check_rsync(result, f"Push of {rel_dir}/")
    transfers, _ = _parse_itemize(result.stdout)
    for local_file in (config.local_root / rel_dir).rglob("*"):
        if not local_file.is_file():
            continue
        rel = local_file.relative_to(config.local_root).as_posix()
        # Match scan_directory: ignored files are never synced, so never tracked.
        if _matches_ignore(rel, config.ignore_patterns):
            continue
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
    if not is_safe_rel_path(rel_path):
        console.print(f"[red]✗[/] Refusing path outside the site root: {rel_path}")
        return False
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
    remote_dir = posixpath.dirname(remote_path)
    try:
        ssh.run(f"mkdir -p {shlex.quote(remote_dir)}")
    except Exception as e:
        console.print(f"[red]✗[/] Could not create remote directory: {e}")
        return False

    try:
        ssh.sftp.put(str(local_path), remote_path)
    except Exception as e:
        console.print(f"[red]✗[/] Upload failed for {rel_path}: {e}")
        return False

    # The file is live from here on, so nothing below may report failure
    # (watch would retry, and the log would record a deploy that happened as
    # failed).

    # Carry the local mtime over as rsync -a would, so `status` can tell which
    # side is newer. Some SFTP servers refuse SETSTAT; that is cosmetic.
    try:
        st = local_path.stat()
        ssh.sftp.utime(remote_path, (st.st_atime, st.st_mtime))
    except Exception as e:
        console.print(f"[yellow]⚠[/] Uploaded {rel_path}, but could not set mtime: {e}")

    try:
        state.update(rel_path, local_path)
        state.save()
    except Exception as e:
        console.print(
            f"[yellow]⚠[/] Uploaded {rel_path}, but could not update sync state: {e}"
        )
    return True


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def rsync_status(config: Config, ssh: SSHManager) -> dict[str, list[str]]:
    """
    Compare local and remote, grouping files by sync status.

    Returns a dict with keys:
    - local_newer:  exists on both sides, differs, local mtime is newer.
    - remote_newer: exists on both sides, differs, remote mtime is newer.
    - local_only:   exists locally, not on remote.
    - remote_only:  exists on remote, not locally.

    Raises SyncError if either comparison fails, rather than reporting the
    site as in sync.
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

    _check_rsync(push_result, "Status comparison (local → remote)")
    _check_rsync(pull_result, "Status comparison (remote → local)")

    push_transfers, push_deletions = _parse_itemize(push_result.stdout)
    pull_transfers, pull_deletions = _parse_itemize(pull_result.stdout)

    # A file that exists on both sides with differing content is listed by BOTH
    # dry-runs (each direction would update the other), so rsync alone can't say
    # which side is newer. Resolve direction by comparing mtimes.
    differing = set(push_transfers) & set(pull_transfers)
    local_newer: list[str] = []
    remote_newer: list[str] = []
    for rel in differing:
        local_mtime = _local_mtime(config, rel)
        remote_mtime = _remote_mtime(ssh, config, rel)
        if remote_mtime is not None and remote_mtime > local_mtime:
            remote_newer.append(rel)
        else:
            local_newer.append(rel)

    # *deleting in push dry-run = remote has file, local doesn't → remote_only
    # *deleting in pull dry-run = local has file, remote doesn't → local_only
    return {
        "local_newer": sorted(local_newer),
        "remote_newer": sorted(remote_newer),
        "local_only": sorted(pull_deletions),
        "remote_only": sorted(push_deletions),
    }


def _local_mtime(config: Config, rel_path: str) -> float:
    """Return the local file's mtime, or 0.0 if it can't be read."""
    try:
        return (config.local_root / rel_path).stat().st_mtime
    except OSError:
        return 0.0


def _remote_mtime(ssh: SSHManager, config: Config, rel_path: str) -> Optional[float]:
    """Return the remote file's mtime via SFTP, or None if it can't be read."""
    try:
        attr = ssh.sftp.stat(config.remote_root + rel_path)
        return attr.st_mtime
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------


# Cache of the resolved remote $HOME, keyed by "user@host", to avoid an extra
# round-trip on every backup call (notably during `watch`).
_remote_home_cache: dict[str, str] = {}


def _remote_backup_base(ssh: SSHManager, config: Config) -> str:
    """Expand the remote backup dir to an absolute path.

    A leading ``~`` is expanded using the remote account's actual ``$HOME``
    rather than assuming ``/home/<user>``.
    """
    base = config.backup_dir
    if base == "~" or base.startswith("~/"):
        cache_key = f"{config.user}@{config.host}"
        home = _remote_home_cache.get(cache_key)
        if home is None:
            out, _ = ssh.run("printf '%s' \"$HOME\"")
            home = out.strip() or f"/home/{config.user}"
            _remote_home_cache[cache_key] = home
        base = home + base[1:]
    return base


def _timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d_%H-%M-%S")


def _backup_cmd(src: str, dest: str) -> str:
    """Shell command that copies *src* to *dest*, mirroring its directory.

    A missing *src* is a new file with nothing to back up, so that case exits
    0. A *dest* that already exists (two saves within one second) is kept, so
    the oldest version wins. Any other failure exits non-zero.
    """
    q_src, q_dest = shlex.quote(src), shlex.quote(dest)
    q_dir = shlex.quote(posixpath.dirname(dest))
    return (
        f"if [ -e {q_src} ] && [ ! -e {q_dest} ]; then "
        f"mkdir -p {q_dir} && cp -p {q_src} {q_dest}; fi"
    )


def _backup_remote_file(ssh: SSHManager, config: Config, rel_path: str) -> None:
    """Copy a single remote file into a timestamped backup directory.

    Raises RuntimeError if the copy fails.
    """
    backup_dir = f"{_remote_backup_base(ssh, config)}/{_timestamp()}"
    ssh.run(_backup_cmd(config.remote_root + rel_path, f"{backup_dir}/{rel_path}"))


def backup_remote_files(
    ssh: SSHManager, config: Config, rel_paths: list[str]
) -> tuple[str, list[str]]:
    """
    Back up a specific set of remote files before overwriting them.

    Copies each file into a timestamped directory under the remote backup
    base, preserving its relative path. Returns ``(backup_dir, failed)``
    where *failed* lists the files that could not be backed up.
    """
    backup_dir = f"{_remote_backup_base(ssh, config)}/{_timestamp()}"
    ssh.run(f"mkdir -p {shlex.quote(backup_dir)}")
    failed: list[str] = []
    for rel_path in rel_paths:
        try:
            ssh.run(
                _backup_cmd(config.remote_root + rel_path, f"{backup_dir}/{rel_path}")
            )
        except RuntimeError:
            failed.append(rel_path)
    return backup_dir, failed


def create_full_backup(ssh: SSHManager, config: Config) -> str:
    """
    Create a full tar.gz backup of the remote site.

    Stores the archive under the remote backup directory with a
    timestamp in the filename. Returns the remote path of the archive.
    """
    backup_base = _remote_backup_base(ssh, config)
    backup_path = f"{backup_base}/{_timestamp()}.tar.gz"
    ssh.run(f"mkdir -p {shlex.quote(backup_base)}")
    console.print("[blue]ℹ[/] Archiving remote site (this may take a moment)...")
    ssh.run(
        f"tar -czf {shlex.quote(backup_path)} -C {shlex.quote(config.remote_root)} ."
    )
    return backup_path


# Names dsync gives top-level entries in the backup dir: a timestamped directory
# (per-push and per-save backups), a full-site ``<timestamp>.tar.gz``, or a
# ``<timestamp>_<name>`` file from versions before per-save backups mirrored
# the tree. Pruning only ever touches entries matching this.
_BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(\.tar\.gz|_.+)?$")


def find_old_backups(ssh: SSHManager, config: Config, keep_days: int) -> list[str]:
    """List dsync backups in the remote backup dir older than *keep_days* days.

    Only top-level entries whose names match dsync's timestamp format are
    returned, so nothing else that happens to live in that directory is
    ever a candidate for deletion.
    """
    if keep_days < 1:
        raise SyncError("Backup retention must be at least 1 day.")
    base = posixpath.normpath(_remote_backup_base(ssh, config))
    if base in ("/", "."):
        raise SyncError(f"Refusing to prune backups in {base!r}.")
    q_base = shlex.quote(base)
    out, _ = ssh.run(
        f"[ -d {q_base} ] || exit 0; "
        f"find {q_base} -mindepth 1 -maxdepth 1 -mtime +{int(keep_days)} -print"
    )
    old: list[str] = []
    for line in out.splitlines():
        path = line.strip()
        if posixpath.dirname(path) == base and _BACKUP_NAME_RE.match(
            posixpath.basename(path)
        ):
            old.append(path)
    return sorted(old)


def delete_backups(ssh: SSHManager, paths: list[str]) -> None:
    """Delete backup entries previously returned by find_old_backups."""
    for path in paths:
        if not _BACKUP_NAME_RE.match(posixpath.basename(path)):
            raise SyncError(f"Refusing to delete non-backup path: {path}")
        ssh.run(f"rm -rf -- {shlex.quote(path)}")


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
        check=False,
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
