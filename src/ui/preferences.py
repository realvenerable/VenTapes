"""Small, thread-safe helpers for the shared JSON preference file.

The UI has several long-lived widgets and background workers that need to
read the same preferences file.  The old pattern (open, parse, mutate, and
write from each callback) allowed a late worker to overwrite a setting that
another callback had just changed.  Keeping the merge and replace in one
place also means preference changes are atomic from the point of view of
readers.

This module deliberately has no GTK dependency.  That keeps it usable from
the player, which is initialized before a display exists, and makes the
behaviour straightforward to test without opening a window.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


_PREFS_LOCK = threading.RLock()


@contextmanager
def _prefs_file_lock(path: str, exclusive: bool):
    """Serialize preference reads/updates across app processes.

    POSIX uses ``flock``.  Windows has no ``fcntl`` module, but its CRT
    exposes the equivalent byte-range lock; use an exclusive lock even for
    reads there because the portable API does not expose a reliable shared
    lock on every supported Windows version.  The in-process lock still
    protects the common case when neither OS locking API is available.
    """

    if fcntl is None and msvcrt is None:
        yield
        return

    lock_path = f"{path}.lock"
    fd = None
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(lock_path, flags, 0o600)
        if fcntl is not None:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, operation)
        else:
            # msvcrt.locking() locks bytes from the current file position.
            # Keep one sentinel byte in the file and always lock from offset 0.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\x00")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        # Keep lock acquisition errors separate from errors raised by the
        # caller's body.  In particular, a read should not accidentally turn
        # an OSError from JSON/open into a second yield from this context
        # manager.
        yield
    except OSError:
        # If the lock file itself cannot be created, preserve the historical
        # best-effort behavior and let the actual preference operation report
        # any useful filesystem error.  Once a descriptor exists, failing to
        # acquire/release the lock is safer to surface than to silently race.
        if fd is None:
            yield
        else:
            raise
    finally:
        if fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def _read_prefs_unlocked(path, default):
    fallback = dict(default or {})
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            return dict(value)
    except (OSError, ValueError, TypeError):
        pass
    return fallback


def read_prefs(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a defensive copy of the JSON object at *path*.

    A missing, empty, or malformed preferences file is treated as an empty
    object.  Callers can therefore use normal ``dict.get`` defaults without
    having to duplicate error handling.
    """

    with _PREFS_LOCK:
        with _prefs_file_lock(path, exclusive=False):
            return _read_prefs_unlocked(path, default)


def update_prefs(path: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *updates* into *path* and atomically replace the file.

    The in-process lock protects threads, while the advisory lock file
    serializes separate application processes.  The temporary file plus
    ``os.replace`` prevents a partially-written JSON document from being
    observed after a crash.  The returned dictionary is the merged snapshot
    that was written.
    """

    if not updates:
        return read_prefs(path)

    with _PREFS_LOCK:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        with _prefs_file_lock(path, exclusive=True):
            current = _read_prefs_unlocked(path, {})
            current.update(updates)
            fd, temporary = tempfile.mkstemp(
                prefix=".prefs-", suffix=".tmp", dir=directory, text=True
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        current,
                        handle,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    handle.write("\n")
                    handle.flush()
                    # A setting is rare; paying for a durable rename here is
                    # preferable to losing a theme choice on a power loss.
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                # fsync the file before the rename (above) protects its
                # contents; syncing the directory as well makes the rename
                # durable on filesystems that otherwise defer directory
                # metadata.  Windows cannot open a directory this way, so
                # this is intentionally best effort.
                try:
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
    return dict(current)


def user_prefs_path() -> str:
    """Return VenTapes' platform-specific preference path.

    Imported lazily so this helper remains usable in worker/startup code
    that has not imported GLib yet.
    """

    from gi.repository import GLib

    return os.path.join(GLib.get_user_data_dir(), "ventapes", "prefs.json")


def get_bool(prefs: Dict[str, Any], key: str, default: bool = False) -> bool:
    """Read a boolean preference without Python's surprising truthiness.

    Unrecognized strings and non-scalar values use *default* rather than
    silently becoming ``True``.
    """

    value = prefs.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        return bool(default)
    if isinstance(value, (int, float)):
        try:
            if math.isfinite(float(value)):
                return value != 0
        except (OverflowError, TypeError, ValueError):
            pass
    return bool(default)


def get_float(
    prefs: Dict[str, Any],
    key: str,
    default: float,
    low: Optional[float] = None,
    high: Optional[float] = None,
) -> float:
    """Read a finite, optionally clamped floating-point preference."""

    try:
        value = float(prefs.get(key, default))
    except (OverflowError, TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    if low is not None:
        value = max(float(low), value)
    if high is not None:
        value = min(float(high), value)
    return value


def get_int(
    prefs: Dict[str, Any],
    key: str,
    default: int,
    low: Optional[int] = None,
    high: Optional[int] = None,
) -> int:
    """Read an integer preference, falling back rather than raising."""

    try:
        value = int(prefs.get(key, default))
    except (TypeError, ValueError, OverflowError):
        value = int(default)
    if low is not None:
        value = max(int(low), value)
    if high is not None:
        value = min(int(high), value)
    return value
