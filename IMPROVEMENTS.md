# SSH Error Handling Improvements

## What Was Wrong

The original SSH connection code had poor error handling when passphrases were incorrect:

1. **Generic exception catching** - All errors were caught with `except Exception`, making it impossible to distinguish between wrong passphrases, network issues, and other problems
2. **No passphrase retry logic** - When a wrong passphrase was cached, retrying didn't help because the cache wasn't cleared
3. **Cryptic error messages** - Users saw raw paramiko exceptions instead of helpful guidance
4. **No recovery path** - When authentication failed, users couldn't retry with a different passphrase without restarting the tool
5. **Hidden key loading errors** - Errors in key decryption (like "password and salt must not be empty") were unclear to users

## Changes Made

### 1. Improved Error Handling in `ssh.py`

**Specific exception handling:**
- `AuthenticationException` → clear passphrase and offer to retry with a new one
- `ValueError`/`SSHException` (key errors) → detect passphrase issues and offer retry
- Generic errors (network, etc.) → automatic retry once with delay

**Passphrase management:**
- Added `force_new` parameter to `get_passphrase()` to clear cache when retrying
- After auth failure, the cached passphrase is cleared, allowing fresh input
- New method `_do_connect_with_new_passphrase()` handles retry flows

**Better UX:**
- `_handle_auth_failure()` - Detects auth issues and prompts for retry
- `_handle_key_error()` - Detects passphrase/key problems specifically
- `_handle_generic_error()` - Handles network and other connection issues
- All handlers provide clear messages about what went wrong

### 2. Comprehensive Test Suite (`tests/test_ssh.py`)

**18 unit tests covering:**

**Passphrase Caching (3 tests)**
- ✓ Passphrase is cached after first prompt
- ✓ Empty input converts to None (no passphrase)
- ✓ `force_new=True` clears cache for retry

**Successful Connections (3 tests)**
- ✓ Successful SSH connection
- ✓ Connection with no passphrase (None)
- ✓ Uses stored passphrase from config

**Authentication Failures (2 tests)**
- ✓ Auth failure prompts for retry with new passphrase
- ✓ Auth failure respects user's choice not to retry

**Key Errors (2 tests)**
- ✓ Wrong passphrase for encrypted key
- ✓ Key file not found error

**Network Errors (2 tests)**
- ✓ Network errors trigger automatic retry
- ✓ Network errors don't retry when `retry=False`

**Passphrase Saving (3 tests)**
- ✓ Offers to save passphrase after successful connection
- ✓ Skips save if user declines
- ✓ Never offers to save if already saved in config

**Context Manager (2 tests)**
- ✓ Properly closes connection
- ✓ Handles connection failures gracefully

**Error Messages (1 test)**
- ✓ Friendly error messages for wrong passphrase

## Example Error Flow Before and After

### Before (Confusing)
```
✗ Connection failed: password and salt must not be empty. Retrying in 3 s...
[full paramiko traceback]
ValueError: password and salt must not be empty
```

### After (Helpful)
```
SSH key passphrase: [user enters wrong pass]
✗ Authentication failed: password and salt must not be empty
✗ Wrong passphrase or encrypted key issue
✓ The passphrase appears to be incorrect.
✓ Retry with a different passphrase? [yes/no]: yes

SSH key passphrase (press Enter if no passphrase): [user enters correct pass]
✓ Connected
✓ Save SSH passphrase to config for future use? (yes/no): yes
✓ Passphrase saved to config
```

## Benefits

1. **User-friendly** - Clear messages about what went wrong
2. **Recoverable** - Users can retry with a different passphrase without restarting
3. **Smart retries** - Different retry strategies for different error types
4. **Well-tested** - 18 tests ensure error handling works correctly
5. **Configuration support** - Passphrases can be saved to avoid repeated prompting
6. **No more cryptic errors** - All exceptions are caught and explained

## Testing

Run the tests with:
```bash
python -m pytest tests/test_ssh.py -v
```

All 18 tests should pass, covering success cases, failure cases, retries, passphrase caching, and user prompts.
