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
| `dsync/ssh.py` | paramiko/SFTP transport; the only module with tests |
| `dsync/config.py` | Config loading |
| `dsync/state.py` | Local sync state tracking |
| `dsync/watcher.py` | watchdog-based file watching |
| `dsync/log.py` | Rich-backed logging |

## Commands

```bash
pip install -e .          # editable install
dsync --help              # verify entry point resolves
ruff check dsync/
ruff format --check dsync/
pytest                    # only tests/test_ssh.py exists today
```

## CI

`.github/workflows/ci.yml` runs two jobs:

- **Lint** on Python 3.12 — `ruff check dsync/` and `ruff format --check dsync/`.
  Format check is enforced, so run `ruff format dsync/` before pushing.
- **Build & Install** across Python **3.9, 3.10, 3.11, 3.12** — `pip install -e .`
  then `dsync --help`.

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

## Testing gap

Only `ssh.py` has coverage. `sync.py` — the module that can delete remote
files — has none. When touching `sync.py`, add tests rather than relying on
manual verification against the live host.
