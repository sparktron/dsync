# dsync — Full Code Review

Reviewed at `cfdf24f` on 2026-08-20. All eight modules in `dsync/`, plus `tests/`,
`pyproject.toml` and `.github/workflows/ci.yml`, read in full. Every finding below was
reproduced in a local checkout rather than inferred from reading.

**Tally:** 5 critical · 4 high · 6 medium · 7 low

> **Status:** CI-1, TEST-1, BUG-1, BUG-5, BUG-2 and BUG-9 are fixed on this
> branch, along with the `rsync_pull` dead-deletion reporting listed under Low.
> 15 findings remain open. Next is the credential group (SEC-2, SEC-3, BUG-4),
> which needs a decision on whether to keep passphrase storage at all.

The tool's shape is sound — clean module split, idiomatic rsync wrapping, and the recent
SSH error-handling work is good. Problems cluster in three places: nobody is watching CI,
credential handling is weaker than `AGENTS.md` claims, and the failure paths lie to the
user. Nothing here needs an architecture change.

## How this was verified

```
# CI's exact lint steps, latest ruff (0.16.3) — both fail
ruff check dsync/           -> 40 errors, exit 1
ruff format --check dsync/  -> 1 file would be reformatted, exit 1

# The test suite CI never runs
pip install pytest && pytest -q   -> 18 passed in 2.33s

# GitHub Actions history for ci.yml on master
cfdf24f  2026-07-27  failure
870f7d3  2026-06-23  failure
71960b6  2026-04-04  failure
edf7e15  2026-04-04  failure
c519f7e  2026-03-28  success   <- last green build
```

Path-escape, ignore-pattern, itemize-parsing and config-permission behaviour were each
exercised with a driver script against the real functions.

---

## Critical

### CI-1 — CI has been red on `master` for four months — FIXED
`.github/workflows/ci.yml`, `dsync/ssh.py:279`

Last green build was `c519f7e` (2026-03-28). Every push since — four runs, including HEAD —
has failed the Lint job. Immediate cause: a missing trailing comma in
`_offer_to_save_passphrase` that `ruff format --check` rejects. Deeper cause: `ci.yml` does
`pip install ruff` unpinned, so any ruff release can turn the build red with no code change.
ruff 0.16 widened its default rule set and now reports 40 findings older versions did not.

**Fix:** run `ruff format dsync/`, pin the ruff version, and add an explicit
`[tool.ruff.lint] select` block to `pyproject.toml` so the rule set is a repo decision.

### SEC-1 — Host key verification is disabled on both transports
`dsync/sync.py:26-34`, `dsync/ssh.py:254`, `dsync/ssh.py:357`

rsync passes `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`; paramiko uses
`AutoAddPolicy()` with no `load_system_host_keys()` anywhere. dsync will authenticate to any
host answering on that address, every time, with no trust-on-first-use record.

An attacker between you and the shared host receives your site's files and can return content
that `pull` writes into your working copy. `UserKnownHostsFile=/dev/null` also guarantees a
genuine host key change is never surfaced.

**Fix:** drop both `-o` flags and use `~/.ssh/known_hosts`. In paramiko call
`load_system_host_keys()` with `RejectPolicy`, plus a first-run prompt showing the fingerprint.

### SEC-2 — SSH key passphrase written to a world-readable file in cleartext
`dsync/config.py:89-94`, `dsync/ssh.py:266-286`

`_offer_to_save_passphrase` persists the passphrase and `save_config` writes it as plain JSON
under the process umask.

Reproduced: file mode `0o644`, directory mode `0o755`, containing
`"passphrase": "super-secret-passphrase"`. Any local account can read it. `AGENTS.md` says
"never log credentials, key material" — this is the same exposure with a longer lifetime.

**Fix (minimum):** create `~/.dsync` mode `0700`, write config via `os.open(..., 0o600)`, and
repair permissions on load. **Better:** stop storing it — hand it to the ssh-agent once per
session (machinery exists in `get_rsync_env`) or use the system keyring.

### BUG-1 — `dsync status` reports "Everything is in sync" when the comparison failed — FIXED
`dsync/sync.py:278-283`

