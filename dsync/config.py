"""Configuration management for dsync."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.prompt import Prompt

console = Console()

CONFIG_DIR = Path.home() / ".dsync"
CONFIG_FILE = CONFIG_DIR / "config.json"

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
        }


def load_config() -> Config:
    """Load config from disk, running the first-run wizard if not found."""
    if not CONFIG_FILE.exists():
        return run_wizard()
    with CONFIG_FILE.open() as f:
        data = json.load(f)
    return Config(data)


def save_config(config: Config) -> None:
    """Persist config to ~/.dsync/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump(config.to_dict(), f, indent=2)


def run_wizard() -> Config:
    """Interactive first-run configuration wizard."""
    console.print("\n[bold blue]dsync[/] — first-run setup\n")
    console.print("No config found at [dim]~/.dsync/config.json[/]. Let's set one up.\n")

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
    save_config(config)
    console.print(f"\n[green]✓[/] Config saved to [dim]{CONFIG_FILE}[/]\n")
    return config
