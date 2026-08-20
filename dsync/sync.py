"""Sync operations: rsync wrappers and single-file SFTP transfers."""

from __future__ import annotations

import posixpath
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import Config
from .ssh import SSHManager, get_rsync_env
from .state import StateManager

console = Console()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RsyncError(RuntimeError):
    """An rsync invocation failed hard enough that its output means nothing."""

    def __init__(self, action: str, returncode: int | None, stderr: str = "") -> None:
        self.action = action
        self.returncode = returncode
        self.stderr = stderr.strip()
        code = f" (rsync exit {returncode})" if returncode is not None else ""
        detail = f"\n{self.stderr}" if self.stderr else ""
        super().__init__(f"{action} failed{code}{detail}")


class PathOutsideRoot(ValueError):
    """A path resolved outside the configured local or remote root."""


# ---------------------------------------------------------------------------
# Path confinement
#
# Everything dsync writes must land under remote_root, and everything it reads
# must come from under local_root. `lstrip("/")` does not strip `..`, so without
# these a mistyped `dsync push ../secrets.env` uploaded a file from outside the
# project to outside the web root, with no confirmation.
# ---------------------------------------------------------------------------


def _within(path: Path, root: Path) -> bool:
    """True if *path* is *root* or sits underneath it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def relative_to_root(config: Config, path: str) -> str:
    """Resolve a user-supplied path to a POSIX path relative to local_root.

    Accepts an absolute filesystem path, a path relative to local_root, a path
    relative to the current directory, or a leading-slash site path such as
    ``/aboutme/index.html``. Raises PathOutsideRoot if the result would fall
    outside local_root.
    """
    root = config.local_root
    candidate = Path(path).expanduser()

    if candidate.is_absolute():
        resolved = candidate.resolve()
        if not _within(resolved, root) and not resolved.exists():
            # A leading slash can also mean "relative to the site root", the way
            # a URL path does: `dsync open /aboutme/index.html`. Only fall back
            # to that reading when no such file exists on disk — if it does, the
            # user meant that file, and it belongs to a different project.
            resolved = (root / candidate.relative_to(candidate.anchor)).resolve()
    else:
        # Read against local_root first, so `dsync push css/style.css` works from
        # any directory; fall back to a cwd-relative reading only if that is the
        # one that exists.
        from_root = (root / candidate).resolve()
        from_cwd = (Path.cwd() / candidate).resolve()
        resolved = from_root
        if not from_root.exists() and from_cwd.exists():
            resolved = from_cwd

    if not _within(resolved, root):
        raise PathOutsideRoot(
            f"{path!r} resolves to {resolved}, which is outside the project root {root}"
        )

    rel = resolved.relative_to(root)
    return "" if rel == Path(".") else rel.as_posix()


def remote_path_for(config: Config, rel_path: str) -> str:
    """Join a root-relative path onto remote_root, refusing to escape it."""
    root = config.remote_root  # normalised by Config to end in "/"
    joined = posixpath.normpath(root + rel_path)
    if joined != root.rstrip("/") and not joined.startswith(root):
        raise PathOutsideRoot(
            f"{rel_path!r} resolves to {joined}, which is outside the remote root {root}"
        )
    return joined


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
    extra_flags: list[str] | None = None,
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
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RsyncError(
            "Running rsync", None, "rsync is not installed or not on PATH"
        ) from exc


# rsync still produces usable output when some files could not be transferred
# (23) or vanished mid-scan (24). Every other non-zero exit means the run tells
# us nothing — and empty output must never be mistaken for "no differences".
_RSYNC_PARTIAL_CODES = (23, 24)


def _check_rsync(result: subprocess.CompletedProcess[str], action: str) -> bool:
    """Raise RsyncError unless the run is usable. Returns True if it was partial.

    Callers must route every rsync result through this. Treating a failed run as
    an empty result is what let `dsync status` report "everything is in sync"
    when the comparison never actually happened.
    """
    if result.returncode == 0:
        return False
    if result.returncode in _RSYNC_PARTIAL_CODES:
        console.print(
            f"[yellow]⚠[/] {action}: rsync reported a partial run "
            f"(exit {result.returncode}); results may be incomplete."
        )
        return True
    raise RsyncError(action, result.returncode, result.stderr or "")


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

    _check_rsync(result, "Pull")

    # Pull runs without --delete, so rsync never reports deletions here.
    transfers, _ = _parse_itemize(result.stdout)
    for f in transfers:
        console.print(f"  [green]↓[/] {f}")
    console.print(f"\n[green]✓[/] {len(transfers)} updated")

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
    _check_rsync(result, "Push dry-run")
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
    Returns the list of files transferred.
    """
    local_src = str(config.local_root / rel_dir) + "/"
    remote_dst = f"{config.user}@{config.host}:{remote_path_for(config, rel_dir)}/"
    result = _run_rsync(config, local_src, remote_dst)
    _check_rsync(result, f"Push of {rel_dir}/")
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

    remote_path = remote_path_for(config, rel_path)

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


def rsync_status(config: Config, ssh: SSHManager) -> dict[str, list[str]]:
    """
    Compare local and remote, grouping files by sync status.

    Returns a dict with keys:
    - local_newer:  exists on both sides, differs, local mtime is newer.
    - remote_newer: exists on both sides, differs, remote mtime is newer.
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

    # Both probes must have actually run. Substituting empty output for a failed
    # comparison makes a network or auth failure look identical to a clean tree.
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


def _remote_mtime(ssh: SSHManager, config: Config, rel_path: str) -> float | None:
    """Return the remote file's mtime via SFTP, or None if it can't be read."""
    try:
        attr = ssh.sftp.stat(remote_path_for(config, rel_path))
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


def _backup_remote_file(ssh: SSHManager, config: Config, rel_path: str) -> None:
    """Copy a single remote file to the timestamped backup directory."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_base = _remote_backup_base(ssh, config)
    safe_name = rel_path.replace("/", "_")
    backup_path = f"{backup_base}/{timestamp}_{safe_name}"
    remote_file = remote_path_for(config, rel_path)
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
    backup_base = _remote_backup_base(ssh, config)
    backup_dir = f"{backup_base}/{timestamp}"
    ssh.run(f"mkdir -p {shlex.quote(backup_dir)}")
    for rel_path in rel_paths:
        remote_file = remote_path_for(config, rel_path)
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
    backup_base = _remote_backup_base(ssh, config)
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
