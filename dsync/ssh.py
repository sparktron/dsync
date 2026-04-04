"""SSH/SFTP connection management using paramiko, plus ssh-agent helpers for rsync."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import click
import paramiko
from rich.console import Console
from rich.prompt import Prompt

from .config import Config, save_config

console = Console()

# Module-level caches (per process / session)
_passphrase_cache: Optional[str] = None
_agent_env: dict[str, str] = {}


def get_passphrase(force_new: bool = False) -> Optional[str]:
    """Prompt for the SSH key passphrase, caching it for the session.

    Args:
        force_new: If True, discard cache and prompt again (e.g., after auth failure)

    Returns None if the user provides no passphrase (empty input).
    """
    global _passphrase_cache
    if force_new:
        _passphrase_cache = None
    if _passphrase_cache is None:
        prompt_text = "[yellow]SSH key passphrase[/] (press Enter if no passphrase)"
        user_input = Prompt.ask(prompt_text, password=True, default="")
        # Convert empty string to None (no passphrase)
        _passphrase_cache = user_input if user_input else None
    return _passphrase_cache


def get_rsync_env(key_path: Path) -> dict[str, str]:
    """
    Return an environment dict that has the SSH key loaded into an agent,
    suitable for passing to rsync subprocess calls.

    Reuses an already-running agent if the key is already loaded;
    otherwise starts a fresh ssh-agent and adds the key via SSH_ASKPASS.
    """
    global _agent_env

    # Return cached agent env if we've already set one up this session.
    if _agent_env:
        return {**os.environ, **_agent_env}

    # If the user already has an agent running with the key loaded, use it.
    if "SSH_AUTH_SOCK" in os.environ:
        listed = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True)
        keygen = subprocess.run(
            ["ssh-keygen", "-l", "-f", str(key_path)],
            capture_output=True,
            text=True,
        )
        if listed.returncode == 0 and keygen.returncode == 0:
            fp = keygen.stdout.split()[1] if keygen.stdout.strip() else ""
            if fp and fp in listed.stdout:
                _agent_env = {"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]}
                return dict(os.environ)

    # Start a fresh ssh-agent.
    passphrase = get_passphrase()
    agent_result = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True)
    new_env: dict[str, str] = {}
    for line in agent_result.stdout.splitlines():
        m = re.match(r"(\w+)=([^;]+);", line)
        if m:
            new_env[m.group(1)] = m.group(2)

    if not new_env:
        # ssh-agent not available; rsync will fall back to interactive prompting.
        return dict(os.environ)

    # Write a temporary askpass script that echoes the passphrase.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix="dsync_askpass_"
    ) as f:
        f.write(f"#!/bin/sh\nprintf '%s' {shlex.quote(passphrase)}\n")
        askpass_path = f.name
    os.chmod(askpass_path, 0o700)

    try:
        add_env = {
            **os.environ,
            **new_env,
            "SSH_ASKPASS": askpass_path,
            "SSH_ASKPASS_REQUIRE": "force",  # OpenSSH ≥ 8.4
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        }
        subprocess.run(
            ["ssh-add", str(key_path)],
            env=add_env,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    finally:
        try:
            os.unlink(askpass_path)
        except OSError:
            pass

    _agent_env = new_env
    return {**os.environ, **_agent_env}


class SSHManager:
    """
    Manages a reusable paramiko SSH/SFTP connection.

    Use as a context manager or call connect()/close() manually.
    Reconnects automatically if the transport drops.
    """

    def __init__(self, config: Config, profile: Optional[str] = None) -> None:
        self.config = config
        self.profile = profile
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._connection_succeeded = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def connect(self, retry: bool = True) -> None:
        """Establish SSH connection, with smart retry logic for auth failures."""
        console.print(
            f"[blue]ℹ[/] Connecting to "
            f"[bold]{self.config.host}:{self.config.port}[/]..."
        )
        try:
            self._do_connect()
            console.print("[green]✓[/] Connected")
            self._connection_succeeded = True
            self._offer_to_save_passphrase()
        except paramiko.ssh_exception.AuthenticationException as exc:
            self._handle_auth_failure(exc, retry=retry)
        except (ValueError, paramiko.ssh_exception.SSHException) as exc:
            self._handle_key_error(exc, retry=retry)
        except Exception as exc:
            self._handle_generic_error(exc, retry=retry)

    def run(self, command: str, check: bool = True) -> tuple[str, str]:
        """
        Execute a shell command on the remote host.

        Returns (stdout, stderr). Raises RuntimeError if check=True and
        the command exits non-zero.
        """
        stdin, stdout, stderr = self.client.exec_command(command)
        out = stdout.read().decode()
        err = stderr.read().decode()
        exit_code = stdout.channel.recv_exit_status()
        if check and exit_code != 0:
            raise RuntimeError(
                f"Remote command failed (exit {exit_code}): {command}\n{err}"
            )
        return out, err

    def close(self) -> None:
        """Close SFTP and SSH connections."""
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Properties — auto-reconnect on access
    # ------------------------------------------------------------------

    @property
    def client(self) -> paramiko.SSHClient:
        """Return the active SSH client, reconnecting if needed."""
        self._ensure_connected()
        assert self._client is not None
        return self._client

    @property
    def sftp(self) -> paramiko.SFTPClient:
        """Return the active SFTP client, reconnecting if needed."""
        self._ensure_connected()
        if self._sftp is None:
            assert self._client is not None
            self._sftp = self._client.open_sftp()
        return self._sftp

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "SSHManager":
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_connect(self) -> None:
        """Perform the actual paramiko connection."""
        # Use stored passphrase if available, otherwise prompt
        if self.config.passphrase is not None:
            passphrase = self.config.passphrase
        else:
            passphrase = get_passphrase()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.user,
            key_filename=str(self.config.key_path),
            passphrase=passphrase,
            timeout=30,
        )
        self._client = client
        self._sftp = None  # opened lazily

    def _offer_to_save_passphrase(self) -> None:
        """Offer to save the passphrase to config for future use."""
        # Only offer if we successfully connected and don't already have a saved passphrase
        if not self._connection_succeeded or self.config.passphrase is not None:
            return

        passphrase = _passphrase_cache
        if passphrase is None:
            return

        response = Prompt.ask(
            "[yellow]Save SSH passphrase to config for future use?[/] (yes/no)",
            choices=["yes", "no"],
            default="no"
        )
        if response == "yes":
            self.config.passphrase = passphrase
            save_config(self.config, profile=self.profile)
            console.print("[green]✓[/] Passphrase saved to config")
        else:
            console.print("[dim]Passphrase not saved[/]")

    def _handle_auth_failure(self, exc: Exception, retry: bool = True) -> None:
        """Handle authentication failures (wrong passphrase or key rejection)."""
        console.print(f"[red]✗[/] Authentication failed: {exc}")
        if not retry:
            raise exc

        # Clear the cached passphrase so user can try again
        console.print("[yellow]The passphrase or key may be incorrect.[/]")
        if click.confirm("Retry with a different passphrase?", default=True):
            time.sleep(1)
            try:
                self._do_connect_with_new_passphrase()
                console.print("[green]✓[/] Connected")
                self._connection_succeeded = True
                self._offer_to_save_passphrase()
            except Exception as e:
                console.print(f"[red]✗[/] Connection still failed: {e}")
                raise
        else:
            raise exc

    def _handle_key_error(self, exc: Exception, retry: bool = True) -> None:
        """Handle key loading errors (invalid key format, wrong passphrase for key)."""
        error_msg = str(exc).lower()
        if "password" in error_msg or "passphrase" in error_msg or "salt" in error_msg:
            console.print("[red]✗[/] Wrong passphrase or encrypted key issue")
            if retry:
                console.print("[yellow]The passphrase appears to be incorrect.[/]")
                if click.confirm("Retry with a different passphrase?", default=True):
                    time.sleep(1)
                    try:
                        self._do_connect_with_new_passphrase()
                        console.print("[green]✓[/] Connected")
                        self._connection_succeeded = True
                        self._offer_to_save_passphrase()
                    except Exception as e:
                        console.print(f"[red]✗[/] Connection still failed: {e}")
                        raise
                else:
                    raise exc
            else:
                raise exc
        else:
            console.print(f"[red]✗[/] Key error: {exc}")
            raise exc

    def _handle_generic_error(self, exc: Exception, retry: bool = True) -> None:
        """Handle network and other connection errors."""
        console.print(f"[red]✗[/] Connection failed: {exc}")
        if retry:
            console.print("[yellow]Retrying in 3 seconds...[/]")
            time.sleep(3)
            try:
                self._do_connect()
                console.print("[green]✓[/] Connected")
                self._connection_succeeded = True
                self._offer_to_save_passphrase()
            except Exception as e:
                console.print(f"[red]✗[/] Retry failed: {e}")
                raise
        else:
            raise exc

    def _do_connect_with_new_passphrase(self) -> None:
        """Connect with a fresh passphrase prompt, clearing the cache."""
        # Force a new passphrase prompt
        passphrase = get_passphrase(force_new=True)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.user,
            key_filename=str(self.config.key_path),
            passphrase=passphrase,
            timeout=30,
        )
        self._client = client
        self._sftp = None  # opened lazily

    def _ensure_connected(self) -> None:
        """Reconnect if the transport is missing or dropped."""
        if self._client is None:
            self.connect()
            return
        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                console.print("[yellow]⚠[/] Connection lost — reconnecting...")
                self._sftp = None
                self.connect(retry=True)
        except Exception:
            console.print("[yellow]⚠[/] Connection check failed — reconnecting...")
            self._sftp = None
            self.connect(retry=True)
