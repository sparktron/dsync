"""Configuration management for dsync."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Prompt

console = Console()

CONFIG_DIR = Path.home() / ".dsync"
CONFIG_FILE = CONFIG_DIR / "config.json"
PROFILES_DIR = CONFIG_DIR / "profiles"

# The config may hold the SSH key passphrase, so it gets the same treatment as
# the private key it unlocks: owner-only, and never group- or world-readable.
DIR_MODE = 0o700
FILE_MODE = 0o600

DEFAULT_IGNORE: list[str] = [
    ".git/",
    "images/",
    "lscache/",
    "*.gz",
    "*.zip",
    ".DS_Store",
    "*~",
    "*.swp",
    "__pycache__/",
    ".dsync_state",
]


class Config:
    """Holds all dsync configuration values."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.host: str = data["host"]
        self.port: int = int(data["port"])
        self.user: str = data["user"]
        self.key_path: Path = Path(data["key_path"]).expanduser()
        self.local_root: Path = Path(data["local_root"]).expanduser()
        self.remote_root: str = data["remote_root"].rstrip("/") + "/"
        self.site_url: str = data["site_url"].rstrip("/")
        self.backup_dir: str = data.get("backup_dir", "~/backups/dsync")
        self.ignore_patterns: list[str] = data.get("ignore_patterns", DEFAULT_IGNORE)
        self.hooks: dict[str, str] = data.get("hooks", {})
        self.passphrase: str | None = data.get("passphrase", None)

    def to_dict(self) -> dict[str, Any]:
        """Serialize config to a JSON-compatible dict."""
        data = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "key_path": str(self.key_path),
            "local_root": str(self.local_root),
            "remote_root": self.remote_root,
            "site_url": self.site_url,
            "backup_dir": self.backup_dir,
            "ignore_patterns": self.ignore_patterns,
            "hooks": self.hooks,
        }
        if self.passphrase is not None:
            data["passphrase"] = self.passphrase
        return data


def _config_file(profile: str | None) -> Path:
    """Return the config file path for the given profile.

    ``None`` and the reserved name ``"default"`` both resolve to the legacy
    ``~/.dsync/config.json`` so that existing installs are unaffected when
    users pass ``--profile default``.
    """
    if profile is None or profile == "default":
        return CONFIG_FILE
    return PROFILES_DIR / f"{profile}.json"


def _too_permissive(path: Path) -> int | None:
    """Return the current mode if *path* is group/other-accessible, else None."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    return mode if mode & 0o077 else None


def _ensure_private_dir(path: Path) -> None:
    """Create *path* (and ~/.dsync above it) owner-only."""
    for d in (CONFIG_DIR, path):
        d.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        # mkdir's mode is masked by umask, and is ignored entirely when the
        # directory already exists -- so set it explicitly.
        if _too_permissive(d) is not None:
            try:
                d.chmod(DIR_MODE)
            except OSError:
                pass


def _repair_permissions(path: Path) -> None:
    """Tighten a config file that holds a passphrase but is readable by others.

    Versions before this check wrote the file with the default umask, so an
    existing install can have the passphrase sitting in a 0644 file.
    """
    mode = _too_permissive(path)
    if mode is None:
        return
    try:
        path.chmod(FILE_MODE)
    except OSError:
        console.print(
            f"[yellow]⚠[/] {path} holds a saved passphrase and is readable by "
            f"other users (mode {mode:04o}), and the permissions could not be "
            f"changed. Fix it with: chmod 600 {path}"
        )
        return
    console.print(
        f"[yellow]⚠[/] {path} held a saved passphrase with mode {mode:04o} — "
        f"tightened to 0600. Other accounts on this machine could have read it, "
        f"so consider rotating the key passphrase."
    )


def load_config(profile: str | None = None) -> Config:
    """Load config from disk, running the first-run wizard if not found."""
    path = _config_file(profile)
    if not path.exists():
        return run_wizard(profile=profile)
    with path.open() as f:
        data = json.load(f)
    config = Config(data)
    if config.passphrase is not None:
        _repair_permissions(path)
    return config


def save_config(config: Config, profile: str | None = None) -> None:
    """Persist config to disk, owner-readable only.

    Written to a 0600 temp file in the destination directory and renamed into
    place: writing in place would leave a window where a saved passphrase sits
    in a file created under the caller's umask.
    """
    path = _config_file(profile)
    _ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        os.fchmod(fd, FILE_MODE)
        handle = os.fdopen(fd, "w")
    except BaseException:
        # fdopen did not take ownership of the descriptor, so close it here.
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        raise
    try:
        with handle as f:
            json.dump(config.to_dict(), f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def list_profiles() -> list[str]:
    """Return the names of all available profiles."""
    names: list[str] = []
    if CONFIG_FILE.exists():
        names.append("default")
    if PROFILES_DIR.exists():
        for f in sorted(PROFILES_DIR.glob("*.json")):
            names.append(f.stem)
    return names


def run_wizard(profile: str | None = None) -> Config:
    """Interactive first-run configuration wizard."""
    profile_label = f"[dim]({profile})[/] " if profile else ""
    console.print(f"\n[bold blue]dsync[/] {profile_label}— first-run setup\n")
    path = _config_file(profile)
    console.print(f"No config found at [dim]{path}[/]. Let's set one up.\n")

    host = Prompt.ask("SSH host", default="dubstep.cleannameservers.com")
    port = int(Prompt.ask("SSH port", default="50288"))
    user = Prompt.ask("SSH user", default="dylanspa")
    key_path = Prompt.ask(
        "Path to SSH private key",
        default="~/Documents/dylansparks.com/id_rsa",
    )
    local_root = Prompt.ask(
        "Local project root",
        default="~/Documents/dylansparks.com/public_html/",
    )
    remote_root = Prompt.ask(
        "Remote web root",
        default="/home/dylanspa/public_html/",
    )
    site_url = Prompt.ask("Live site URL", default="https://dylansparks.com")
    backup_dir = Prompt.ask("Remote backup directory", default="~/backups/dsync")

    data: dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "key_path": key_path,
        "local_root": local_root,
        "remote_root": remote_root,
        "site_url": site_url,
        "backup_dir": backup_dir,
        "ignore_patterns": DEFAULT_IGNORE,
    }

    config = Config(data)
    save_config(config, profile=profile)
    console.print(f"\n[green]✓[/] Config saved to [dim]{path}[/]\n")
    return config
