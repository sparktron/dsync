"""CLI-level tests for failure reporting and the operation log.

These cover two ways dsync used to misreport what happened:

- `dsync status` printed "Everything is in sync." when the comparison itself
  failed, and exited 0.
- The log recorded `ok=True` for a pull that failed (rsync_pull swallowed the
  error and signalled nothing to its caller), and `ok=False` for a push that had
  simply nothing to do.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dsync.cli import cli
from dsync.config import Config
from dsync.sync import RsyncError


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    return Config(
        {
            "host": "example.com",
            "port": 22,
            "user": "testuser",
            "key_path": str(tmp_path / "id_rsa"),
            "local_root": str(root),
            "remote_root": "/home/testuser/public_html/",
            "site_url": "https://example.com",
        }
    )


@pytest.fixture
def env(config):
    """Patch out config loading, state, SSH and the log for a CLI invocation."""
    with (
        patch("dsync.cli.load_config", return_value=config),
        patch("dsync.cli.StateManager") as state,
        patch("dsync.cli.SSHManager"),
        patch("dsync.cli.run_hook", return_value=True),
        patch("dsync.cli.append_log") as append,
    ):
        state.return_value.is_empty.return_value = True
        yield {"append_log": append, "state": state}


def _log_calls(env):
    return [(c.args, c.kwargs) for c in env["append_log"].call_args_list]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_failure_does_not_claim_in_sync(runner, env):
    err = RsyncError("Status comparison (local → remote)", 255, "connection refused")
    with patch("dsync.cli.rsync_status", side_effect=err):
        result = runner.invoke(cli, ["status"])

    assert result.exit_code != 0, "a failed comparison must not exit 0"
    assert "in sync" not in result.output
    assert "connection refused" in result.output


def test_status_success_reports_in_sync_when_empty(runner, env):
    groups = {
        "local_newer": [],
        "remote_newer": [],
        "local_only": [],
        "remote_only": [],
    }
    with patch("dsync.cli.rsync_status", return_value=groups):
        result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "in sync" in result.output


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def test_failed_pull_is_logged_as_failure(runner, env):
    with patch("dsync.cli.rsync_pull", side_effect=RsyncError("Pull", 255, "boom")):
        result = runner.invoke(cli, ["pull"])

    assert result.exit_code != 0
    ((args, kwargs),) = _log_calls(env)
    assert args[0] == "pull"
    assert kwargs["ok"] is False


def test_successful_pull_is_logged_as_success(runner, env):
    with patch("dsync.cli.rsync_pull", return_value=None):
        result = runner.invoke(cli, ["pull"])

    assert result.exit_code == 0
    ((_args, kwargs),) = _log_calls(env)
    assert kwargs["ok"] is True


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def test_push_with_nothing_to_do_is_a_success(runner, env):
    """An already-in-sync tree is a successful push of zero files, not a failure."""
    with patch("dsync.cli.rsync_push_dry_run", return_value=[]):
        result = runner.invoke(cli, ["push"])

    assert result.exit_code == 0
    ((args, kwargs),) = _log_calls(env)
    assert args[0] == "push"
    assert args[1] == []
    assert kwargs["ok"] is True


def test_push_declined_at_the_prompt_is_not_logged(runner, env):
    """Nothing was attempted, so there is nothing to record."""
    with (
        patch("dsync.cli.rsync_push_dry_run", return_value=["index.html"]),
        patch("dsync.cli._show_push_diffs"),
    ):
        result = runner.invoke(cli, ["push"], input="n\n")

    assert result.exit_code == 0
    assert _log_calls(env) == []


def test_failed_push_is_logged_as_failure(runner, env):
    err = RsyncError("Push dry-run", 255, "host unreachable")
    with patch("dsync.cli.rsync_push_dry_run", side_effect=err):
        result = runner.invoke(cli, ["push"])

    assert result.exit_code != 0
    ((_args, kwargs),) = _log_calls(env)
    assert kwargs["ok"] is False
    assert "host unreachable" in result.output


def test_successful_push_logs_the_transferred_files(runner, env):
    with (
        patch("dsync.cli.rsync_push_dry_run", return_value=["index.html"]),
        patch("dsync.cli.backup_remote_files", return_value="/backups/x"),
        patch("dsync.cli.rsync_push_all", return_value=["index.html"]),
    ):
        result = runner.invoke(cli, ["push"], input="y\n")

    assert result.exit_code == 0
    ((args, kwargs),) = _log_calls(env)
    assert args[1] == ["index.html"]
    assert kwargs["ok"] is True


def test_failed_single_file_push_is_logged_as_failure(runner, env, config):
    (config.local_root / "index.html").write_text("<h1>hi</h1>")
    with patch("dsync.cli.push_single_file", return_value=False):
        result = runner.invoke(cli, ["push", "index.html"])

    assert result.exit_code != 0
    ((_args, kwargs),) = _log_calls(env)
    assert kwargs["ok"] is False


# ---------------------------------------------------------------------------
# Connection failures
#
# An unreachable host is the common failure in practice. It used to surface as a
# raw paramiko traceback with no log entry at all.
# ---------------------------------------------------------------------------


def test_unreachable_host_on_pull_is_reported_and_logged(runner, config):
    import paramiko

    unreachable = paramiko.ssh_exception.NoValidConnectionsError(
        {("127.0.0.1", 1): OSError("refused")}
    )
    with (
        patch("dsync.cli.load_config", return_value=config),
        patch("dsync.cli.StateManager") as state,
        patch("dsync.cli.run_hook", return_value=True),
        patch("dsync.cli.append_log") as append,
        patch("dsync.cli.SSHManager") as ssh,
    ):
        state.return_value.is_empty.return_value = True
        ssh.return_value.__enter__.side_effect = unreachable
        result = runner.invoke(cli, ["pull"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Pull failed" in result.output
    ((_args, kwargs),) = [(c.args, c.kwargs) for c in append.call_args_list]
    assert kwargs["ok"] is False


def test_unreachable_host_on_status_exits_nonzero(runner, config):
    import paramiko

    with (
        patch("dsync.cli.load_config", return_value=config),
        patch("dsync.cli.SSHManager") as ssh,
    ):
        ssh.return_value.__enter__.side_effect = paramiko.SSHException("no route")
        result = runner.invoke(cli, ["status"])

    assert result.exit_code != 0
    assert "in sync" not in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# Path confinement at the CLI boundary
# ---------------------------------------------------------------------------


def test_push_of_an_escaping_path_fails_without_uploading(runner, env, config):
    (config.local_root.parent / "secrets.env").write_text("SECRET")
    with patch("dsync.cli.push_single_file") as upload:
        result = runner.invoke(cli, ["push", "../secrets.env"])

    assert result.exit_code != 0
    upload.assert_not_called()
    assert "outside the project root" in result.output
    ((_args, kwargs),) = _log_calls(env)
    assert kwargs["ok"] is False


def test_open_of_an_escaping_path_is_refused(runner, config):
    with (
        patch("dsync.cli.load_config", return_value=config),
        patch("dsync.cli.webbrowser.open") as browser,
    ):
        result = runner.invoke(cli, ["open", "../../../etc/passwd"])

    assert result.exit_code != 0
    browser.assert_not_called()


def test_open_builds_the_url_for_an_in_project_path(runner, config):
    (config.local_root / "css").mkdir()
    (config.local_root / "css" / "style.css").write_text("body{}")
    with (
        patch("dsync.cli.load_config", return_value=config),
        patch("dsync.cli.webbrowser.open") as browser,
    ):
        result = runner.invoke(cli, ["open", "css/style.css"])

    assert result.exit_code == 0
    browser.assert_called_once_with("https://example.com/css/style.css")
