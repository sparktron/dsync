"""SSH/SFTP connection management using paramiko, plus ssh-agent helpers for rsync."""

from __future__ import annotations

import atexit
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

import click
import paramiko
from rich.console import Console
from rich.prompt import Prompt

from .config import Config

console = Console()

# Module-level caches (per process / session)
_passphrase_cache: str | None = None
_passphrase_asked: bool = False
_agent_env: dict[str, str] = {}

# Set only when *we* started the agent, so teardown never kills the user's own.
_own_agent_env: dict[str, str] = {}

# Keys we load expire on their own, so a missed teardown (SIGKILL, crash) does
# not leave the decrypted key resident indefinitely.
AGENT_KEY_LIFETIME_SECONDS = 3600


def get_passphrase(force_new: bool = False) -> str | None:
    """Prompt for the SSH key passphrase, caching it for the session.

    Args:
        force_new: If True, discard cache and prompt again (e.g., after auth failure)

    Returns None if the user provides no passphrase (empty input). A ``None``
    result is cached too, so an unencrypted key only prompts once per session.
    """
    global _passphrase_cache, _passphrase_asked
    if force_new:
        _passphrase_asked = False
        _passphrase_cache = None
    if not _passphrase_asked:
        prompt_text = "[yellow]SSH key passphrase[/] (press Enter if no passphrase)"
        user_input = Prompt.ask(prompt_text, password=True, default="")
        # Convert empty string to None (no passphrase)
        _passphrase_cache = user_input if user_input else None
        _passphrase_asked = True
    return _passphrase_cache


def _run_tool(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str] | None:
    """Run an ssh helper binary, returning None if it is missing or times out.

    ssh-agent, ssh-add and ssh-keygen are not guaranteed to be present. Letting
    a missing binary raise FileNotFoundError turns every push into a traceback;
    degrading to the caller's own environment lets ssh prompt instead.

    A timeout is honoured because ssh-add can spin forever: given SSH_ASKPASS
    plus SSH_ASKPASS_REQUIRE=force, a rejected passphrase sends it round its
    retry loop with no tty to give up on.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


def _key_fingerprint(key_path: Path) -> str:
    """Return the key's fingerprint, or "" if it cannot be read."""
    keygen = _run_tool(["ssh-keygen", "-l", "-f", str(key_path)])
    if keygen is None or keygen.returncode != 0:
        return ""
    fields = keygen.stdout.split()
    return fields[1] if len(fields) > 1 else ""


def agent_has_key(key_path: Path) -> bool:
    """True if a reachable ssh-agent already holds this key."""
    if "SSH_AUTH_SOCK" not in os.environ:
        return False
    listed = _run_tool(["ssh-add", "-l"])
    if listed is None or listed.returncode != 0:
        return False
    fingerprint = _key_fingerprint(key_path)
    return bool(fingerprint) and fingerprint in listed.stdout


def key_is_encrypted(key_path: Path) -> bool:
    """True if the private key needs a passphrase to load.

    Anything unreadable is reported as unencrypted so we do not prompt for a key
    that does not exist — the connection attempt then fails with a real error.
    """
    for key_class in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
        try:
            key_class.from_private_key_file(str(key_path))
            return False  # loaded without a passphrase
        except paramiko.ssh_exception.PasswordRequiredException:
            return True
        except Exception:
            continue  # wrong key type for this class, or unreadable
    return False


# ssh-add is bounded: a wrong passphrase must fail, not hang the whole command.
SSH_ADD_TIMEOUT_SECONDS = 20


