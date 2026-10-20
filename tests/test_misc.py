"""Tests for the watcher, state persistence, config validation and CLI paths."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from watchdog.events import FileModifiedEvent, FileMovedEvent

from dsync import cli
from dsync.config import Config, ConfigError
from dsync.state import StateManager
from dsync.sync import SyncError
from dsync.watcher import _DebouncedHandler

BASE = {
    "host": "example.com",
    "port": 22,
    "user": "u",
    "key_path": "~/.ssh/id_rsa",
    "remote_root": "/home/u/public_html/",
    "site_url": "https://example.com",
}


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    return Config({**BASE, "local_root": str(root)})


class TestWatcher:
    """Timers are faked; pending callbacks run after the event is handled.

    Firing inside Timer.start() would deadlock: _schedule holds the handler's
    lock while starting the timer, and _fire takes the same lock.
    """

    def _run(self, config, *events):
        fired: list[str] = []
        pending: list = []
        h = _DebouncedHandler(
            config.local_root, fired.append, config.ignore_patterns, debounce_ms=0
        )
        with patch("dsync.watcher.threading.Timer") as timer:
            timer.side_effect = lambda delay, fn, args: MagicMock(
                start=lambda: pending.append((fn, args))
            )
            for handle, event in events:
                getattr(h, handle)(event)
        for fn, args in pending:
            fn(*args)
        return fired

    def test_atomic_save_rename_is_pushed(self, config):
        fired = self._run(
            config,
            (
                "on_moved",
                FileMovedEvent(
                    str(config.local_root / "index.html.tmp123"),
                    str(config.local_root / "index.html"),
                ),
            ),
        )
        assert fired == ["index.html"]

    def test_ignored_files_still_ignored(self, config):
        fired = self._run(
            config,
            ("on_modified", FileModifiedEvent(str(config.local_root / "a.swp"))),
            (
                "on_moved",
                FileMovedEvent(
                    str(config.local_root / "a.html"),
                    str(config.local_root / "a.html~"),
                ),
            ),
        )
        assert fired == []


class TestStateSave:
    def test_atomic_and_round_trips(self, tmp_path, monkeypatch):
        path = tmp_path / "state.json"
        monkeypatch.setattr("dsync.state._state_file_for", lambda p: path)
        f = tmp_path / "x.html"
        f.write_text("x")
        s = StateManager()
        s.update("x.html", f)
        s.save()
        assert list(tmp_path.glob(".state-*")) == []
        assert StateManager().get("x.html").checksum == s.get("x.html").checksum

    def test_failed_write_keeps_old_state(self, tmp_path, monkeypatch):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({}))
        monkeypatch.setattr("dsync.state._state_file_for", lambda p: path)
        s = StateManager()
        with patch("dsync.state.json.dump", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                s.save()
        assert json.loads(path.read_text()) == {}
        assert list(tmp_path.glob(".state-*")) == []


class TestConfigValidation:
    def test_missing_keys_named(self):
        with pytest.raises(ConfigError, match="local_root, remote_root"):
            Config({"host": "h", "port": 22, "user": "u", "key_path": "k", "site_url": "s"})

    def test_bad_port(self):
        with pytest.raises(ConfigError, match="port"):
            Config({**BASE, "local_root": "/x", "port": "ssh"})

    def test_default_ignore_not_shared(self):
        a = Config({**BASE, "local_root": "/x"})
        b = Config({**BASE, "local_root": "/y"})
        a.ignore_patterns.append("extra")
        assert "extra" not in b.ignore_patterns

    def test_backup_keep_days_round_trips(self):
        c = Config({**BASE, "local_root": "/x", "backup_keep_days": 14})
        assert Config(c.to_dict()).backup_keep_days == 14


class TestToRelPath:
    def test_absolute_under_root(self, config):
        f = config.local_root / "css" / "a.css"
        f.parent.mkdir()
        f.write_text("")
        assert cli._to_rel_path(config, str(f)) == "css/a.css"

    def test_symlinked_root(self, config, tmp_path):
        link = tmp_path / "link"
        link.symlink_to(config.local_root)
        (config.local_root / "a.html").write_text("")
        linked = Config({**BASE, "local_root": str(link)})
        assert cli._to_rel_path(linked, str(link / "a.html")) == "a.html"

    def test_relative_to_root(self, config):
        assert cli._to_rel_path(config, "css/a.css") == "css/a.css"


class TestPushPath:
    def test_rejects_parent_escape(self, config, tmp_path):
        (tmp_path / "secret.txt").write_text("x")
        with pytest.raises(SyncError, match="outside the site root"):
            cli._push_path(MagicMock(), config, MagicMock(), "../secret.txt")

    def test_missing_path(self, config):
        with pytest.raises(SyncError, match="not found"):
            cli._push_path(MagicMock(), config, MagicMock(), "nope.html")


class TestCliOutcomes:
    """Commands must exit non-zero and log a failure when rsync fails."""

    @pytest.fixture
    def env(self, config, monkeypatch):
        logs: list[tuple] = []
        monkeypatch.setattr(cli, "load_config", lambda profile=None: config)
        monkeypatch.setattr(cli, "StateManager", lambda profile=None: MagicMock())
        monkeypatch.setattr(cli, "SSHManager", MagicMock())
        monkeypatch.setattr(
            cli, "append_log", lambda action, files, ok, **kw: logs.append((action, ok))
        )
        monkeypatch.setattr(cli, "run_hook", MagicMock(return_value=True))
        return logs

    def test_push_dry_run_failure_is_not_in_sync(self, env, monkeypatch):
        monkeypatch.setattr(
            cli, "rsync_push_dry_run", MagicMock(side_effect=SyncError("boom"))
        )
        result = CliRunner().invoke(cli.cli, ["push"])
        assert result.exit_code == 1
        assert "in sync" not in result.output
        assert env == [("push", False)]

    def test_push_nothing_to_do_is_not_logged_as_failure(self, env, monkeypatch):
        monkeypatch.setattr(cli, "rsync_push_dry_run", MagicMock(return_value=[]))
        result = CliRunner().invoke(cli.cli, ["push"])
        assert result.exit_code == 0
        assert env == []
        cli.run_hook.assert_called_once_with(cli.load_config(), "pre_push")

    def test_pull_failure_logged_and_skips_post_hook(self, env, monkeypatch):
        monkeypatch.setattr(cli, "rsync_pull", MagicMock(side_effect=SyncError("x")))
        result = CliRunner().invoke(cli.cli, ["pull"], input="y\n")
        assert result.exit_code == 1
        assert env == [("pull", False)]
        hooks = [c.args[1] for c in cli.run_hook.call_args_list]
        assert "post_pull" not in hooks

    def test_status_failure_exits_nonzero(self, env, monkeypatch):
        monkeypatch.setattr(cli, "rsync_status", MagicMock(side_effect=SyncError("x")))
        result = CliRunner().invoke(cli.cli, ["status"])
        assert result.exit_code == 1
        assert "in sync" not in result.output

    def test_prune_requires_days(self, env):
        result = CliRunner().invoke(cli.cli, ["prune-backups"])
        assert result.exit_code == 2
        assert "--days" in result.output

    def test_prune_confirms_before_deleting(self, env, monkeypatch):
        monkeypatch.setattr(
            cli, "find_old_backups", MagicMock(return_value=["/b/2026-01-01_00-00-00"])
        )
        delete = MagicMock()
        monkeypatch.setattr(cli, "delete_backups", delete)
        result = CliRunner().invoke(cli.cli, ["prune-backups", "--days", "30"], input="n\n")
        assert result.exit_code == 0
        delete.assert_not_called()


class TestHostKeyStore:
    """Host-key verification must fail closed when known_hosts is unusable."""

    def test_unreadable_known_hosts_aborts(self, tmp_path, monkeypatch):
        import paramiko

        from dsync import ssh as ssh_mod

        kh = tmp_path / "known_hosts"
        kh.write_text("")
        monkeypatch.setattr(ssh_mod, "KNOWN_HOSTS", kh)
        client = MagicMock()
        client.load_system_host_keys.side_effect = PermissionError("denied")
        with pytest.raises(paramiko.SSHException, match="Cannot read"):
            ssh_mod._load_known_hosts(client)
        client.load_system_host_keys.assert_called_once_with(str(kh))

    def test_missing_known_hosts_is_first_use(self, tmp_path, monkeypatch):
        from dsync import ssh as ssh_mod

        monkeypatch.setattr(ssh_mod, "KNOWN_HOSTS", tmp_path / "absent")
        client = MagicMock()
        ssh_mod._load_known_hosts(client)
        client.load_system_host_keys.assert_not_called()

    def test_unwritable_store_refuses_new_key(self, tmp_path, monkeypatch):
        import paramiko

        from dsync import ssh as ssh_mod

        kh = tmp_path / "known_hosts"
        kh.mkdir()  # opening a directory for append fails
        monkeypatch.setattr(ssh_mod, "KNOWN_HOSTS", kh)
        client = MagicMock()
        with pytest.raises(paramiko.SSHException, match="Refusing unknown host"):
            ssh_mod._AcceptNewPolicy().missing_host_key(client, "h", MagicMock())
        client.get_host_keys.return_value.add.assert_not_called()

    def test_new_key_recorded(self, tmp_path, monkeypatch):
        from dsync import ssh as ssh_mod

        kh = tmp_path / ".ssh" / "known_hosts"
        monkeypatch.setattr(ssh_mod, "KNOWN_HOSTS", kh)
        monkeypatch.setattr(ssh_mod, "console", MagicMock())
        key = MagicMock()
        key.get_name.return_value = "ssh-ed25519"
        key.get_base64.return_value = "AAAA"
        key.asbytes.return_value = b"k"
        ssh_mod._AcceptNewPolicy().missing_host_key(MagicMock(), "[h]:2222", key)
        assert kh.read_text() == "[h]:2222 ssh-ed25519 AAAA\n"


class TestUploadSucceedsDespiteMetadataErrors:
    def test_utime_rejected_still_success(self, config, tmp_path, monkeypatch):
        from dsync import sync

        monkeypatch.setattr("dsync.state._state_file_for", lambda p: tmp_path / "s.json")
        (config.local_root / "a.html").write_text("x")
        ssh = MagicMock()
        ssh.run.return_value = ("", "")
        ssh.sftp.utime.side_effect = OSError("SETSTAT unsupported")
        state = StateManager()

        assert sync.push_single_file(ssh, config, state, "a.html") is True
        ssh.sftp.put.assert_called_once()
        assert state.get("a.html") is not None

    def test_put_failure_is_failure(self, config, tmp_path, monkeypatch):
        from dsync import sync

        monkeypatch.setattr("dsync.state._state_file_for", lambda p: tmp_path / "s.json")
        (config.local_root / "a.html").write_text("x")
        ssh = MagicMock()
        ssh.run.return_value = ("", "")
        ssh.sftp.put.side_effect = OSError("quota exceeded")
        assert sync.push_single_file(ssh, config, StateManager(), "a.html") is False
