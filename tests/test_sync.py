"""Argv-level tests for the rsync invocations in dsync.sync.

`sync.py` is the module that can delete files on a live public website, and
`_run_rsync` takes `dry_run` as a bare boolean. Nothing previously asserted that
the destructive `--delete` flag only ever ships alongside `--dry-run`, so a
one-character edit could have turned `dsync status` into a destructive command
silently. These tests pin that pairing, plus the exclude/src/dst wiring.

They assert on the argv handed to subprocess, so no network or SSH is involved.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from dsync import sync
from dsync.config import Config
from dsync.state import _matches_ignore
from dsync.sync import _parse_itemize

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    """A Config rooted in tmp_path so nothing touches a real project."""
    local_root = tmp_path / "site"
    (local_root / "css").mkdir(parents=True)
    (local_root / "index.html").write_text("<h1>hi</h1>")
    (local_root / "css" / "style.css").write_text("body{}")
    return Config(
        {
            "host": "example.com",
            "port": 50288,
            "user": "testuser",
            "key_path": str(tmp_path / "id_rsa"),
            "local_root": str(local_root),
            "remote_root": "/home/testuser/public_html/",
            "site_url": "https://example.com",
            "ignore_patterns": [".git/", "*.gz"],
        }
    )


@pytest.fixture
def argv(monkeypatch):
    """Capture every rsync argv, and keep get_rsync_env from spawning an agent."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    monkeypatch.setattr(sync, "get_rsync_env", lambda *a, **k: {})
    return calls


# ---------------------------------------------------------------------------
# Drivers — one per rsync-invoking entry point
# ---------------------------------------------------------------------------


def _drive_pull(config):
    sync.rsync_pull(config, MagicMock())


def _drive_push_dry_run(config):
    sync.rsync_push_dry_run(config)


def _drive_push_all(config):
    sync.rsync_push_all(config, MagicMock())


def _drive_push_directory(config):
    sync.rsync_push_directory(config, MagicMock(), "css")


def _drive_status(config):
    sync.rsync_status(config, MagicMock())


ALL_DRIVERS = [
    ("rsync_pull", _drive_pull),
    ("rsync_push_dry_run", _drive_push_dry_run),
    ("rsync_push_all", _drive_push_all),
    ("rsync_push_directory", _drive_push_directory),
    ("rsync_status", _drive_status),
]

# Entry points that perform a real (non-dry-run) transfer. None of them may
# carry --delete: dsync's push path is additive by design.
MUTATING_DRIVERS = [
    ("rsync_pull", _drive_pull),
    ("rsync_push_all", _drive_push_all),
    ("rsync_push_directory", _drive_push_directory),
]


# ---------------------------------------------------------------------------
# The load-bearing invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "driver"), ALL_DRIVERS)
def test_delete_never_ships_without_dry_run(name, driver, config, argv):
    """--delete is only ever safe here paired with --dry-run."""
    driver(config)
    assert argv, f"{name} issued no rsync command"
    for cmd in argv:
        if "--delete" in cmd:
            assert "--dry-run" in cmd, (
                f"{name} passed --delete without --dry-run: {cmd}"
            )


@pytest.mark.parametrize(("name", "driver"), MUTATING_DRIVERS)
def test_transferring_commands_never_delete(name, driver, config, argv):
    """A command that actually writes to the remote must not delete anything."""
    driver(config)
    for cmd in argv:
        assert "--dry-run" not in cmd, f"{name} unexpectedly became a dry run"
        assert "--delete" not in cmd, (
            f"{name} performs a real transfer and must not pass --delete: {cmd}"
        )


# ---------------------------------------------------------------------------
# Per-entry-point flag expectations
# ---------------------------------------------------------------------------


def test_push_dry_run_is_a_dry_run_without_delete(config, argv):
    sync.rsync_push_dry_run(config)
    (cmd,) = argv
    assert "--dry-run" in cmd
    assert "--delete" not in cmd


def test_status_issues_two_dry_runs_both_with_delete(config, argv):
    """Status compares both directions; --delete is what surfaces extra files."""
    sync.rsync_status(config, MagicMock())
    assert len(argv) == 2, "status should issue exactly one dry-run per direction"
    for cmd in argv:
        assert "--dry-run" in cmd
        assert "--delete" in cmd


