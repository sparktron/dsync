"""Regression coverage for useful changes reconciled from the older branch."""

from pathlib import Path
import subprocess
from unittest.mock import MagicMock

import paramiko
import pytest
from click.testing import CliRunner

from dsync import ssh
from dsync.cli import _to_rel_path, cli
from dsync.config import Config
from dsync.sync import SyncError


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    return Config(
        dict(
            host="example.com",
            port=22,
            user="u",
            key_path=str(tmp_path / "key"),
            local_root=str(root),
            remote_root="/site/",
            site_url="https://example.com",
        )
    )


def test_helpers_timeout_and_missing_binary(monkeypatch):
    run = MagicMock(side_effect=subprocess.TimeoutExpired("ssh-add", 20))
    monkeypatch.setattr(ssh.subprocess, "run", run)
    assert ssh._run_tool(["ssh-add", "-l"]) is None
    assert run.call_args.kwargs["timeout"] == 20
    run.side_effect = FileNotFoundError()
    assert ssh._run_tool(["ssh-agent", "-s"]) is None


def test_askpass_is_one_shot_and_bounded(tmp_path, monkeypatch):
    real_run = subprocess.run
    seen = []

    def run(cmd, **kwargs):
        helper = Path(kwargs["env"]["SSH_ASKPASS"])
        seen.append(helper)
        assert kwargs["timeout"] == ssh.SSH_ADD_TIMEOUT_SECONDS
        assert (
            real_run([str(helper)], capture_output=True, text=True, check=True).stdout
            == "secret"
        )
        assert not helper.exists()
        return subprocess.CompletedProcess(cmd, 1, "", "rejected")

    monkeypatch.setattr(ssh, "_run_tool", run)
    assert ssh._ssh_add(tmp_path / "key", "secret", {}).returncode == 1
    assert not seen[0].exists()


def test_failed_agent_is_killed_and_not_cached(config, monkeypatch):
    monkeypatch.setattr(ssh, "_agent_env", {})
    monkeypatch.setattr(ssh, "_passphrase_cache", "bad")
    monkeypatch.setattr(ssh, "_passphrase_asked", True)
    monkeypatch.setattr(ssh, "agent_has_key", lambda p: False)
    monkeypatch.setattr(ssh, "key_is_encrypted", lambda p: True)
    monkeypatch.setattr(
        ssh,
        "_run_tool",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, "SSH_AUTH_SOCK=/tmp/test;\nSSH_AGENT_PID=123;", ""
        ),
    )
    monkeypatch.setattr(ssh, "_ssh_add", lambda *a, **k: None)
    killed = MagicMock()
    monkeypatch.setattr(ssh, "_kill_agent", killed)
    monkeypatch.setattr(ssh.atexit, "register", MagicMock())
    with pytest.raises(paramiko.SSHException, match="could not be loaded"):
        ssh.get_rsync_env(config.key_path)
    killed.assert_called_once()
    assert ssh._agent_env == {}
    assert ssh._passphrase_cache is None
    assert not ssh._passphrase_asked


def test_agent_key_avoids_prompt(config, monkeypatch):
    monkeypatch.setattr(ssh, "agent_has_key", lambda p: True)
    prompt = MagicMock(side_effect=AssertionError("unexpected prompt"))
    monkeypatch.setattr(ssh, "get_passphrase", prompt)
    client = MagicMock()
    monkeypatch.setattr(ssh.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(ssh, "_load_known_hosts", MagicMock())
    ssh.SSHManager(config)._do_connect()
    assert client.connect.call_args.kwargs["passphrase"] is None


@pytest.mark.parametrize("path", ["../outside", "link/file.txt"])
def test_cli_path_rejects_escape(config, tmp_path, path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (config.local_root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SyncError, match="outside"):
        _to_rel_path(config, path)


def test_existing_external_absolute_path_rejected(config, tmp_path):
    outside = tmp_path / "secret"
    outside.write_text("secret")
    with pytest.raises(SyncError):
        _to_rel_path(config, str(outside))


def test_site_paths_and_symlink_root(config, tmp_path):
    (config.local_root / "page.html").write_text("hi")
    assert _to_rel_path(config, "/page.html") == "page.html"
    assert _to_rel_path(config, str(config.local_root / "page.html")) == "page.html"
    link = tmp_path / "alias"
    link.symlink_to(config.local_root, target_is_directory=True)
    config.local_root = link
    assert _to_rel_path(config, "page.html") == "page.html"


def test_open_rejects_escape_before_browser(config, monkeypatch):
    import dsync.cli as module

    monkeypatch.setattr(module, "load_config", lambda **kw: config)
    browser = MagicMock()
    monkeypatch.setattr(module.webbrowser, "open", browser)
    result = CliRunner().invoke(cli, ["open", "../outside"])
    assert result.exit_code != 0
    browser.assert_not_called()


def test_ssh_connection_failure_is_logged(config, monkeypatch):
    import dsync.cli as module

    monkeypatch.setattr(module, "load_config", lambda **kw: config)
    monkeypatch.setattr(module, "StateManager", MagicMock())
    monkeypatch.setattr(module, "run_hook", lambda *a: True)
    manager = MagicMock()
    manager.return_value.__enter__.side_effect = paramiko.SSHException("offline")
    monkeypatch.setattr(module, "SSHManager", manager)
    log = MagicMock()
    monkeypatch.setattr(module, "append_log", log)
    result = CliRunner().invoke(cli, ["push"])
    assert result.exit_code != 0
    assert "offline" in result.output
    assert log.call_args.kwargs["ok"] is False
