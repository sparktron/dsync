# 🔄 dsync

> SSH deploy tool for personal sites on cPanel shared hosting.  Basically a purpose-built wrapper around rsync (and paramiko/SFTP) for a specific personal deployment workflow.

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![rsync](https://img.shields.io/badge/powered%20by-rsync-orange)](https://rsync.samba.org/)
[![CI](https://github.com/sparktron/dsync/actions/workflows/ci.yml/badge.svg)](https://github.com/sparktron/dsync/actions/workflows/ci.yml)

Edit files locally, sync them to cPanel shared hosting over SSH/rsync.
No build step. No framework. Just files.

---

## 📋 Table of Contents

- [Requirements](#-requirements)
- [Install](#-install)
- [First-run setup](#-first-run-setup)
- [Commands](#-commands)
  - [`dsync pull`](#dsync-pull)
  - [`dsync push`](#dsync-push-path)
  - [`dsync watch`](#dsync-watch)
  - [`dsync status`](#dsync-status)
  - [`dsync backup`](#dsync-backup)
  - [`dsync open`](#dsync-open-path)
  - [`dsync log`](#dsync-log)
  - [`dsync profiles`](#dsync-profiles)
- [Config reference](#-config-reference)
- [Hooks](#-hooks)
- [Multiple profiles](#-multiple-profiles)
- [How SSH auth works](#-how-ssh-auth-works)
- [State file](#-state-file)
- [Development](#-development)

---

## ✅ Requirements

- [Python 3.9+](https://www.python.org/downloads/)
- [`rsync`](https://rsync.samba.org/) installed locally
- `ssh-agent` or `ssh-add` available (used to load your passphrase-protected key for rsync)

---

## 📦 Install

```bash
pip install -e /path/to/dsync
```

Or, from within the repo:

```bash
pip install -e .
```

After install, `dsync` is available as a CLI command.

---

## 🧙 First-run setup

Running any `dsync` command when no config exists launches an interactive wizard:

```
dsync pull
```

```
dsync — first-run setup

No config found at ~/.dsync/config.json. Let's set one up.

SSH host [your-host.example.com]:
SSH port [22]:
SSH user [youruser]:
Path to SSH private key [~/.ssh/id_rsa]:
Local project root [~/projects/mysite/]:
Remote web root [/home/youruser/public_html/]:
Live site URL [https://example.com]:
Remote backup directory [~/backups/dsync]:

✓ Config saved to /home/you/.dsync/config.json
```

Config is stored at `~/.dsync/config.json`. Edit it directly to change settings.

---

## 💻 Commands

### `dsync pull`

Pull the full site from the server to local.

```
dsync pull
ℹ Connecting to your-host.example.com:22...
✓ Connected
ℹ Pulling site...
  ↓ index.html
  ↓ css/custom.css
✓ 2 updated, 0 deleted
```

Excluded by default: `.git/`, `images/`, `lscache/`, `*.gz`, `*.zip`.

---

### `dsync push [path]`

Push local changes to the server.

**No argument** — diff local vs remote, confirm, then sync:

```
dsync push
ℹ Computing changes...

Files to push (2):
  index.html
  css/custom.css

Push 2 file(s)? [Y/n]:
ℹ Creating server backup of changed files...
✓ Backup at /home/youruser/backups/dsync/2024-01-15_14-32-01
  ✓ index.html → https://example.com/
  ✓ css/custom.css → https://example.com/css/custom.css

✓ 2 file(s) pushed.
```

**With a path** — push only that file or directory:

```
dsync push css/custom.css
ℹ Backing up remote file...
✓ Done — https://example.com/css/custom.css
```

**`--diff`** — show a colorized diff (remote → local) before the confirm prompt:

```
dsync push --diff
ℹ Computing changes...

Files to push (1):
  css/custom.css

Diffs (remote → local):

css/custom.css
--- remote/css/custom.css
+++ local/css/custom.css
@@ -12,6 +12,7 @@
   color: #333;
+  font-weight: bold;

Push 1 file(s)? [Y/n]:
```

Binary files are skipped automatically. New files show their full content as an addition.

---

### `dsync watch`

Watch the local directory and auto-push on save.

```
dsync watch
Watching ~/projects/mysite/ — Ctrl+C to stop
[14:32:01] ✓ pushed index.html → https://example.com/
[14:32:44] ✓ pushed aboutme/index.html → https://example.com/aboutme/
^C
Stopped.
```

Saves are debounced 800 ms to avoid duplicate uploads on rapid writes. If an upload fails, dsync retries automatically up to **3 times** with exponential backoff (2 s, then 4 s). If a file is saved again while a retry is pending, the retry is cancelled and a fresh upload starts immediately.

---

### `dsync status`

Show which files are out of sync between local and remote.

```
dsync status
ℹ Comparing local vs remote (this may take a moment)...

         Sync Status
┌────────────────┬────────────────────────────────┐
│ Status         │ File                           │
├────────────────┼────────────────────────────────┤
│ local newer    │ css/custom.css                 │
│ remote only    │ .htaccess.bak                  │
└────────────────┴────────────────────────────────┘
```

Groups: **local newer**, **remote newer**, **local only**, **remote only**.

**`--local`** — instant offline check against the last sync state (no SSH connection):

```
dsync status --local
   Local Status (vs last sync)
┌──────────────┬──────────────────────┐
│ Status       │ File                 │
├──────────────┼──────────────────────┤
│ modified     │ css/custom.css       │
│ new          │ blog/post-3.html     │
└──────────────┴──────────────────────┘

Local check only — run dsync status to compare with remote.
```

Useful on large sites where the full rsync dry-run is slow.

---

### `dsync backup`

Trigger a full server-side backup:

```
dsync backup
ℹ Connecting to your-host.example.com:22...
✓ Connected
ℹ Archiving remote site (this may take a moment)...
✓ Backup created: /home/youruser/backups/dsync/2024-01-15_14-33-00.tar.gz
```

---

### `dsync open [path]`

Open the live URL for a local file in the default browser:

```
dsync open aboutme/index.html
ℹ Opening https://example.com/aboutme/
```

With no argument, opens the site root.

---

### `dsync log`

Show recent sync operation history:

```
dsync log
                          Sync Log
┌─────────────────────┬────────────┬───────┬──────────┬────────┬─────────┐
│ Time                │ Action     │ Files │ Duration │ Status │ Profile │
├─────────────────────┼────────────┼───────┼──────────┼────────┼─────────┤
│ 2024-01-15T14:33:00 │ backup     │     0 │ 8.2s     │ ok     │ default │
│ 2024-01-15T14:32:05 │ push       │     2 │ 1.4s     │ ok     │ default │
│ 2024-01-15T14:31:50 │ pull       │     0 │ 2.1s     │ ok     │ default │
└─────────────────────┴────────────┴───────┴──────────┴────────┴─────────┘
```

Use `-n` to control how many entries to show (default: 20):

```
dsync log -n 50
```

The log is stored at `~/.dsync/sync.log` (capped at 500 entries).

---

### `dsync profiles`

List all available configuration profiles:

```
dsync profiles
Available profiles:
  default
  staging

Use dsync --profile NAME <command> to target a specific profile.
```

---

## ⚙️ Config reference

`~/.dsync/config.json`:

```json
{
  "host": "your-host.example.com",
  "port": 22,
  "user": "youruser",
  "key_path": "~/.ssh/id_rsa",
  "local_root": "~/projects/mysite/",
  "remote_root": "/home/youruser/public_html/",
  "site_url": "https://example.com",
  "backup_dir": "~/backups/dsync",
  "ignore_patterns": [
    ".git/",
    "images/",
    "lscache/",
    "*.gz",
    "*.zip",
    ".DS_Store",
    "*~",
    "*.swp",
    "__pycache__/",
    ".dsync_state"
  ],
  "hooks": {
    "pre_push": "",
    "post_push": "",
    "pre_pull": "",
    "post_pull": ""
  }
}
```

---

## 🪝 Hooks

Hooks let you run shell commands automatically before or after a push or pull. They run in your `local_root` directory.

| Hook | When it runs |
|------|-------------|
| `pre_push` | Before uploading files to the server |
| `post_push` | After a successful upload |
| `pre_pull` | Before downloading files from the server |
| `post_pull` | After a successful download |

**A failing pre-hook aborts the operation.** Post-hooks only run if files were actually transferred.

Example — compile Sass before pushing, then bust the CDN cache after:

```json
{
  "hooks": {
    "pre_push": "sass src/styles.scss css/styles.css",
    "post_push": "curl -s -X POST https://example.com/cdn/purge"
  }
}
```

---

## 👤 Multiple profiles

Manage multiple sites from one installation using named profiles. Each profile has its own config and sync state.

**Create a new profile** — run any command with `--profile NAME` and the wizard launches automatically if no config exists for that profile yet:

```
dsync --profile staging pull
```

Named profile configs are stored at `~/.dsync/profiles/<name>.json`. The default profile continues to use `~/.dsync/config.json`.

**Use a profile for any command:**

```bash
dsync --profile staging push
dsync --profile staging status --local
dsync -p staging watch
```

**List all profiles:**

```
dsync profiles
```

---

## 🔐 How SSH auth works

`dsync` uses **[paramiko](https://www.paramiko.org/)** for direct SSH/SFTP operations (backups, single-file
uploads in watch mode) and **system rsync** for bulk transfers.

On first use you'll be prompted for your key passphrase. It is:

- **Cached in memory** for the duration of the process — never written to disk.
- Used to authenticate the paramiko connection directly.
- Used to load your key into a temporary `ssh-agent` session so rsync can
  authenticate without re-prompting.

The temporary agent is torn down when the process exits.

---

## 🗂️ State file

After each push/pull, dsync writes a state manifest — a record of every synced file's mtime, MD5 checksum, and sync timestamp. This enables fast local diffing without rescanning the server.

| Profile | State file |
|---------|-----------|
| default | `~/.dsync/state.json` |
| `staging` | `~/.dsync/state_staging.json` |

---

## 🛠️ Development

Install the package with dev dependencies:

```bash
pip install -e ".[dev]"
```

Run the linter:

```bash
ruff check dsync/
ruff format --check dsync/
```

CI runs automatically on every push and PR via [GitHub Actions](.github/workflows/ci.yml),
checking lint and verifying the package installs cleanly across Python 3.9–3.12.
