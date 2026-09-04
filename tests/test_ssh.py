"""Unit tests for SSH connection, passphrase handling and agent lifetime."""

import json
import os
import stat
import subprocess
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from dsync.config import Config
from dsync.ssh import SSHManager, get_passphrase


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    return Config(
        {
            "host": "example.com",
            "port": 22,
            "user": "testuser",
            "key_path": "~/.ssh/id_rsa",
            "local_root": "~/project",
            "remote_root": "/var/www/",
            "site_url": "https://example.com",
        }
    )


@pytest.fixture
def ssh_manager(mock_config):
    """Create an SSHManager instance for testing."""
    return SSHManager(mock_config, profile=None)


class TestPassphraseCaching:
    """Test passphrase caching behavior."""

    @patch("dsync.ssh._passphrase_asked", False)
    @patch("dsync.ssh._passphrase_cache", None)
    @patch("dsync.ssh.Prompt.ask")
    def test_passphrase_cached_after_first_prompt(self, mock_prompt):
        """Test that passphrase is cached after first prompt."""
        mock_prompt.return_value = "mypassphrase"

        # First call should prompt
        result1 = get_passphrase()
        assert result1 == "mypassphrase"
        assert mock_prompt.call_count == 1

        # Second call should return cached value without prompting
        result2 = get_passphrase()
        assert result2 == "mypassphrase"
        assert mock_prompt.call_count == 1  # No additional call

    @patch("dsync.ssh._passphrase_asked", False)
    @patch("dsync.ssh._passphrase_cache", None)
    @patch("dsync.ssh.Prompt.ask")
    def test_empty_passphrase_converted_to_none(self, mock_prompt):
        """Test that empty passphrase input is converted to None."""
        mock_prompt.return_value = ""

        result = get_passphrase()
        assert result is None

    @patch("dsync.ssh._passphrase_asked", False)
    @patch("dsync.ssh._passphrase_cache", None)
    @patch("dsync.ssh.Prompt.ask")
    def test_force_new_passphrase_clears_cache(self, mock_prompt):
        """Test that force_new=True prompts again even if cached."""
        mock_prompt.side_effect = ["firstpass", "secondpass"]

        result1 = get_passphrase()
        assert result1 == "firstpass"

        result2 = get_passphrase(force_new=True)
        assert result2 == "secondpass"
        assert mock_prompt.call_count == 2


class TestConnectionSuccess:
    """Test successful connection scenarios."""

    @patch("paramiko.SSHClient")
    def test_successful_connection(
        self, mock_ssh_client_class, ssh_manager, mock_config
    ):
        """Test successful SSH connection."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client

        with patch("dsync.ssh.get_passphrase", return_value="correctpass"):
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=False)

        # Verify connection was attempted
        mock_client.set_missing_host_key_policy.assert_called_once()
        mock_client.connect.assert_called_once()
        assert ssh_manager._client == mock_client
        assert ssh_manager._connection_succeeded

    @patch("paramiko.SSHClient")
    def test_connection_with_none_passphrase(self, mock_ssh_client_class, ssh_manager):
        """Test connection when no passphrase is needed."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client

        with patch("dsync.ssh.get_passphrase", return_value=None):
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=False)

        # Verify connect was called with passphrase=None
        call_kwargs = mock_client.connect.call_args[1]
        assert call_kwargs["passphrase"] is None


