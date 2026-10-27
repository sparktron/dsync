"""SSH/SFTP connection management using paramiko, plus ssh-agent helpers for rsync."""

from __future__ import annotations

import atexit
import base64
import hashlib
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
_passphrase_asked: bool = False
_agent_env: dict[str, str] = {}

# Upper bound on how long a key stays loaded in an agent dsync started.
AGENT_KEY_LIFETIME_S = 3600

KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


def _kill_agent(agent_env: dict[str, str]) -> None:
    """Stop an ssh-agent that dsync started (registered with atexit)."""
    if "SSH_AGENT_PID" not in agent_env:
        return
    _run_tool(
        ["ssh-agent", "-k"],
        env={**os.environ, **agent_env},
    )


class _AcceptNewPolicy(paramiko.MissingHostKeyPolicy):
    """Paramiko equivalent of OpenSSH's ``StrictHostKeyChecking=accept-new``.

    A host with no known key is trusted on first contact and recorded in
    ~/.ssh/known_hosts (shared with the ssh that rsync runs). A host whose key
    *changed* never reaches this policy: paramiko raises BadHostKeyException.

    Fails closed: if the key cannot be recorded, the connection is refused,
    since an unwritable store would otherwise make every key "new".
    """

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        try:
            KNOWN_HOSTS.parent.mkdir(mode=0o700, exist_ok=True)
            with KNOWN_HOSTS.open("a") as f:
                f.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")
        except OSError as exc:
            raise paramiko.SSHException(
                f"Refusing unknown host {hostname}: could not record its key "
                f"in {KNOWN_HOSTS}: {exc}"
            ) from exc
        client.get_host_keys().add(hostname, key.get_name(), key)
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest())
        fingerprint = "SHA256:" + digest.decode().rstrip("=")
        console.print(
            f"[yellow]⚠[/] New host key for {hostname} "
            f"({key.get_name()} {fingerprint}) — added to {KNOWN_HOSTS}"
        )


def _load_known_hosts(client: paramiko.SSHClient) -> None:
    """Load ~/.ssh/known_hosts so a changed server key raises BadHostKeyException.

    Passes the path explicitly: paramiko's no-argument form silently ignores
    read errors, which would leave no keys loaded and let _AcceptNewPolicy
    trust any key. Only a genuinely absent file (first use) is tolerated.
    """
    if not KNOWN_HOSTS.exists():
        return
    try:
        client.load_system_host_keys(str(KNOWN_HOSTS))
    except OSError as exc:
        raise paramiko.SSHException(
            f"Cannot read {KNOWN_HOSTS} to verify the server: {exc}"
        ) from exc


def get_passphrase(force_new: bool = False) -> Optional[str]:
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


def reset_passphrase_cache() -> None:
    """Forget a rejected passphrase before the next attempt."""
    global _passphrase_cache, _passphrase_asked
    _passphrase_cache = None
    _passphrase_asked = False


SSH_ADD_TIMEOUT_SECONDS = 20


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
        kwargs.setdefault("timeout", SSH_ADD_TIMEOUT_SECONDS)
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False, **kwargs
        )
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
            pass


def get_rsync_env(key_path: Path, config: Optional[Config] = None) -> dict[str, str]:
    """
    Return an environment dict that has the SSH key loaded into an agent,
    suitable for passing to rsync subprocess calls.

    Args:
        key_path: Path to the SSH private key file
        config: Optional Config object to use stored passphrase if available

    Reuses an already-running agent if the key is already loaded;
    otherwise starts a fresh ssh-agent and adds the key via SSH_ASKPASS.
    """
    global _agent_env

    # Return cached agent env if we've already set one up this session.
    if _agent_env:
        return {**os.environ, **_agent_env}

    if agent_has_key(key_path):
        return dict(os.environ)

    # Start a fresh ssh-agent.
    # Use stored passphrase from config if available, otherwise prompt
    if config and config.passphrase is not None:
        passphrase = config.passphrase
    elif not key_is_encrypted(key_path):
        passphrase = None
    else:
        passphrase = get_passphrase()
    agent_result = _run_tool(["ssh-agent", "-s"])
    if agent_result is None or agent_result.returncode != 0:
        return dict(os.environ)
    new_env: dict[str, str] = {}
    for line in agent_result.stdout.splitlines():
        m = re.match(r"(\w+)=([^;]+);", line)
        if m:
            new_env[m.group(1)] = m.group(2)

    if not new_env:
        # ssh-agent not available; rsync will fall back to interactive prompting.
        return dict(os.environ)

    try:
        added = _ssh_add(
            key_path,
            passphrase,
            {**os.environ, **new_env},
            extra_args=["-t", str(AGENT_KEY_LIFETIME_S)],
        )
    except BaseException:
        _kill_agent(new_env)
        raise
    if added is None or added.returncode != 0:
        _kill_agent(new_env)
        reset_passphrase_cache()
        raise paramiko.SSHException(
            "SSH key could not be loaded; check its passphrase."
        )

    # Only successful agents need deferred cleanup; failed ones are already dead.
    atexit.register(_kill_agent, dict(new_env))
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
        except paramiko.ssh_exception.BadHostKeyException as exc:
            # Never retry or prompt through this: it is the MITM signal.
            console.print(
                f"[red]✗[/] Host key for {self.config.host} does not match "
                f"{KNOWN_HOSTS}. If the server's key really changed, remove "
                f"the old entry (ssh-keygen -R) and reconnect."
            )
            raise exc
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

    def _do_connect(self, force_new_passphrase: bool = False) -> None:
        """Perform the actual paramiko connection.

        With *force_new_passphrase*, prompt afresh instead of using the cached
        or stored passphrase (used after an authentication failure).
        """
        if force_new_passphrase:
            passphrase = get_passphrase(force_new=True)
        elif self.config.passphrase is not None:
            passphrase = self.config.passphrase
        elif agent_has_key(self.config.key_path) or (
            self.config.key_path.is_file()
            and not key_is_encrypted(self.config.key_path)
        ):
            passphrase = None
        else:
            passphrase = get_passphrase()

        client = paramiko.SSHClient()
        _load_known_hosts(client)
        client.set_missing_host_key_policy(_AcceptNewPolicy())
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
            default="no",
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
        self._do_connect(force_new_passphrase=True)

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
