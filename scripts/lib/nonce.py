"""Per-Principal nonce floor — atomic allocation across processes.

Every signature must set the `nonce` field of the EIP-712 envelope to an
integer strictly greater than the value the server has stored. We maintain a
local floor in `~/.govnet/nonces/<principal>.json` to ensure two concurrent
skill invocations don't reuse the same nonce:

- Create / increment: a sibling lock file (`<principal>.lock`) plus
  `fcntl.flock` provides cross-process mutual exclusion; the read-modify-write
  happens inside the lock, then `os.replace` atomically overwrites the data
  file. Two concurrent skill processes **will not** receive the same nonce —
  the later one blocks until the earlier one finishes.
- `fsync` ensures we never leave a half-written state on crash.
- Server drift: on `AUTH_NONCE_TOO_LOW` / `NONCE_TOO_LOW`, the caller should
  re-fetch `/v1/auth/info` for the server-stored value, then call
  `bump_to(value)` to push the local floor up before retrying — `bump_to`
  also takes the flock path.
- Windows: `fcntl` is not available — the placeholder implementation falls
  back to lock-free, leaving the old "two processes share a nonce" risk in
  place. Production deployments should run on Linux/macOS.

`GOVNET_NONCE_DIR` overrides the default `~/.govnet/nonces/`.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl  # POSIX-only — not available on Windows
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover
    _HAS_FCNTL = False


def _nonce_dir() -> Path:
    base = os.environ.get("GOVNET_NONCE_DIR")
    if base:
        path = Path(base).expanduser()
    else:
        path = Path.home() / ".govnet" / "nonces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _nonce_path(principal: str) -> Path:
    # Always lowercase — different case forms of the same address should share a single nonce floor.
    return _nonce_dir() / f"{principal.lower()}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, payload: dict) -> None:
    """Write via `os.replace` — rename within the same mount point is atomic.

    Any failing path (write/fsync/replace failure) cleans up the temp file,
    preventing orphan tmp accumulation when the disk is full or the nonce
    directory is mounted incorrectly.
    """
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{time.time_ns()}")
    data = json.dumps(payload).encode("utf-8")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def _principal_lock(principal: str) -> Iterator[None]:
    """Cross-process mutex — acquires LOCK_EX on entry, releases on exit.

    Uses a sibling `<principal>.lock` file (rather than the data file itself)
    to avoid losing the lock when `os.replace` swaps inodes. On Windows
    without fcntl, this degrades to lock-free.
    """
    if not _HAS_FCNTL:
        yield
        return
    lock_path = _nonce_path(principal).with_suffix(".lock")
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_floor(principal: str) -> int:
    """Return the locally stored nonce floor. Returns 0 if the file is missing (so first signature uses 1).

    Does not hold the lock — callers must ensure no other process writes
    between read and use. `next_nonce` / `bump_to` call this while holding
    the lock.
    """
    path = _nonce_path(principal)
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            return int(json.load(f)["nonce"])
    except (json.JSONDecodeError, KeyError, ValueError):
        # File is corrupted — wiping and starting over is safer than silently ignoring.
        return 0


def next_nonce(principal: str) -> int:
    """Read current floor → +1 → persist → return the new value.

    `_principal_lock` ensures two concurrent skill processes won't read the
    same stale value. Two concurrent calls are now equivalent to sequential:
    A gets 1, B gets 2, never duplicating. The lock scope does not span the
    network request, so a stale lock (from a crashed process) is released
    automatically when its fd closes.
    """
    with _principal_lock(principal):
        new = read_floor(principal) + 1
        _atomic_write(
            _nonce_path(principal),
            {"nonce": new, "updated_at": _now_iso()},
        )
    return new


def bump_to(principal: str, server_stored: int) -> int:
    """Raise the local floor to `server_stored` (if higher), then +1 and return.

    Used on the `AUTH_NONCE_TOO_LOW` retry path — the server's
    `/v1/auth/info` returns the currently stored nonce; this function
    atomically pushes the local floor up and allocates the next value.
    """
    with _principal_lock(principal):
        current = read_floor(principal)
        floor = max(current, int(server_stored)) + 1
        _atomic_write(
            _nonce_path(principal),
            {"nonce": floor, "updated_at": _now_iso()},
        )
    return floor


def reset(principal: str, value: Optional[int] = None) -> None:
    """Reset (for tests or explicit syncs). `value=None` deletes the file."""
    with _principal_lock(principal):
        path = _nonce_path(principal)
        if value is None:
            if path.exists():
                path.unlink()
            return
        _atomic_write(path, {"nonce": int(value), "updated_at": _now_iso()})