def test_status_probes_push_then_pull_direction(config, argv):
    sync.rsync_status(config, MagicMock())
    push_cmd, pull_cmd = argv
    local = str(config.local_root) + "/"
    remote = f"{config.user}@{config.host}:{config.remote_root}"
    assert push_cmd[-2:] == [local, remote]
    assert pull_cmd[-2:] == [remote, local]


# ---------------------------------------------------------------------------
# Source / destination wiring
# ---------------------------------------------------------------------------


def test_pull_reads_remote_writes_local(config, argv):
    sync.rsync_pull(config, MagicMock())
    (cmd,) = argv
    assert cmd[-2] == f"{config.user}@{config.host}:{config.remote_root}"
    assert cmd[-1] == str(config.local_root) + "/"


def test_push_all_reads_local_writes_remote(config, argv):
    sync.rsync_push_all(config, MagicMock())
    (cmd,) = argv
    assert cmd[-2] == str(config.local_root) + "/"
    assert cmd[-1] == f"{config.user}@{config.host}:{config.remote_root}"


def test_push_directory_scopes_both_sides_to_the_subtree(config, argv):
    sync.rsync_push_directory(config, MagicMock(), "css")
    (cmd,) = argv
    assert cmd[-2] == str(config.local_root / "css") + "/"
    assert cmd[-1] == f"{config.user}@{config.host}:{config.remote_root}css/"


# ---------------------------------------------------------------------------
# Excludes and transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "driver"), ALL_DRIVERS)
def test_ignore_patterns_forwarded_as_excludes(name, driver, config, argv):
    driver(config)
    for cmd in argv:
        pairs = [(cmd[i], cmd[i + 1]) for i, a in enumerate(cmd) if a == "--exclude"]
        assert [p[1] for p in pairs] == config.ignore_patterns, (
            f"{name} did not forward ignore_patterns: {cmd}"
        )


@pytest.mark.parametrize(("name", "driver"), ALL_DRIVERS)
def test_transport_carries_port_and_key(name, driver, config, argv):
    driver(config)
    for cmd in argv:
        ssh_cmd = cmd[cmd.index("-e") + 1]
        assert f"-p {config.port}" in ssh_cmd
        assert str(config.key_path) in ssh_cmd


@pytest.mark.parametrize(("name", "driver"), ALL_DRIVERS)
def test_checksum_and_itemize_always_requested(name, driver, config, argv):
    """Status parsing depends on --itemize-changes; --checksum on mtime-unsafe hosts."""
    driver(config)
    for cmd in argv:
        assert cmd[0] == "rsync"
        assert "--checksum" in cmd
        assert "--itemize-changes" in cmd


# ---------------------------------------------------------------------------
# Itemize parsing
# ---------------------------------------------------------------------------


def test_parse_itemize_splits_transfers_from_deletions():
    output = (
        ">f+++++++++ index.html\n"
        ">f.st...... css/style.css\n"
        "cd+++++++++ newdir/\n"
        "*deleting   old/page.html\n"
    )
    transfers, deletions = _parse_itemize(output)
    assert transfers == ["index.html", "css/style.css"]
    assert deletions == ["old/page.html"]


def test_parse_itemize_keeps_paths_containing_spaces():
    transfers, _ = _parse_itemize(">f+++++++++ my page with spaces.html\n")
    assert transfers == ["my page with spaces.html"]


def test_parse_itemize_skips_directories_and_the_root_entry():
    transfers, deletions = _parse_itemize("cd+++++++++ assets/\n.d..t...... ./\n")
    assert transfers == []
    assert deletions == []


def test_parse_itemize_tolerates_empty_output():
    assert _parse_itemize("") == ([], [])


# ---------------------------------------------------------------------------
# Ignore matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("index.html", False),
        ("images/a.png", True),
        ("css/images/a.png", True),
        ("notes/images.txt", False),
        ("archive.gz", True),
        ("sub/.git/config", True),
        ("docs/DS_Store", False),
    ],
)
def test_matches_ignore_unanchored_patterns(rel_path, expected):
    patterns = [".git/", "images/", "*.gz", ".DS_Store"]
    assert _matches_ignore(rel_path, patterns) is expected


def test_matches_ignore_with_no_patterns():
    assert _matches_ignore("anything/at/all.html", []) is False