def _ssh_add(
    key_path: Path,
    passphrase: str | None,
    env: dict[str, str],
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Add a key to the agent identified by *env*. None means it did not run.

    The askpass helper is one-shot: it deletes itself as it runs, so ssh-add's
    retry loop gets nothing on a second attempt and gives up instead of
    re-submitting the same rejected passphrase forever.
    """
    cmd = ["ssh-add", *(extra_args or []), str(key_path)]
    if not passphrase:
        return _run_tool(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=SSH_ADD_TIMEOUT_SECONDS,
        )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix="dsync_askpass_"
    ) as f:
        f.write(f"#!/bin/sh\nrm -f \"$0\"\nprintf '%s' {shlex.quote(passphrase)}\n")
        askpass_path = f.name
    os.chmod(askpass_path, 0o700)
    try:
        return _run_tool(
            cmd,
            env={
                **env,
                "SSH_ASKPASS": askpass_path,
                "SSH_ASKPASS_REQUIRE": "force",  # OpenSSH ≥ 8.4
                "DISPLAY": os.environ.get("DISPLAY", ":0"),
            },
            stdin=subprocess.DEVNULL,
            timeout=SSH_ADD_TIMEOUT_SECONDS,
        )
    finally:
        try:
            os.unlink(askpass_path)
        except OSError:
            pass  # the one-shot script already removed itself


def load_key_into_agent(key_path: Path, passphrase: str | None = None) -> bool:
    """Add a key to the user's running ssh-agent. Returns True on success.

    Used by the one-time migration away from storing the passphrase in config,
    so the move costs the user at most one prompt.
    """
    if "SSH_AUTH_SOCK" not in os.environ:
        console.print("[yellow]⚠[/] No ssh-agent is running (SSH_AUTH_SOCK is unset).")
        return False
    added = _ssh_add(key_path, passphrase, dict(os.environ))
    return added is not None and added.returncode == 0


def shutdown_agent() -> None:
    """Kill the ssh-agent this process started, if any. Never the user's own."""
    global _own_agent_env, _agent_env
    if not _own_agent_env:
        return
    _run_tool(["ssh-agent", "-k"], env={**os.environ, **_own_agent_env})
    _own_agent_env = {}
    _agent_env = {}


atexit.register(shutdown_agent)


def reset_passphrase_cache() -> None:
    """Forget the cached passphrase so the next request prompts again."""
    global _passphrase_cache, _passphrase_asked
    _passphrase_cache = None
    _passphrase_asked = False


def get_rsync_env(key_path: Path, config: Config | None = None) -> dict[str, str]:
    """
    Return an environment dict that has the SSH key loaded into an agent,
    suitable for passing to rsync subprocess calls.

    Args:
        key_path: Path to the SSH private key file
        config: Optional Config object to use stored passphrase if available

    Reuses an already-running agent if the key is already loaded;
    otherwise starts a fresh ssh-agent and adds the key via SSH_ASKPASS.
    """
    global _agent_env, _own_agent_env

    # Return cached agent env if we've already set one up this session.
    if _agent_env:
        return {**os.environ, **_agent_env}

    # If the user already has an agent running with the key loaded, use it —
    # and leave it alone: we never add to or kill an agent we did not start.
    if agent_has_key(key_path):
        _agent_env = {"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]}
        return dict(os.environ)

    passphrase = get_passphrase() if key_is_encrypted(key_path) else None

    # Start a fresh ssh-agent, which shutdown_agent() tears down at exit.
    agent_result = _run_tool(["ssh-agent", "-s"])
    new_env: dict[str, str] = {}
    if agent_result is not None:
        for line in agent_result.stdout.splitlines():
            m = re.match(r"(\w+)=([^;]+);", line)
            if m:
                new_env[m.group(1)] = m.group(2)

    if not new_env:
        # ssh-agent missing or unusable; rsync falls back to interactive prompting.
        console.print(
            "[yellow]⚠[/] ssh-agent unavailable — ssh may prompt for the key passphrase."
        )
        return dict(os.environ)

    added = _ssh_add(
        key_path,
        passphrase,
        {**os.environ, **new_env},
        extra_args=["-t", str(AGENT_KEY_LIFETIME_SECONDS)],
    )

    if added is None or added.returncode != 0:
        # Caching a broken agent env would point rsync at an agent holding no
        # keys, and ssh's own prompt is captured by _run_rsync — which looks
        # exactly like a hang. Tear it down and let ssh prompt instead.
        raw = (added.stderr or "").strip() if added else "ssh-add did not complete"
        if passphrase:
            # The one-shot askpass is spent after the first attempt, so ssh-add
            # asking a second time means the first passphrase was rejected.
            console.print(
                f"[yellow]⚠[/] ssh-agent rejected the passphrase for {key_path}."
            )
            console.print("   ssh will ask for it directly instead.")
            reset_passphrase_cache()  # do not reuse a rejected passphrase
        else:
            console.print(
                f"[yellow]⚠[/] Could not load {key_path} into ssh-agent: {raw or 'unknown error'}"
            )
        _run_tool(["ssh-agent", "-k"], env={**os.environ, **new_env})
        return dict(os.environ)

    _agent_env = new_env
    _own_agent_env = new_env
    return {**os.environ, **_agent_env}


class SSHManager:
    """
    Manages a reusable paramiko SSH/SFTP connection.

    Use as a context manager or call connect()/close() manually.
    Reconnects automatically if the transport drops.
    """

    def __init__(self, config: Config, profile: str | None = None) -> None:
        self.config = config
        self.profile = profile
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
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
        _stdin, stdout, stderr = self.client.exec_command(command)
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

    def __enter__(self) -> SSHManager:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _passphrase_for_connect(self) -> str | None:
        """The passphrase to connect with, or None if one is not needed.

        paramiko tries key_filename before agent keys, and swallows the
        PasswordRequiredException from a passphrase-less load of an encrypted
        key before falling through to the agent. So whenever the agent already
        holds this key — or the key is unencrypted — connecting with None
        succeeds and there is nothing to ask the user.
        """
        if agent_has_key(self.config.key_path):
            return None
        if not key_is_encrypted(self.config.key_path):
            return None
        return get_passphrase()

    def _do_connect(self) -> None:
        """Perform the actual paramiko connection."""
        passphrase = self._passphrase_for_connect()

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
