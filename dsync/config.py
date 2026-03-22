"""Configuration management for dsync."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Prompt

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
        self.local_root: Path = Path(data["local_root"]).expanduser()
        self.remote_root: str = data["remote_root"].rstrip("/") + "/"
        self.site_url: str = data["site_url"].rstrip("/")
        self.backup_dir: str = data.get("backup_dir", "~/backups/dsync")
        self.ignore_patterns: list[str] = data.get("ignore_patterns", DEFAULT_IGNORE)
        self.hooks: dict[str, str] = data.get("hooks", {})

    def to_dict(self) -> dict[str, Any]:
        """Serialize config to a JSON-compatible dict."""
        return {
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
    return Config(data)


def save_config(config: Config, profile: str | None = None) -> None:
    """Persist config to disk."""
    path = _config_file(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(config.to_dict(), f, indent=2)


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
