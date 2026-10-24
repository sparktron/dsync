# Status

Updated: 2026-10-24

## Current state

PR #5 reconciles the older review branch with master at 486992f. Master's tree was the baseline: host-key verification, backup retention, timestamp handling, watcher rename events, atomic state saves, lint configuration, and existing regression tests are preserved.

Remaining changes are bounded SSH helper calls, one-shot askpass, prompt avoidance for loaded agent keys and unencrypted keys, cleanup on failed key loading, stricter CLI path confinement, and clean reporting/logging of SSH connection errors.

## Decisions

Keep master's optional passphrase storage and its owner-only, atomic configuration writes. The old branch's automatic removal of stored credentials is intentionally excluded because it changes an existing feature. Existing authentication and configuration tests remain intact.

Superseded audit documentation and tests from the old branch are replaced by master's implementation and regression tests plus tests/test_reconciliation.py.

## Validation

- Full local suite: 114 passed on Python 3.10.
- `ruff check dsync/`: passed.
- `ruff format --check dsync/`: passed.
- `git diff --check`: passed.
- Tests include executing the real askpass helper to verify its one-shot behavior; SSH/SFTP connections are mocked.
- Not run: live deployment, remote server access, local Python 3.9/3.11/3.12 suites. The existing CI matrix covers those interpreters after push.

## Next

Review PR #5 and its CI results. No live site was modified. PR has not been merged.