```python
push_transfers, push_deletions = _parse_itemize(
    push_result.stdout if push_result.returncode == 0 else ""
)
```

A non-zero exit substitutes empty output instead of raising. Both dry-runs failing yields four
empty lists, `total == 0`, and the green `✓ Everything is in sync.` at `cli.py:381` — no
stderr, no non-zero CLI exit. Network failure, auth failure, a wrong `remote_root`, and
rsync's routine partial-transfer codes 23/24 all land here.

For a tool whose job is reporting whether the live site matches your working copy, a false
"in sync" is indistinguishable from success.

**Fix:** raise on non-zero (allowing 23/24 explicitly if wanted), surface stderr, exit non-zero.

### BUG-2 — `dsync push ../file` writes outside the remote web root — FIXED
`dsync/cli.py:139-151`, `dsync/sync.py:224`

`_push_path` does `rel_path = path.lstrip("/")`, which strips leading slashes but not `..`.
When `abs_path.relative_to(config.local_root)` raises `ValueError` the code does `pass` and
keeps the original string, which flows into `remote_path = config.remote_root + rel_path`.

Reproduced:

```
input  ../secrets.env
remote /home/u/public_html/../secrets.env        -> /home/u/secrets.env
input  ../../../etc/passwd
remote /home/u/public_html/../../../etc/passwd   -> /etc/passwd
```

Self-inflicted rather than an external attack, but a live footgun: a mistyped relative path
uploads a file from outside the project to outside the web root with no confirmation, and
`_backup_remote_file` flattens the name so the backup gives no hint. `dsync open` shares the
same unguarded `rel_path`.

**Fix:** resolve against `local_root` and reject anything not under it — the `ValueError`
branch should be a hard error, not `pass`. Assert the same remote-side with `posixpath.normpath`.

---

## High

### SEC-3 — Every rsync run leaks an ssh-agent holding the decrypted key, forever
`dsync/ssh.py:89-138`

`get_rsync_env` spawns `ssh-agent -s` and adds the key. There is no `ssh-agent -k` anywhere and
no `atexit` handler, so the agent outlives the process. The cache is process-local, so every
`push`/`pull`/`status` starts another one. They accumulate across a working day, each holding
the decrypted private key with no lifetime bound.

**Fix:** `atexit` hook running `ssh-agent -k` for an agent dsync started (tracked via
`SSH_AGENT_PID`), plus `ssh-add -t <seconds>` so the key expires if teardown is missed. Never
kill an agent you did not start.

### BUG-3 — `dsync watch` can hang on an invisible prompt in a background thread
`dsync/cli.py:268-274`, `dsync/ssh.py:170`, `dsync/ssh.py:369-383`

The upload runs on a watchdog timer thread holding `_upload_lock`. If the transport dropped,
`ssh.sftp` -> `_ensure_connected` -> `connect()`, and a successful reconnect calls
`_offer_to_save_passphrase()`, a blocking `Prompt.ask`. A failed reconnect issues a blocking
`click.confirm`.

Both read stdin from a non-main thread while the main thread sits in `while True: sleep(0.5)`.
`_upload_lock` stays held and every later save queues behind it. The save offer fires on every
successful connect whenever a passphrase is cached but unsaved, so an overnight watch session
that drops once will wedge.

**Fix:** gate interactive prompting on `threading.current_thread() is threading.main_thread()`,
add a `connect(interactive=False)` path reporting failure by return value, and offer to save the
passphrase only at initial connect.

### BUG-4 — `ssh-add`'s exit status is discarded, so a bad passphrase surfaces as a hang
`dsync/ssh.py:117-137`

Both `ssh-add` calls use `capture_output=True` and never check `returncode`; `_agent_env` is
cached regardless. With a wrong passphrase the agent is empty, and rsync runs against an agent
with no keys — its stdout/stderr are captured by `_run_rsync`, so ssh's prompt or error is
swallowed and the command appears frozen. The wrong passphrase stays cached for the session.

**Fix:** check the return code; on failure clear the passphrase cache, report which key failed,
and do not populate `_agent_env`.

