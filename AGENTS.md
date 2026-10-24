# AGENTS.md

Guidance for coding agents working in this repository. Single source of truth;
`CLAUDE.md` imports it.

## What this is

SSH deploy tool for personal sites on cPanel shared hosting — a purpose-built
wrapper around rsync (with paramiko/SFTP as the fallback transport). Python
package, Click CLI, installed as the `dsync` command.

Primary consumer is `dylansparks.com`, which has no build step: edit files
locally, sync them up.

## Layout

| File | Role |
|---|---|
| `dsync/cli.py` | Click entry point — `dsync = "dsync.cli:cli"` |
| `dsync/sync.py` | rsync invocation and transfer logic |
| `dsync/ssh.py` | paramiko/SFTP transport, ssh-agent setup for rsync |
| `dsync/config.py` | Config loading |
| `dsync/state.py` | Local sync state tracking |
| `dsync/watcher.py` | watchdog-based file watching |
| `dsync/log.py` | Rich-backed logging |

## Commands

```bash
pip install -e '.[dev]'   # editable install + ruff + pytest
dsync --help              # verify entry point resolves
ruff check dsync/
ruff format --check dsync/
pytest
```

## CI

`.github/workflows/ci.yml` runs two jobs:

- **Lint** on Python 3.12 — `ruff check dsync/` and `ruff format --check dsync/`.
  Format check is enforced, so run `ruff format dsync/` before pushing.
- **Build & Test** across Python **3.9, 3.10, 3.11, 3.12** — `pip install -e .`,
  `dsync --help`, then `pytest`.

`requires-python = ">=3.9"`. Do not use 3.10+ syntax (`match`, `X | Y` in
annotations at runtime, `tomllib`) — the 3.9 matrix leg will fail.

## Deploy safety

This tool writes to a live public website over SSH. Treat every change to
`sync.py` as production-affecting.

- rsync `--delete` semantics are destructive on the remote. Never widen the
  delete scope or change exclude patterns without saying so explicitly.
- Never log credentials, key material, or full SSH URIs with embedded auth.
- Dry-run paths must stay genuinely side-effect-free. If you add a code path
  that writes, confirm it is gated behind the non-dry-run branch.

- rsync failures raise `SyncError`; never turn a failed run into an empty
  result, which callers read as "everything is in sync".
- Host keys are verified against `~/.ssh/known_hosts` (`accept-new`) for both
  rsync and paramiko. Do not reintroduce `StrictHostKeyChecking=no`.
- `prune-backups` deletes only timestamp-named entries directly under
  `backup_dir`; keep that filter if you touch it.

## Tests

`tests/test_sync.py` covers `sync.py` with rsync and SSH stubbed out;
`tests/test_ssh.py`, `tests/test_config.py` and `tests/test_misc.py` cover the
rest. When touching `sync.py`, add tests rather than relying on manual
verification against the live host.