class TestAuthenticationFailure:
    """Test authentication failure handling."""

    @patch("paramiko.SSHClient")
    @patch("click.confirm")
    def test_auth_failure_prompts_for_retry(
        self, mock_confirm, mock_ssh_client_class, ssh_manager
    ):
        """Test that auth failure prompts user to retry."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = True

        # First call raises auth error, second call succeeds
        mock_client.connect.side_effect = [
            paramiko.ssh_exception.AuthenticationException("Authentication failed"),
            None,  # Success on second attempt
        ]

        with patch("dsync.ssh.get_passphrase", return_value="wrongpass"):
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=True)

        # Should have called confirm
        mock_confirm.assert_called_once()
        # Should have attempted connection twice
        assert mock_client.connect.call_count == 2

    @patch("paramiko.SSHClient")
    @patch("click.confirm")
    def test_auth_failure_no_retry_if_user_declines(
        self, mock_confirm, mock_ssh_client_class, ssh_manager
    ):
        """Test that auth failure respects user's choice not to retry."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = False
        mock_client.connect.side_effect = (
            paramiko.ssh_exception.AuthenticationException("Authentication failed")
        )

        with patch("dsync.ssh.get_passphrase", return_value="wrongpass"):
            with patch("dsync.ssh.console"):
                with pytest.raises(paramiko.ssh_exception.AuthenticationException):
                    ssh_manager.connect(retry=True)


class TestKeyError:
    """Test SSH key loading errors."""

    @patch("paramiko.SSHClient")
    @patch("click.confirm")
    def test_wrong_passphrase_for_encrypted_key(
        self, mock_confirm, mock_ssh_client_class, ssh_manager
    ):
        """Test handling of wrong passphrase for encrypted key."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = True

        # Simulate passphrase validation error
        mock_client.connect.side_effect = [
            ValueError("password and salt must not be empty"),
            None,  # Success on retry
        ]

        with patch("dsync.ssh.get_passphrase", return_value="wrongpass"):
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=True)

        # Should prompt for new passphrase
        mock_confirm.assert_called_once()

    @patch("paramiko.SSHClient")
    def test_key_file_not_found_error(self, mock_ssh_client_class, ssh_manager):
        """Test handling when key file doesn't exist."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_client.connect.side_effect = FileNotFoundError("Key file not found")

        with patch("dsync.ssh.get_passphrase", return_value="pass"):
            with patch("dsync.ssh.console"):
                with pytest.raises(FileNotFoundError):
                    ssh_manager.connect(retry=False)


class TestGenericErrors:
    """Test handling of network and other generic errors."""

    @patch("paramiko.SSHClient")
    @patch("time.sleep")
    def test_network_error_retries_once(
        self, mock_sleep, mock_ssh_client_class, ssh_manager
    ):
        """Test that network errors trigger automatic retry."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client

        # First call raises network error, second succeeds
        mock_client.connect.side_effect = [
            OSError("Connection refused"),
            None,  # Success on retry
        ]

        with patch("dsync.ssh.get_passphrase", return_value="pass"):
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=True)

        # Should have slept before retry
        mock_sleep.assert_called_once_with(3)
        # Should have attempted connection twice
        assert mock_client.connect.call_count == 2

    @patch("paramiko.SSHClient")
    def test_network_error_no_retry_when_disabled(
        self, mock_ssh_client_class, ssh_manager
    ):
        """Test that network errors are not retried when retry=False."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_client.connect.side_effect = OSError("Connection refused")

        with patch("dsync.ssh.get_passphrase", return_value="pass"):
            with patch("dsync.ssh.console"):
                with pytest.raises(OSError):
                    ssh_manager.connect(retry=False)


class TestContextManager:
    """Test context manager behavior."""

    @patch("paramiko.SSHClient")
    def test_context_manager_closes_connection(
        self, mock_ssh_client_class, ssh_manager
    ):
        """Test that context manager properly closes connection."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client

        with patch("dsync.ssh.get_passphrase", return_value="pass"):
            with patch("dsync.ssh.console"):
                with ssh_manager:
                    assert ssh_manager._client == mock_client

        # Verify close was called
        mock_client.close.assert_called_once()

    @patch("paramiko.SSHClient")
    def test_context_manager_with_connection_failure(
        self, mock_ssh_client_class, ssh_manager
    ):
        """Test that connection failure is properly raised from context manager."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_client.connect.side_effect = (
            paramiko.ssh_exception.AuthenticationException("Auth failed")
        )

        with patch("dsync.ssh.get_passphrase", return_value="wrongpass"):
            with patch("dsync.ssh.console"):
                with patch("click.confirm", return_value=False):
                    with pytest.raises(paramiko.ssh_exception.AuthenticationException):
                        with ssh_manager:
                            pass


