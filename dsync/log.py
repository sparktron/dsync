"""Operation logging: persistent JSON-lines audit trail of sync operations."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".dsync" / "sync.log"
MAX_ENTRIES = 500


def append_log(
    action: str,
    files: list[str],
    ok: bool,
    duration_ms: int,
    profile: str | None = None,
) -> None:
    """Append one log entry (JSON line) to the sync log."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry: dict = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "files": files,
        "ok": ok,
        "ms": duration_ms,
    }
    if profile:
        entry["profile"] = profile
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    _trim_log()


def read_log(n: int = 50) -> list[dict]:
    """Read the last *n* log entries, newest first."""
    if not LOG_FILE.exists():
        return []
    entries = []
    with LOG_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return list(reversed(entries[-n:]))


def _trim_log() -> None:
    """Keep the log file under MAX_ENTRIES lines."""
    if not LOG_FILE.exists():
        return
    with LOG_FILE.open() as f:
        lines = [ln for ln in f.readlines() if ln.strip()]
    if len(lines) > MAX_ENTRIES:
        with LOG_FILE.open("w") as f:
            f.writelines(lines[-MAX_ENTRIES:])
