"""File system watcher: monitors the local root and auto-pushes on save."""

from __future__ import annotations

import fnmatch
import threading
import time
from pathlib import Path
from typing import Callable

from rich.console import Console
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config

console = Console()

# Patterns excluded from watch events (in addition to config ignore_patterns).
_WATCH_IGNORE: list[str] = [
    ".git",
    "images",
    "lscache",
    "__pycache__",
    ".DS_Store",
    "*~",
    "*.swp",
    ".dsync_state",
    "*.gz",
    "*.zip",
]


def _should_ignore(abs_path: str) -> bool:
    """Return True if this path should be excluded from watch events."""
    parts = Path(abs_path).parts
    name = Path(abs_path).name
    for part in (*parts, name):
        for pattern in _WATCH_IGNORE:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


class _DebouncedHandler(FileSystemEventHandler):
    """
    Watchdog event handler that debounces per-file events.

    Rapid saves (e.g. editor writing a temp file then renaming) are
    collapsed into a single callback call after ``debounce_ms`` ms of
    quiet time.
    """

    def __init__(
        self,
        root: Path,
        callback: Callable[[str], None],
        debounce_ms: int = 800,
    ) -> None:
        super().__init__()
        self.root = root
        self.callback = callback
        self.debounce_ms = debounce_ms
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(str(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(str(event.src_path))

    def _schedule(self, abs_path: str) -> None:
        """Cancel any pending timer for this file and start a fresh one."""
        if _should_ignore(abs_path):
            return
        try:
            rel_path = str(Path(abs_path).relative_to(self.root))
        except ValueError:
            return

        with self._lock:
            existing = self._timers.get(rel_path)
            if existing:
                existing.cancel()
            timer = threading.Timer(
                self.debounce_ms / 1000.0, self._fire, args=[rel_path]
            )
            self._timers[rel_path] = timer
            timer.start()

    def _fire(self, rel_path: str) -> None:
        """Invoke the callback after the debounce period."""
        with self._lock:
            self._timers.pop(rel_path, None)
        self.callback(rel_path)


class FileWatcher:
    """
    Watches the local project root and calls *callback* with the relative
    path of any file that is created or modified.

    Usage::

        watcher = FileWatcher(config, on_change)
        watcher.run()   # blocks until Ctrl+C
    """

    def __init__(
        self,
        config: Config,
        callback: Callable[[str], None],
        debounce_ms: int = 800,
    ) -> None:
        self.config = config
        self.callback = callback
        self.debounce_ms = debounce_ms

    def run(self) -> None:
        """Start the observer loop. Blocks until interrupted with Ctrl+C."""
        handler = _DebouncedHandler(
            root=self.config.local_root,
            callback=self.callback,
            debounce_ms=self.debounce_ms,
        )
        observer = Observer()
        observer.schedule(handler, str(self.config.local_root), recursive=True)
        observer.start()

        console.print(
            f"[blue]Watching[/] {self.config.local_root} "
            "— [dim]Ctrl+C to stop[/]"
        )
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            observer.join()
        console.print("\n[yellow]Stopped.[/]")