### TEST-1 — CI runs no tests, and `pytest` isn't a declared dependency — FIXED
`.github/workflows/ci.yml`, `pyproject.toml:20-23`

`ci.yml` has two jobs: lint, and install-plus-`--help`. Neither invokes pytest.
`[project.optional-dependencies].dev` lists only `ruff`, so `pip install -e ".[dev]"` gives you
no test runner — even though `AGENTS.md` documents `pytest` as a standard command and
`IMPROVEMENTS.md` tells readers to run it. The 18 tests do pass (confirmed after installing
pytest manually); they are simply never exercised by automation.

This compounds the gap `AGENTS.md` already names: `sync.py`, the module that can delete remote
files, has zero coverage. Highest-value target is `_run_rsync` — `dry_run` is a bare boolean
with nothing asserting that `rsync_push_dry_run` and both `rsync_status` calls actually emit
`--dry-run` alongside `--delete`. A one-character edit could make the status command
destructive and nothing would catch it.

**Fix:** add `pytest` to `dev`, add a test job to the matrix, and write argv-assertion tests for
`_run_rsync` pinning the `--dry-run`/`--delete` pairing. `_parse_itemize` and `_matches_ignore`
are pure and cheap to cover.

---

## Medium

### BUG-5 — The operation log records the wrong outcome in both directions — FIXED
`dsync/cli.py:77-83`, `dsync/cli.py:126-134`

*Pull always logs success.* `rsync_pull` prints an error and returns `None` on rsync failure —
it signals nothing — so `cli.py` unconditionally writes `ok=True`. A failed pull is
indistinguishable from a clean one in `dsync log`.

*Push logs failure when there was nothing to do.* `deployed = len(transferred) > 0`, so an
already-in-sync tree and a user answering "no" at the prompt both record `ok=False` and render
as red `failed` rows.

**Fix:** have `rsync_pull` return a success flag, and distinguish "no work" from "failed".

### BUG-6 — Anchored ignore patterns are honoured by rsync but not by the state manifest
`dsync/state.py:139-150`

`_matches_ignore` strips a trailing slash and fnmatches each path component, but does nothing
with a *leading* slash. rsync treats `/images/` as anchored at the transfer root and excludes
it; `_matches_ignore` fnmatches the literal `/images` against components and never matches.

Reproduced with `patterns=['/images/']`: `images/a.png` -> `ignored=False` (rsync: excluded).

The manifest then tracks files rsync never transfers, so `dsync status --local` reports
permanent phantom drift. Default patterns are unanchored and unaffected, so this only bites
users who tighten their config — exactly when they are being careful.

**Fix:** treat a leading slash as an anchor — match anchored patterns against the full relative
path only, unanchored ones against components as today.

### BUG-7 — Remote backups accumulate without bound, one copy per save
`dsync/sync.py:354-365`, `dsync/sync.py:390-405`

Nothing ever prunes `backup_dir`. `_backup_remote_file` runs on every single-file push, so in
`watch` mode every save makes a timestamped copy. `create_full_backup` adds an unpruned tar.gz
per `dsync backup`. On cPanel shared hosting the account quota eventually notices, and it will
fail writes to the site itself, not just the backup directory.

**Fix:** add a retention policy (keep N per path, or prune beyond an age) run after each backup.
Consider skipping or debouncing the per-save backup in watch mode.

### PERF-1 — Backups and status cost one SSH round trip per file
`dsync/sync.py:368-387`, `dsync/sync.py:288-297`

`backup_remote_files` issues `mkdir -p` plus one `exec_command` per file — a hundred-file push
is 101 sequential round trips before the transfer starts. `rsync_status` calls `_remote_mtime`
per differing file, each a separate SFTP `stat`. In watch mode each save costs three
`exec_command` calls before the SFTP put.

**Fix:** batch the copies into one shell invocation; replace the per-file `stat` loop with one
remote `find -printf` or a batched listing.

### BUG-8 — URLs aren't percent-encoded
`dsync/sync.py:444-459`

`file_to_url` concatenates the raw relative path onto `site_url`. Verified: `my page.html` ->
`https://example.com/my page.html`; `a&b/index.html` -> `https://example.com/a&b/`. These go to
`webbrowser.open` and are printed as confirmation links after a push.

