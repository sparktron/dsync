"""Configuration management for dsync."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt

console = Console()

CONFIG_DIR = Path.home() / ".dsync"
CONFIG_FILE = CONFIG_DIR / "config.json"
PROFILES_DIR = CONFIG_DIR / "profiles"

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
        # Resolved, not merely expanded: every path comparison in the tool comes
        # from Path.resolve(), so an unresolved root with a symlink component
        # made relative_to() fail for files that genuinely are in the project.
        self.local_root: Path = Path(data["local_root"]).expanduser().resolve()
        self.remote_root: str = data["remote_root"].rstrip("/") + "/"
        self.site_url: str = data["site_url"].rstrip("/")
        self.backup_dir: str = data.get("backup_dir", "~/backups/dsync")
        self.ignore_patterns: list[str] = data.get("ignore_patterns", DEFAULT_IGNORE)
        self.hooks: dict[str, str] = data.get("hooks", {})
        # Legacy field: older versions stored the SSH key passphrase here in
        # cleartext. It is still read so existing configs keep working, but it is
        # never written back (see to_dict) and migrate_stored_passphrase removes
        # it on first use.
        self.passphrase: str | None = data.get("passphrase")

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
        # The passphrase is deliberately not serialised — it belongs in the
        # ssh-agent, not in a JSON file on disk.
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


def load_config(profile: str | None = None) -> Config:
    """Load config from disk, running the first-run wizard if not found."""
    path = _config_file(profile)
    if not path.exists():
        return run_wizard(profile=profile)
    with path.open() as f:
        data = json.load(f)
    config = Config(data)
    migrate_stored_passphrase(config, profile=profile)
    return config


def save_config(config: Config, profile: str | None = None) -> None:
    """Persist config to disk, readable only by the owner."""
    path = _config_file(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure(path.parent, 0o700)
    # os.open rather than Path.open: the mode must be set at creation time, or
    # the file exists world-readable for the window before a chmod lands.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config.to_dict(), f, indent=2)
    _secure(path, 0o600)


def _secure(path: Path, mode: int) -> None:
    """Best-effort tighten of an existing path's permissions."""
    try:
        if path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
    except OSError:
        pass


def migrate_stored_passphrase(config: Config, profile: str | None = None) -> None:
    """Remove a cleartext passphrase left in an older config file.

    Offers to hand the key to the ssh-agent first, so the migration does not
    cost the user a prompt on every later command.
    """
    if config.passphrase is None:
        return

    path = _config_file(profile)
    console.print(
        f"[yellow]⚠[/] Your SSH key passphrase is stored in cleartext at [dim]{path}[/]."
    )
    console.print("   dsync no longer stores it — the ssh-agent holds it instead.")

    from .ssh import load_key_into_agent

    if Confirm.ask("   Load the key into your ssh-agent now?", default=True):
        if load_key_into_agent(config.key_path, config.passphrase):
            console.print("[green]✓[/] Key loaded into ssh-agent.")
        else:
            console.print(
                "[yellow]⚠[/] Could not load it; you will be prompted when connecting."
            )

    config.passphrase = None
    save_config(config, profile=profile)
    console.print(f"[green]✓[/] Passphrase removed from {path} (now mode 0600).")


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