class TestErrorMessages:
    """Test that error messages are user-friendly."""

    @patch("paramiko.SSHClient")
    @patch("click.confirm")
    def test_friendly_error_for_wrong_passphrase(
        self, mock_confirm, mock_ssh_client_class, ssh_manager
    ):
        """Test that wrong passphrase shows helpful error message."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = False
        mock_client.connect.side_effect = ValueError(
            "password and salt must not be empty"
        )

        with patch("dsync.ssh.get_passphrase", return_value="wrongpass"):
            with patch("dsync.ssh.console") as mock_console:
                with pytest.raises(ValueError):
                    ssh_manager.connect(retry=True)

                # Verify helpful message was printed
                printed_messages = [
                    str(call) for call in mock_console.print.call_args_list
                ]
                assert any("passphrase" in str(msg).lower() for msg in printed_messages)


class TestMissingSSHHelpers:
    """ssh-agent and friends are not guaranteed to be installed."""

    def test_run_tool_returns_none_when_binary_is_absent(self):
        from dsync.ssh import _run_tool

        assert _run_tool(["definitely-not-a-real-binary-xyz"]) is None

    def test_run_tool_returns_result_when_binary_exists(self):
        from dsync.ssh import _run_tool

        result = _run_tool(["echo", "hello"])
        assert result is not None
        assert result.stdout.strip() == "hello"

    def test_missing_ssh_agent_degrades_instead_of_raising(self, monkeypatch, tmp_path):
        """Without ssh-agent, rsync should still run and let ssh prompt."""
        import dsync.ssh as mod

        monkeypatch.setattr(mod, "_agent_env", {})
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.setattr(mod, "_run_tool", lambda *a, **k: None)

        with (
            patch("dsync.ssh.console"),
            patch("dsync.ssh.get_passphrase", return_value=None),
        ):
            env = mod.get_rsync_env(tmp_path / "id_rsa")

        assert isinstance(env, dict)
        assert "PATH" in env


class TestPassphraseIsNotStored:
    """dsync no longer writes the SSH key passphrase to disk."""

    def test_to_dict_never_serialises_the_passphrase(self, mock_config):
        mock_config.passphrase = "super-secret"
        assert "passphrase" not in mock_config.to_dict()

    def test_saved_config_is_owner_only(self, mock_config, tmp_path, monkeypatch):
        import dsync.config as cfgmod

        target = tmp_path / ".dsync" / "config.json"
        monkeypatch.setattr(cfgmod, "CONFIG_FILE", target)
        mock_config.passphrase = "super-secret"
        cfgmod.save_config(mock_config, profile=None)

        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert "super-secret" not in target.read_text()

    def test_no_save_prompt_after_a_successful_connection(self, ssh_manager):
        with (
            patch("paramiko.SSHClient"),
            patch("dsync.ssh.get_passphrase", return_value="mypass"),
            patch("dsync.ssh.console"),
            patch("dsync.ssh.Prompt.ask") as prompt,
        ):
            ssh_manager.connect(retry=False)
        prompt.assert_not_called()


class TestStoredPassphraseMigration:
    """An existing cleartext passphrase is removed on first load."""

    @pytest.fixture
    def legacy(self, tmp_path, monkeypatch):
        import dsync.config as cfgmod

        target = tmp_path / ".dsync" / "config.json"
        target.parent.mkdir(parents=True)
        target.write_text(
            json.dumps(
                {
                    "host": "example.com",
                    "port": 22,
                    "user": "testuser",
                    "key_path": str(tmp_path / "id_rsa"),
                    "local_root": str(tmp_path),
                    "remote_root": "/var/www/",
                    "site_url": "https://example.com",
                    "passphrase": "super-secret",
                }
            )
        )
        monkeypatch.setattr(cfgmod, "CONFIG_FILE", target)
        return cfgmod, target

    def test_passphrase_is_stripped_from_the_file(self, legacy):
        cfgmod, target = legacy
        with (
            patch("dsync.config.Confirm.ask", return_value=False),
            patch("dsync.config.console"),
        ):
            config = cfgmod.load_config(profile=None)

        assert config.passphrase is None
        assert "super-secret" not in target.read_text()
        assert "passphrase" not in json.loads(target.read_text())
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_migration_offers_to_load_the_key_into_the_agent(self, legacy):
        cfgmod, _target = legacy
        with (
            patch("dsync.config.Confirm.ask", return_value=True),
            patch("dsync.ssh.load_key_into_agent", return_value=True) as load,
            patch("dsync.config.console"),
        ):
            cfgmod.load_config(profile=None)
        load.assert_called_once()
        assert load.call_args[0][1] == "super-secret"

    def test_migration_is_a_no_op_without_a_stored_passphrase(self, legacy):
        cfgmod, target = legacy
        data = json.loads(target.read_text())
        del data["passphrase"]
        target.write_text(json.dumps(data))
        with patch("dsync.config.Confirm.ask") as confirm:
            cfgmod.load_config(profile=None)
        confirm.assert_not_called()


class TestAgentFirstConnect:
    """The user is only asked for a passphrase when the agent cannot answer."""

    def test_no_prompt_when_the_agent_already_holds_the_key(self, ssh_manager):
        with (
            patch("dsync.ssh.agent_has_key", return_value=True),
            patch("dsync.ssh.get_passphrase") as prompt,
        ):
            assert ssh_manager._passphrase_for_connect() is None
        prompt.assert_not_called()

    def test_no_prompt_for_an_unencrypted_key(self, ssh_manager):
        with (
            patch("dsync.ssh.agent_has_key", return_value=False),
            patch("dsync.ssh.key_is_encrypted", return_value=False),
            patch("dsync.ssh.get_passphrase") as prompt,
        ):
            assert ssh_manager._passphrase_for_connect() is None
        prompt.assert_not_called()

    def test_prompts_for_an_encrypted_key_with_a_cold_agent(self, ssh_manager):
        with (
            patch("dsync.ssh.agent_has_key", return_value=False),
            patch("dsync.ssh.key_is_encrypted", return_value=True),
            patch("dsync.ssh.get_passphrase", return_value="secret") as prompt,
        ):
            assert ssh_manager._passphrase_for_connect() == "secret"
        prompt.assert_called_once()


class TestAgentLifetime:
    """dsync tears down only the agent it started."""

    def test_shutdown_kills_an_agent_we_started(self, monkeypatch):
        import dsync.ssh as mod

        monkeypatch.setattr(mod, "_own_agent_env", {"SSH_AGENT_PID": "424242"})
        calls = []
        monkeypatch.setattr(mod, "_run_tool", lambda cmd, **kw: calls.append(cmd))
        mod.shutdown_agent()
        assert calls == [["ssh-agent", "-k"]]
        assert mod._own_agent_env == {}

    def test_shutdown_leaves_the_users_own_agent_alone(self, monkeypatch):
        import dsync.ssh as mod

        monkeypatch.setattr(mod, "_own_agent_env", {})
        calls = []
        monkeypatch.setattr(mod, "_run_tool", lambda cmd, **kw: calls.append(cmd))
        mod.shutdown_agent()
        assert calls == []

    def test_key_is_added_with_a_bounded_lifetime(self, monkeypatch, tmp_path):
        import dsync.ssh as mod

        monkeypatch.setattr(mod, "_agent_env", {})
        monkeypatch.setattr(mod, "_own_agent_env", {})
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.setattr(mod, "key_is_encrypted", lambda p: False)

        seen = []

        def fake_tool(cmd, **kwargs):
            seen.append(cmd)
            if cmd[0] == "ssh-agent":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="SSH_AUTH_SOCK=/tmp/s.1; export SSH_AUTH_SOCK;\n"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(mod, "_run_tool", fake_tool)
        with patch("dsync.ssh.console"):
            mod.get_rsync_env(tmp_path / "id_rsa")

        add = next(c for c in seen if c[0] == "ssh-add" and "-t" in c)
        assert add[1] == "-t"
        assert int(add[2]) == mod.AGENT_KEY_LIFETIME_SECONDS


class TestSshAddFailure:
    """A failed ssh-add must not leave rsync pointed at an empty agent."""

    def test_failed_add_does_not_cache_a_broken_agent(self, monkeypatch, tmp_path):
        import dsync.ssh as mod

        monkeypatch.setattr(mod, "_agent_env", {})
        monkeypatch.setattr(mod, "_own_agent_env", {})
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.setattr(mod, "key_is_encrypted", lambda p: True)
        monkeypatch.setattr(mod, "get_passphrase", lambda *a, **k: "wrong")

        killed = []

        def fake_tool(cmd, **kwargs):
            if cmd[0] == "ssh-agent" and "-k" in cmd:
                killed.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "ssh-agent":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="SSH_AUTH_SOCK=/tmp/s.1; export SSH_AUTH_SOCK;\n"
                )
            # ssh-add rejects the passphrase
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Bad passphrase"
            )

        monkeypatch.setattr(mod, "_run_tool", fake_tool)
        with patch("dsync.ssh.console"):
            env = mod.get_rsync_env(tmp_path / "id_rsa")

        assert mod._agent_env == {}, "a broken agent env must not be cached"
        assert env.get("SSH_AUTH_SOCK") != "/tmp/s.1"
        assert killed, "the agent we started should be torn down"
        assert mod._passphrase_cache is None, "a rejected passphrase must not persist"


class TestSshAddCannotHang:
    """A rejected passphrase must fail fast, not spin forever.

    Given SSH_ASKPASS plus SSH_ASKPASS_REQUIRE=force, ssh-add re-runs the askpass
    helper when a passphrase is refused, and with no tty to fall back to it never
    gives up — observed spinning indefinitely at 100% CPU. Two guards: the askpass
    script is one-shot (it removes itself as it runs, so the retry finds nothing)
    and the call carries a timeout.
    """

    def test_run_tool_returns_none_on_timeout(self):
        from dsync.ssh import _run_tool

        assert _run_tool(["sleep", "5"], timeout=0.2) is None

    def test_ssh_add_is_called_with_a_timeout(self, monkeypatch, tmp_path):
        import dsync.ssh as mod

        seen = {}

        def fake_tool(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(mod, "_run_tool", fake_tool)
        mod._ssh_add(tmp_path / "id_rsa", "secret", {}, extra_args=["-t", "60"])

        assert seen["kwargs"]["timeout"] == mod.SSH_ADD_TIMEOUT_SECONDS
        assert seen["cmd"][:3] == ["ssh-add", "-t", "60"]

    def test_askpass_script_is_one_shot(self, monkeypatch, tmp_path):
        """The helper deletes itself, so a second attempt gets nothing."""
        import dsync.ssh as mod

        captured = {}

        def fake_tool(cmd, **kwargs):
            path = kwargs["env"]["SSH_ASKPASS"]
            captured["first"] = subprocess.run(
                [path], capture_output=True, text=True
            ).stdout
            try:
                second = subprocess.run([path], capture_output=True, text=True)
                captured["second_failed"] = second.returncode != 0
            except FileNotFoundError:
                # The script removed itself — exactly what stops the retry loop.
                captured["second_failed"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(mod, "_run_tool", fake_tool)
        mod._ssh_add(tmp_path / "id_rsa", "hunter2", dict(os.environ))

        assert captured["first"] == "hunter2"
        assert captured["second_failed"], "the askpass helper must not answer twice"

    def test_unencrypted_key_skips_the_askpass_dance(self, monkeypatch, tmp_path):
        import dsync.ssh as mod

        seen = {}

        def fake_tool(cmd, **kwargs):
            seen["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(mod, "_run_tool", fake_tool)
        mod._ssh_add(tmp_path / "id_rsa", None, {})
        assert "SSH_ASKPASS" not in seen["env"]