**Fix:** `urllib.parse.quote(url_path, safe="/")`. Related: the function builds URLs from
`os.sep`-joined paths on Windows, and `remote_root + rel_path` in `push_single_file` has the
same problem — force POSIX separators where a local path becomes a remote path or URL.

### BUG-9 — `local_root` is expanded but never resolved — FIXED
`dsync/config.py:40`, `dsync/cli.py:144-149`, `dsync/cli.py:495-500`

`Config.local_root` is `Path(...).expanduser()` with no `.resolve()`, but `_push_path` and
`open_url` compare it against `Path(path).expanduser().resolve()`. Any symlink in the configured
root — common on macOS — makes `relative_to` fail for a path that genuinely is inside the
project. The failure is silent (the `except ValueError: pass` from BUG-2) and surfaces as a
confusing "Path not found" for a file that plainly exists.

**Fix:** `.expanduser().resolve()` in `Config.__init__`.

---

## Low

| Item | Location | Note |
|---|---|---|
| Dead code paths | `state.py:103,107`; `sync.py:51,124-128` | `StateManager.get`/`.remove` never called; `_run_rsync`'s `capture` parameter never passed. `rsync_pull` prints "deleted locally" for deletions that can never occur — it runs without `--delete`. |
| Corrupt config crashes | `config.py:79-86` | `load_config` catches neither `JSONDecodeError` nor the `KeyError` from a missing key. `StateManager._load` already handles this correctly — mirror it. |
| Paramiko channels never closed | `ssh.py:178-193` | `run()` opens a session per call and closes nothing; `stdin` is unpacked and discarded (ruff `RUF059`). Matters most in long `watch` sessions. `.decode()` without `errors=` raises on non-UTF-8 output. |
| Passphrase briefly on disk | `ssh.py:102-107` | Askpass script is created `0600`, chmod'd `0700`, unlinked in `finally` — correct, but a SIGKILL in that window leaves the cleartext passphrase in `/tmp`. A FIFO avoids disk entirely. |
| `stat()` race in local status | `cli.py:416-418` | `exists()` then `stat()` on separate lines; a file removed in between raises out of the command. Use one `try/except OSError`. |
| `tests/` excluded from lint | `.github/workflows/ci.yml` | Both ruff steps target `dsync/` only. `tests/test_ssh.py` currently fails `ruff format --check` in several places. |
| `IMPROVEMENTS.md` is stale scratch | `IMPROVEMENTS.md` | A point-in-time changelog for one past PR, written as if current, telling readers to run a suite CI doesn't run and `pip install -e ".[dev]"` can't install. Fold anything durable into `README.md` and delete. |

---

## Recommended order

1. ~~**Get CI green and keep it that way.**~~ Done — formatter fix, ruff pinned in `[dev]`,
   explicit `select` list in `pyproject.toml`. *(CI-1)*
2. ~~**Add the test job and the `--dry-run` argv tests.**~~ Done — `tests/test_sync.py`,
   plus a Test job across the 3.9–3.12 matrix. *(TEST-1)*
3. ~~**Fix the two lies.**~~ Done — `RsyncError` raised at every rsync call site, connection
   failures reported cleanly instead of as tracebacks, and the log now distinguishes
   "nothing to do" from "failed". *(BUG-1, BUG-5)*
4. ~~**Confine paths to the roots.**~~ Done — `relative_to_root` and `remote_path_for`
   centralise the policy, `local_root` is resolved, and the target is validated before
   any connection is opened. *(BUG-2, BUG-9)*
5. **Close the credential gaps.** Config to `0600`, agent lifetime bounded, `ssh-add` failures
   surfaced. *(SEC-2, SEC-3, BUG-4)*
6. **Restore host key verification.** Last, because it needs a first-run trust flow designed
   rather than a flag flipped — and it will correctly refuse to connect until that exists. *(SEC-1)*
7. **Then the rest:** watch-mode thread safety, backup retention, batched round trips, URL
   encoding, and the low-severity sweep. *(BUG-3, BUG-7, PERF-1, BUG-8)*
