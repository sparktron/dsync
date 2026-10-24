"""Unit tests for sync.py -- no network, rsync and SSH are stubbed."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from dsync import sync
from dsync.config import Config
from dsync.state import StateManager, _matches_ignore


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    return Config(
        {
            "host": "example.com",
            "port": 22,
            "user": "u",
            "key_path": str(tmp_path / "id_rsa"),
            "local_root": str(root),
            "remote_root": "/home/u/public_html",
            "site_url": "https://example.com/",
            "backup_dir": "/home/u/backups/dsync",
        }
    )


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dsync.state._state_file_for", lambda profile: tmp_path / "state.json"
    )
    return StateManager()


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _stub_rsync(monkeypatch, *results):
    calls = []
    it = iter(results)

    def fake(config, src, dst, extra_flags=None, dry_run=False, capture=True):
        calls.append({"src": src, "dst": dst, "flags": extra_flags, "dry": dry_run})
        return next(it)

    monkeypatch.setattr(sync, "_run_rsync", fake)
    return calls


# ---------------------------------------------------------------------------
# Itemize parsing
# ---------------------------------------------------------------------------


class TestParseItemize:
    def test_transfers_and_deletions(self):
        out = (
            "<f+++++++++ new.html\n"
            "<f.st...... css/style.css\n"
            "cd+++++++++ newdir/\n"
            "*deleting   old.html\n"
            ".d..t...... ./\n"
        )
        transfers, deletions = sync._parse_itemize(out)
        assert transfers == ["new.html", "css/style.css"]
        assert deletions == ["old.html"]

    def test_paths_with_spaces(self):
        transfers, _ = sync._parse_itemize("<f+++++++++ my file.html\n")
        assert transfers == ["my file.html"]

    def test_symlink_counts_as_transfer(self):
        transfers, _ = sync._parse_itemize("cL+++++++++ link -> target\n")
        assert transfers == ["link -> target"]

    def test_attribute_only_change_is_not_a_transfer(self):
        transfers, deletions = sync._parse_itemize(".f..t...... same.html\n")
        assert transfers == [] and deletions == []

    def test_empty(self):
        assert sync._parse_itemize("") == ([], [])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestFileToUrl:
    @pytest.mark.parametrize(
        ("rel", "url"),
        [
            ("index.html", "https://example.com/"),
            ("aboutme/index.html", "https://example.com/aboutme/"),
            ("css/style.css", "https://example.com/css/style.css"),
        ],
    )
    def test_mapping(self, config, rel, url):
        assert sync.file_to_url(config, rel) == url


class TestMatchesIgnore:
    @pytest.mark.parametrize(
        ("rel", "expected"),
        [
            (".git/config", True),
            ("images/a.png", True),
            ("sub/images/a.png", True),
            ("backup.zip", True),
            ("a/b/.DS_Store", True),
            ("index.html", False),
            ("css/style.css", False),
        ],
    )
    def test_default_patterns(self, config, rel, expected):
        assert _matches_ignore(rel, config.ignore_patterns) is expected


class TestSafeRelPath:
    @pytest.mark.parametrize("rel", ["index.html", "a/b.css", "a..b/c", "my file"])
    def test_safe(self, rel):
        assert sync.is_safe_rel_path(rel)

    @pytest.mark.parametrize("rel", ["", "/etc/passwd", "../x", "a/../../x", ".."])
    def test_unsafe(self, rel):
        assert not sync.is_safe_rel_path(rel)


class TestSshCmd:
    def test_verifies_host_keys(self, config):
        cmd = sync._ssh_cmd(config)
        assert "StrictHostKeyChecking=no" not in cmd
        assert "/dev/null" not in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd

    def test_rsync_protects_args(self, config, monkeypatch):
        captured = {}
        monkeypatch.setattr(sync, "get_rsync_env", lambda *a, **k: {})
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: captured.setdefault("cmd", cmd) and _proc(),
        )
        sync._run_rsync(config, "a/", "b/")
        assert "--protect-args" in captured["cmd"]


# ---------------------------------------------------------------------------
# rsync failures must surface, never read as "in sync"
# ---------------------------------------------------------------------------


class TestFailuresRaise:
    def test_push_dry_run_failure_raises(self, config, monkeypatch):
        _stub_rsync(monkeypatch, _proc(255, stderr="Permission denied"))
        with pytest.raises(sync.SyncError, match="Permission denied"):
            sync.rsync_push_dry_run(config)

    def test_push_dry_run_success_empty_means_in_sync(self, config, monkeypatch):
        _stub_rsync(monkeypatch, _proc(0, stdout=""))
        assert sync.rsync_push_dry_run(config) == []

    def test_push_all_failure_raises_and_keeps_state(
        self, config, state, monkeypatch
    ):
        _stub_rsync(monkeypatch, _proc(23, stderr="partial transfer"))
        with pytest.raises(sync.SyncError):
            sync.rsync_push_all(config, state)
        assert state.is_empty()

    def test_pull_failure_raises(self, config, state, monkeypatch):
        _stub_rsync(monkeypatch, _proc(12, stderr="protocol error"))
        with pytest.raises(sync.SyncError):
            sync.rsync_pull(config, state)

    def test_pull_returns_transfers(self, config, state, monkeypatch):
        _stub_rsync(monkeypatch, _proc(0, stdout=">f+++++++++ a.html\n"))
        assert sync.rsync_pull(config, state) == ["a.html"]

    @pytest.mark.parametrize("failing", [0, 1])
    def test_status_failure_raises(self, config, monkeypatch, failing):
        results = [_proc(0), _proc(0)]
        results[failing] = _proc(255, stderr="Connection refused")
        _stub_rsync(monkeypatch, *results)
        with pytest.raises(sync.SyncError, match="Connection refused"):
            sync.rsync_status(config, MagicMock())

    def test_directory_failure_raises(self, config, state, monkeypatch):
        (config.local_root / "css").mkdir()
        _stub_rsync(monkeypatch, _proc(23))
        with pytest.raises(sync.SyncError):
            sync.rsync_push_directory(config, state, "css")


class TestStatus:
    def test_groups(self, config, monkeypatch):
        (config.local_root / "both.html").write_text("local")
        os.utime(config.local_root / "both.html", (100, 100))
        (config.local_root / "mine.html").write_text("x")
        push = _proc(
            stdout="<f.st...... both.html\n<f.st...... edited.html\n"
            "*deleting   theirs.html\n"
        )
        pull = _proc(
            stdout=">f.st...... both.html\n>f.st...... edited.html\n"
            "*deleting   mine.html\n"
        )
        calls = _stub_rsync(monkeypatch, push, pull)
        ssh = MagicMock()
        # Remote copy of both.html is newer; edited.html is newer locally.
        ssh.sftp.stat.side_effect = lambda p: MagicMock(
            st_mtime=200 if p.endswith("both.html") else 0
        )
        (config.local_root / "edited.html").write_text("y")

        groups = sync.rsync_status(config, ssh)

        assert groups == {
            "local_newer": ["edited.html"],
            "remote_newer": ["both.html"],
            "local_only": ["mine.html"],
            "remote_only": ["theirs.html"],
        }
        assert all(c["dry"] and c["flags"] == ["--delete"] for c in calls)


# ---------------------------------------------------------------------------
# Directory push / state tracking
# ---------------------------------------------------------------------------


class TestPushDirectory:
    def test_ignored_files_not_tracked(self, config, state, monkeypatch):
        d = config.local_root / "blog"
        d.mkdir()
        (d / "post.html").write_text("p")
        (d / "post.html.swp").write_text("swap")
        (d / ".DS_Store").write_text("junk")
        _stub_rsync(monkeypatch, _proc(stdout="<f+++++++++ post.html\n"))

        transferred = sync.rsync_push_directory(config, state, "blog")

        assert transferred == ["blog/post.html"]
        assert [p for p, _ in state.items()] == ["blog/post.html"]

    def test_rejects_escaping_path(self, config, state, monkeypatch):
        calls = _stub_rsync(monkeypatch)
        with pytest.raises(sync.SyncError, match="outside the site root"):
            sync.rsync_push_directory(config, state, "../elsewhere")
        assert calls == []


# ---------------------------------------------------------------------------
# Single-file push
# ---------------------------------------------------------------------------


class TestPushSingleFile:
    def test_uploads_preserving_mtime(self, config, state):
        f = config.local_root / "css" / "a.css"
        f.parent.mkdir()
        f.write_text("body{}")
        os.utime(f, (111, 222))
        ssh = MagicMock()
        ssh.run.return_value = ("", "")

        assert sync.push_single_file(ssh, config, state, "css/a.css")

        remote = "/home/u/public_html/css/a.css"
        ssh.sftp.put.assert_called_once_with(str(f), remote)
        ssh.sftp.utime.assert_called_once_with(remote, (111, 222))
        mkdirs = [c.args[0] for c in ssh.run.call_args_list if "mkdir -p" in c.args[0]]
        assert any("/home/u/public_html/css" in c for c in mkdirs)
        assert state.get("css/a.css") is not None

    def test_refuses_escaping_path(self, config, state):
        ssh = MagicMock()
        assert not sync.push_single_file(ssh, config, state, "../x")
        ssh.sftp.put.assert_not_called()


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


class TestBackups:
    def test_backup_mirrors_tree_without_collisions(self, config):
        ssh = MagicMock()
        ssh.run.return_value = ("", "")
        backup_dir, failed = sync.backup_remote_files(
            ssh, config, ["a/b_c.html", "a_b/c.html"]
        )
        assert failed == []
        cmds = " ".join(c.args[0] for c in ssh.run.call_args_list)
        assert f"{backup_dir}/a/b_c.html" in cmds
        assert f"{backup_dir}/a_b/c.html" in cmds
        assert "|| true" not in cmds

    def test_backup_reports_failures(self, config):
        ssh = MagicMock()

        def run(cmd, check=True):
            if "bad.html" in cmd:
                raise RuntimeError("cp: Permission denied")
            return "", ""

        ssh.run.side_effect = run
        _, failed = sync.backup_remote_files(ssh, config, ["ok.html", "bad.html"])
        assert failed == ["bad.html"]

    def test_backup_cmd_skips_missing_source(self, tmp_path):
        """Run the generated shell for real: new file -> no-op, exit 0."""
        src, dest = tmp_path / "missing", tmp_path / "bk" / "sub" / "missing"
        r = subprocess.run(
            ["sh", "-c", sync._backup_cmd(str(src), str(dest))], check=False
        )
        assert r.returncode == 0 and not dest.exists()

    def test_backup_cmd_copies_and_keeps_first(self, tmp_path):
        src = tmp_path / "f.html"
        dest = tmp_path / "bk" / "d" / "f.html"
        src.write_text("v1")
        cmd = ["sh", "-c", sync._backup_cmd(str(src), str(dest))]
        assert subprocess.run(cmd, check=False).returncode == 0
        src.write_text("v2")
        assert subprocess.run(cmd, check=False).returncode == 0
        assert dest.read_text() == "v1"

    def test_backup_cmd_fails_loudly(self, tmp_path):
        src = tmp_path / "f.html"
        src.write_text("x")
        blocker = tmp_path / "bk"
        blocker.write_text("not a dir")
        r = subprocess.run(
            ["sh", "-c", sync._backup_cmd(str(src), str(blocker / "f.html"))],
            capture_output=True,
            check=False,
        )
        assert r.returncode != 0

    def test_find_old_backups_filters_to_dsync_names(self, config):
        base = "/home/u/backups/dsync"
        ssh = MagicMock()
        ssh.run.return_value = (
            f"{base}/2026-01-01_10-00-00\n"
            f"{base}/2026-01-02_10-00-00.tar.gz\n"
            f"{base}/2025-12-01_09-00-00_css_style.css\n"
            f"{base}/important-notes.txt\n"
            f"{base}/2026-01-01_10-00-00/nested\n",
            "",
        )
        old = sync.find_old_backups(ssh, config, 30)
        assert old == [
            f"{base}/2025-12-01_09-00-00_css_style.css",
            f"{base}/2026-01-01_10-00-00",
            f"{base}/2026-01-02_10-00-00.tar.gz",
        ]
        assert "-mtime +30" in ssh.run.call_args.args[0]

    def test_find_old_backups_rejects_bad_retention(self, config):
        with pytest.raises(sync.SyncError):
            sync.find_old_backups(MagicMock(), config, 0)

    def test_find_old_backups_refuses_root(self, config):
        config.backup_dir = "/"
        with pytest.raises(sync.SyncError, match="Refusing"):
            sync.find_old_backups(MagicMock(), config, 30)

    def test_delete_refuses_non_backup(self):
        ssh = MagicMock()
        with pytest.raises(sync.SyncError):
            sync.delete_backups(ssh, ["/home/u/public_html"])
        ssh.run.assert_not_called()

    def test_delete_backups(self):
        ssh = MagicMock()
        sync.delete_backups(ssh, ["/b/2026-01-01_10-00-00"])
        ssh.run.assert_called_once_with("rm -rf -- /b/2026-01-01_10-00-00")
