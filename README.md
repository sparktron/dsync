# dsync

SSH deploy tool for [dylansparks.com](https://dylansparks.com).

Edit files locally, sync them to cPanel shared hosting over SSH/rsync.
No build step. No framework. Just files.

---

## Requirements

- Python 3.9+
- `rsync` installed locally
- `ssh-agent` or `ssh-add` available (used to load your passphrase-protected key for rsync)

---

## Install

```bash
pip install -e /path/to/dsync
```

Or, from within the repo:

```bash
pip install -e .
```

After install, `dsync` is available as a CLI command.

---

## First-run setup

Running any `dsync` command when no config exists launches an interactive wizard:

```
dsync pull
```

```
dsync — first-run setup

No config found at ~/.dsync/config.json. Let's set one up.

SSH host [dubstep.cleannameservers.com]:
SSH port [50288]:
SSH user [dylanspa]:
Path to SSH private key [~/Documents/dylansparks.com/id_rsa]:
Local project root [~/Documents/dylansparks.com/public_html/]:
Remote web root [/home/dylanspa/public_html/]:
Live site URL [https://dylansparks.com]:
Remote backup directory [~/backups/dsync]:

✓ Config saved to /home/you/.dsync/config.json
```

Config is stored at `~/.dsync/config.json`. Edit it directly to change settings.

---

## Commands

### `dsync pull`

Pull the full site from the server to local.

```
dsync pull
ℹ Connecting to dubstep.cleannameservers.com:50288...
✓ Connected
ℹ Pulling site...
  ↓ index.html
  ↓ css/custom.260105063601.css
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
  css/custom.260105063601.css

Push 2 file(s)? [Y/n]:
ℹ Creating server backup of changed files...
✓ Backup at /home/dylanspa/backups/dsync/2024-01-15_14-32-01
  ✓ index.html → https://dylansparks.com/
  ✓ css/custom.260105063601.css → https://dylansparks.com/css/custom.260105063601.css

✓ 2 file(s) pushed.
```

**With a path** — push only that file or directory:

```
dsync push css/custom.260105063601.css
ℹ Backing up remote file...
✓ Done — https://dylansparks.com/css/custom.260105063601.css
```

**`--diff`** — show a colorized diff of every changed file (remote → local) before the confirm prompt:

```
dsync push --diff
```

---

### `dsync watch`

Watch the local directory and auto-push on save.

```
dsync watch
Watching ~/Documents/dylansparks.com/public_html/ — Ctrl+C to stop
[14:32:01] ✓ pushed index.html → https://dylansparks.com/
[14:32:44] ✓ pushed aboutme/index.html → https://dylansparks.com/aboutme/
^C
Stopped.
```

Saves are debounced 800 ms to avoid duplicate uploads on rapid writes.
Failed uploads retry automatically up to 3 times with exponential backoff (2 s, 4 s).

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
│ local newer    │ css/custom.260105063601.css     │
│ remote only    │ .htaccess.bak                  │
└────────────────┴────────────────────────────────┘
```

Groups: **local newer**, **remote newer**, **local only**, **remote only**.

**`--local`** — instant offline check against the last sync state (no network):

```
dsync status --local
```

Reports **modified**, **new**, and **deleted** files relative to the last push/pull.

---

### `dsync backup`

Trigger a full server-side backup:

```
dsync backup
ℹ Connecting to dubstep.cleannameservers.com:50288...
✓ Connected
ℹ Archiving remote site (this may take a moment)...
✓ Backup created: /home/dylanspa/backups/dsync/2024-01-15_14-33-00.tar.gz
```

---

### `dsync open [path]`

Open the live URL for a local file in the default browser:

```
dsync open aboutme/index.html
ℹ Opening https://dylansparks.com/aboutme/
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
│ 2024-01-15T14:32:01 │ push       │     2 │ 1.2s     │ ok     │ default │
│ 2024-01-15T14:30:00 │ pull       │     0 │ 800ms    │ ok     │ default │
└─────────────────────┴────────────┴───────┴──────────┴────────┴─────────┘
```

Use `-n` to control how many entries to show (default: 20). Log is stored at
`~/.dsync/sync.log`.

---

### `dsync profiles`

List available configuration profiles:

```
dsync profiles
Available profiles:
  default
  staging
```

Use `dsync --profile NAME <command>` to target a specific profile.

---

## Multi-profile support

Run dsync against multiple sites from one installation. Named profiles store
config at `~/.dsync/profiles/<name>.json` with a separate state file per
profile. The default profile continues to use `~/.dsync/config.json`.

```bash
# First use of a new profile triggers the wizard
dsync --profile staging pull

# All commands support --profile / -p
dsync -p staging push
dsync -p staging status --local
```

---

## Hooks

Run shell commands automatically before or after push/pull. Add a `"hooks"`
section to your config:

```json
{
  "hooks": {
    "pre_push":  "npm run build",
    "post_push": "curl -X POST https://dylansparks.com/purge-cache",
    "pre_pull":  "",
    "post_pull": ""
  }
}
```

- A failing `pre_push` / `pre_pull` (non-zero exit) **aborts** the operation.
- `post_push` / `post_pull` only run after a successful sync.
- Hooks run in your `local_root` directory.
- Omit a key (or leave it blank) to skip that hook.

---

## Config reference

`~/.dsync/config.json`:

```json
{
  "host": "dubstep.cleannameservers.com",
  "port": 50288,
  "user": "dylanspa",
  "key_path": "~/Documents/dylansparks.com/id_rsa",
  "local_root": "~/Documents/dylansparks.com/public_html/",
  "remote_root": "/home/dylanspa/public_html/",
  "site_url": "https://dylansparks.com",
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

## How SSH auth works

`dsync` uses **paramiko** for direct SSH/SFTP operations (backups, single-file
uploads in watch mode) and **system rsync** for bulk transfers.

On first use you'll be prompted for your key passphrase. It is:

- **Cached in memory** for the duration of the process — never written to disk.
- Used to authenticate the paramiko connection directly.
- Used to load your key into a temporary `ssh-agent` session so rsync can
  authenticate without re-prompting.

The temporary agent is torn down when the process exits.

---

## State file

After each push/pull, dsync writes a state manifest (mtime, MD5 checksum,
and sync timestamp for every tracked file). This enables fast local diffing
without rescanning the server.

| Profile | State file |
|---------|-----------|
| default | `~/.dsync/state.json` |
| named   | `~/.dsync/state_<name>.json` |
