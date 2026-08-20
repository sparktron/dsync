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
| `dsync/ssh.py` | paramiko/SFTP transport |
| `dsync/config.py` | Config loading |
| `dsync/state.py` | Local sync state tracking |
| `dsync/watcher.py` | watchdog-based file watching |
| `dsync/log.py` | Rich-backed logging |

## Commands

```bash
pip install -e ".[dev]"   # editable install, with ruff + pytest
dsync --help              # verify entry point resolves
ruff check dsync/ tests/
ruff format --check dsync/ tests/
pytest                    # tests/test_ssh.py, tests/test_sync.py
```

## CI

`.github/workflows/ci.yml` runs two jobs, on every branch push and on PRs to
master:

- **Lint** on Python 3.12 — `ruff check dsync/ tests/` and
  `ruff format --check dsync/ tests/`. Format check is enforced, so run
  `ruff format dsync/ tests/` before pushing. Tests are linted too.
- **Test** across Python **3.9, 3.10, 3.11, 3.12** — `pip install -e ".[dev]"`,
  then `dsync --help` and `pytest -q`.

Both jobs install via `.[dev]`, so the **ruff version is pinned in
`pyproject.toml`** and a new ruff release cannot turn the build red on its own.
Bump it deliberately. The lint rule set is likewise explicit — see
`[tool.ruff.lint]` — rather than inherited from ruff's shifting defaults.

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

## Testing

`tests/test_ssh.py` covers connection and passphrase error handling, including
degrading gracefully when ssh-agent is not installed.
`tests/test_sync.py` covers the rsync layer at the argv level — it asserts that
`--delete` never ships without `--dry-run`, that the transferring entry points
(`rsync_pull`, `rsync_push_all`, `rsync_push_directory`) never pass `--delete`
at all, and that src/dst ordering and excludes are wired correctly.

Those argv assertions are the guard on the most dangerous code in the repo.
If you change `_run_rsync` or any of its callers, expect them to fire — and
treat a failure as a real finding, not a test to update. Add cases rather than
relaxing them.

`tests/test_cli.py` covers how failures are reported: a failed comparison must
never print "everything is in sync", and the operation log must record what
actually happened rather than inferring success from a file count.

Every rsync result must go through `_check_rsync`, which raises `RsyncError`
on a hard failure and tolerates the partial-transfer codes (23, 24). Never
substitute empty output for a failed run — that is what made a broken `status`
indistinguishable from a clean tree.

Still uncovered: `config.py`, `state.py`, `log.py`, `watcher.py`, and the
non-rsync half of `sync.py` (the SFTP and backup helpers).
