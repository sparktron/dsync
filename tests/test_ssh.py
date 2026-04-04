"""Unit tests for SSH connection and error handling."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import paramiko
from pathlib import Path

from dsync.ssh import SSHManager, get_passphrase, _passphrase_cache
from dsync.config import Config


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    return Config({
        "host": "example.com",
        "port": 22,
        "user": "testuser",
        "key_path": "~/.ssh/id_rsa",
        "local_root": "~/project",
        "remote_root": "/var/www/",
        "site_url": "https://example.com",
    })


@pytest.fixture
def ssh_manager(mock_config):
    """Create an SSHManager instance for testing."""
    return SSHManager(mock_config, profile=None)


class TestPassphraseCaching:
    """Test passphrase caching behavior."""

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

    @patch("dsync.ssh._passphrase_cache", None)
    @patch("dsync.ssh.Prompt.ask")
    def test_empty_passphrase_converted_to_none(self, mock_prompt):
        """Test that empty passphrase input is converted to None."""
        mock_prompt.return_value = ""

        result = get_passphrase()
        assert result is None

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
    def test_successful_connection(self, mock_ssh_client_class, ssh_manager, mock_config):
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

    @patch("paramiko.SSHClient")
    def test_uses_stored_passphrase_from_config(self, mock_ssh_client_class, ssh_manager, mock_config):
        """Test that stored passphrase in config is used without prompting."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_config.passphrase = "stored_passphrase"

        with patch("dsync.ssh.get_passphrase") as mock_get_pass:
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=False)

        # get_passphrase should not be called when passphrase is in config
        mock_get_pass.assert_not_called()

        # Verify the stored passphrase was used
        call_kwargs = mock_client.connect.call_args[1]
        assert call_kwargs["passphrase"] == "stored_passphrase"


class TestAuthenticationFailure:
    """Test authentication failure handling."""

    @patch("paramiko.SSHClient")
    @patch("click.confirm")
    def test_auth_failure_prompts_for_retry(self, mock_confirm, mock_ssh_client_class, ssh_manager):
        """Test that auth failure prompts user to retry."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = True

        # First call raises auth error, second call succeeds
        mock_client.connect.side_effect = [
            paramiko.ssh_exception.AuthenticationException("Authentication failed"),
            None  # Success on second attempt
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
    def test_auth_failure_no_retry_if_user_declines(self, mock_confirm, mock_ssh_client_class, ssh_manager):
        """Test that auth failure respects user's choice not to retry."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = False
        mock_client.connect.side_effect = paramiko.ssh_exception.AuthenticationException("Authentication failed")

        with patch("dsync.ssh.get_passphrase", return_value="wrongpass"):
            with patch("dsync.ssh.console"):
                with pytest.raises(paramiko.ssh_exception.AuthenticationException):
                    ssh_manager.connect(retry=True)


class TestKeyError:
    """Test SSH key loading errors."""

    @patch("paramiko.SSHClient")
    @patch("click.confirm")
    def test_wrong_passphrase_for_encrypted_key(self, mock_confirm, mock_ssh_client_class, ssh_manager):
        """Test handling of wrong passphrase for encrypted key."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = True

        # Simulate passphrase validation error
        mock_client.connect.side_effect = [
            ValueError("password and salt must not be empty"),
            None  # Success on retry
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
    def test_network_error_retries_once(self, mock_sleep, mock_ssh_client_class, ssh_manager):
        """Test that network errors trigger automatic retry."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client

        # First call raises network error, second succeeds
        mock_client.connect.side_effect = [
            OSError("Connection refused"),
            None  # Success on retry
        ]

        with patch("dsync.ssh.get_passphrase", return_value="pass"):
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=True)

        # Should have slept before retry
        mock_sleep.assert_called_once_with(3)
        # Should have attempted connection twice
        assert mock_client.connect.call_count == 2

    @patch("paramiko.SSHClient")
    def test_network_error_no_retry_when_disabled(self, mock_ssh_client_class, ssh_manager):
        """Test that network errors are not retried when retry=False."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_client.connect.side_effect = OSError("Connection refused")

        with patch("dsync.ssh.get_passphrase", return_value="pass"):
            with patch("dsync.ssh.console"):
                with pytest.raises(OSError):
                    ssh_manager.connect(retry=False)


class TestPassphraseSaving:
    """Test passphrase saving functionality."""

    @patch("paramiko.SSHClient")
    @patch("dsync.ssh.save_config")
    def test_offer_to_save_passphrase_on_success(self, mock_save, mock_ssh_client_class, ssh_manager):
        """Test that user is offered to save passphrase after successful connection."""
        import dsync.ssh

        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client

        # Set the global passphrase cache so _offer_to_save_passphrase can access it
        original_cache = dsync.ssh._passphrase_cache
        try:
            with patch("dsync.ssh.get_passphrase", return_value="mypass"):
                with patch("dsync.ssh.Prompt.ask", return_value="yes"):
                    with patch("dsync.ssh.console"):
                        dsync.ssh._passphrase_cache = "mypass"
                        ssh_manager.connect(retry=False)

            # save_config should have been called
            mock_save.assert_called_once()
            assert ssh_manager.config.passphrase == "mypass"
        finally:
            dsync.ssh._passphrase_cache = original_cache

    @patch("paramiko.SSHClient")
    @patch("dsync.ssh.Prompt.ask")
    @patch("dsync.ssh.save_config")
    def test_skip_save_passphrase_when_user_declines(self, mock_save, mock_prompt, mock_ssh_client_class, ssh_manager):
        """Test that passphrase is not saved when user declines."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_prompt.return_value = "no"

        with patch("dsync.ssh.get_passphrase", return_value="mypass"):
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=False)

        # save_config should not have been called
        mock_save.assert_not_called()

    @patch("paramiko.SSHClient")
    def test_never_offer_to_save_if_already_saved(self, mock_ssh_client_class, ssh_manager, mock_config):
        """Test that save offer is skipped if passphrase already saved in config."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        ssh_manager.config.passphrase = "alreadysaved"

        with patch("dsync.ssh.Prompt.ask") as mock_prompt:
            with patch("dsync.ssh.console"):
                ssh_manager.connect(retry=False)

        # Should never prompt to save
        mock_prompt.assert_not_called()


class TestContextManager:
    """Test context manager behavior."""

    @patch("paramiko.SSHClient")
    def test_context_manager_closes_connection(self, mock_ssh_client_class, ssh_manager):
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
    def test_context_manager_with_connection_failure(self, mock_ssh_client_class, ssh_manager):
        """Test that connection failure is properly raised from context manager."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_client.connect.side_effect = paramiko.ssh_exception.AuthenticationException("Auth failed")

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
    def test_friendly_error_for_wrong_passphrase(self, mock_confirm, mock_ssh_client_class, ssh_manager):
        """Test that wrong passphrase shows helpful error message."""
        mock_client = MagicMock()
        mock_ssh_client_class.return_value = mock_client
        mock_confirm.return_value = False
        mock_client.connect.side_effect = ValueError("password and salt must not be empty")

        with patch("dsync.ssh.get_passphrase", return_value="wrongpass"):
            with patch("dsync.ssh.console") as mock_console:
                with pytest.raises(ValueError):
                    ssh_manager.connect(retry=True)

                # Verify helpful message was printed
                printed_messages = [str(call) for call in mock_console.print.call_args_list]
                assert any("passphrase" in str(msg).lower() for msg in printed_messages)
