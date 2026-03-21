"""State tracking: persistent manifest of file checksums and sync times."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterator

_STATE_DIR = Path.home() / ".dsync"

# Module-level constant kept for backward compatibility.
STATE_FILE = _STATE_DIR / "state.json"


def _state_file_for(profile: str | None) -> Path:
    """Return the state file path for the given profile."""
    if profile is None:
        return STATE_FILE
    return _STATE_DIR / f"state_{profile}.json"


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
    Manages the dsync state manifest at ~/.dsync/state[_profile].json.

    The manifest maps relative file paths to their last-known mtime,
    checksum, and sync timestamp.  Used for fast local diffing without
    requiring a full remote scan every time.
    """

    def __init__(self, profile: str | None = None) -> None:
        self._state_file = _state_file_for(profile)
        self._state: dict[str, FileState] = {}
        self._load()

    def _load(self) -> None:
        """Load state from disk if it exists."""
        if self._state_file.exists():
            try:
                with self._state_file.open() as f:
                    raw: dict[str, Any] = json.load(f)
                self._state = {
                    path: FileState.from_dict(entry) for path, entry in raw.items()
                }
            except (json.JSONDecodeError, KeyError):
                # Corrupt state — start fresh.
                self._state = {}

    def save(self) -> None:
        """Persist state to disk."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        with self._state_file.open("w") as f:
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

    def scan_directory(self, root: Path, ignore_patterns: list[str] | None = None) -> None:
        """Rebuild the full manifest by scanning a local directory.

        Files whose relative path matches any pattern in *ignore_patterns*
        (the same list passed to rsync --exclude) are skipped so the manifest
        stays in sync with what rsync actually tracks.
        """
        ignore = ignore_patterns or []
        self._state = {}
        for file in root.rglob("*"):
            if not file.is_file():
                continue
            rel = str(file.relative_to(root))
            if _matches_ignore(rel, ignore):
                continue
            self.update(rel, file)


def _matches_ignore(rel_path: str, patterns: list[str]) -> bool:
    """Return True if *rel_path* matches any rsync-style ignore pattern."""
    parts = Path(rel_path).parts
    for pattern in patterns:
        # Strip trailing slash (directory marker in rsync patterns).
        pat = pattern.rstrip("/")
        # Match against each path component and the full relative path.
        if any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
        if fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def compute_checksum(path: Path) -> str:
    """Compute the MD5 checksum of a file."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
