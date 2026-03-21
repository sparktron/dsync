"""State tracking: persistent manifest of file checksums and sync times."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterator

STATE_FILE = Path.home() / ".dsync" / "state.json"


class FileState:
    """State entry for a single synced file."""

    def __init__(self, mtime: float, checksum: str, last_synced: float) -> None:
        self.mtime = mtime
        self.checksum = checksum
        self.last_synced = last_synced

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "mtime": self.mtime,
            "checksum": self.checksum,
            "last_synced": self.last_synced,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileState":
        """Deserialize from a JSON dict."""
        return cls(
            mtime=data["mtime"],
            checksum=data["checksum"],
            last_synced=data["last_synced"],
        )


class StateManager:
    """
    Manages the dsync state manifest at ~/.dsync/state.json.

    The manifest maps relative file paths to their last-known mtime,
    checksum, and sync timestamp.  Used for fast local diffing without
    requiring a full remote scan every time.
    """

    def __init__(self) -> None:
        self._state: dict[str, FileState] = {}
        self._load()

    def _load(self) -> None:
        """Load state from disk if it exists."""
        if STATE_FILE.exists():
            try:
                with STATE_FILE.open() as f:
                    raw: dict[str, Any] = json.load(f)
                self._state = {
                    path: FileState.from_dict(entry) for path, entry in raw.items()
                }
            except (json.JSONDecodeError, KeyError):
                # Corrupt state — start fresh.
                self._state = {}

    def save(self) -> None:
        """Persist state to disk."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w") as f:
            json.dump(
                {path: entry.to_dict() for path, entry in self._state.items()},
                f,
                indent=2,
            )

    def update(self, rel_path: str, local_path: Path) -> None:
        """Record current mtime + checksum for a local file."""
        stat = local_path.stat()
        checksum = compute_checksum(local_path)
        self._state[rel_path] = FileState(
            mtime=stat.st_mtime,
            checksum=checksum,
            last_synced=time.time(),
        )

    def get(self, rel_path: str) -> FileState | None:
        """Return state for a file, or None if unknown."""
        return self._state.get(rel_path)

    def remove(self, rel_path: str) -> None:
        """Remove a file's state entry."""
        self._state.pop(rel_path, None)

    def items(self) -> Iterator[tuple[str, FileState]]:
        """Iterate over (rel_path, FileState) pairs."""
        return iter(self._state.items())

    def is_empty(self) -> bool:
        """Return True if the manifest has no entries."""
        return len(self._state) == 0

    def scan_directory(self, root: Path) -> None:
        """Rebuild the full manifest by scanning a local directory."""
        self._state = {}
        for file in root.rglob("*"):
            if file.is_file():
                rel = str(file.relative_to(root))
                self.update(rel, file)


def compute_checksum(path: Path) -> str:
    """Compute the MD5 checksum of a file."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
