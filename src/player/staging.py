"""Small, dependency-free helpers for bounded playback staging files.

The player stages a few streams locally when GStreamer cannot seek a remote
source reliably.  Keeping the filesystem policy here makes it possible to
test the safety limits without importing GTK or GStreamer.
"""

from __future__ import annotations

import math
import os
import shutil
import time
from typing import Iterable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


STAGING_DIR_PREFIX = "ventapes-stream-"
DEFAULT_STAGING_LIMIT_MB = 512
DEFAULT_STALE_SECONDS = 3600
STAGING_AUDIO_BASENAME = "audio"
_TEMP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
STAGING_LEASE_FILENAME = ".ventapes-lease"
STAGING_LEASE_GRACE_SECONDS = 15.0
MIN_STAGING_LIMIT_MB = 1
MAX_STAGING_LIMIT_MB = 2048


class StagingLease:
    """A small OS-backed ownership lease for one staging path.

    The lock lives for the whole download/playback handoff, rather than only
    while bytes are being written.  A crashed process releases it with its
    file descriptors; the marker/mtime left behind lets a later process
    distinguish that dead directory from a live owner's directory.
    """

    def __init__(self, path, fd):
        self.path = path
        self.fd = fd
        self._closed = False

    def refresh(self):
        if self._closed:
            return False
        try:
            os.utime(self.path, None)
            return True
        except OSError:
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _lock_file(fd, blocking):
    """Take an exclusive lock on *fd*, returning whether it succeeded."""

    if fcntl is not None:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        return True
    if msvcrt is not None:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\x00")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(
            fd,
            msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,
            1,
        )
        return True
    return True


def acquire_staging_lock(path, blocking=True):
    """Acquire an exclusive advisory lock at *path*.

    This is used both for the cross-process aggregate staging budget and for
    per-directory ownership.  ``None`` means another process owns the lock
    (or the lock could not be created); callers can fall back to streaming.
    """

    if not path:
        return None
    directory = os.path.dirname(path) or "."
    fd = None
    try:
        os.makedirs(directory, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(path, flags, 0o600)
        _lock_file(fd, blocking)
        return StagingLease(path, fd)
    except (OSError, IOError):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return None


def acquire_staging_lease(directory, blocking=True):
    """Acquire the ownership lease inside a staging directory."""

    if not directory:
        return None
    return acquire_staging_lock(
        os.path.join(directory, STAGING_LEASE_FILENAME), blocking=blocking
    )


def refresh_staging_lease(lease):
    return lease is not None and lease.refresh()


def staging_lease_state(directory):
    """Return ``held``, ``released``, or ``missing`` for a directory lease."""

    lease_path = os.path.join(directory, STAGING_LEASE_FILENAME)
    if not os.path.exists(lease_path):
        return "missing"
    fd = None
    try:
        flags = os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(lease_path, flags)
        _lock_file(fd, blocking=False)
    except (OSError, IOError):
        return "held"
    else:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return "released"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def staging_activity_mtime(path):
    """Return the newest mtime in a staging tree, without following links."""

    newest = 0.0
    try:
        newest = os.lstat(path).st_mtime
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
                except OSError:
                    continue
    except OSError:
        return newest
    return newest


class StagingCancelled(Exception):
    """Raised inside a downloader progress hook when its job is stale."""


class StagingLimitExceeded(Exception):
    """Raised when a staged file would exceed the configured budget."""


def limit_bytes_from_mb(value, default_mb=DEFAULT_STAGING_LIMIT_MB) -> int:
    """Parse a staging budget in MiB and clamp it to a safe range.

    ``VENTAPES_STAGING_MAX_MB`` is useful for constrained deployments and
    tests.  Invalid values fall back rather than accidentally disabling the
    limit.
    """

    try:
        megabytes = float(value)
    except (TypeError, ValueError):
        megabytes = float(default_mb)
    if not math.isfinite(megabytes):
        megabytes = float(default_mb)
    megabytes = max(
        float(MIN_STAGING_LIMIT_MB),
        min(float(MAX_STAGING_LIMIT_MB), megabytes),
    )
    return int(megabytes * 1024 * 1024)


def directory_size(path: str) -> int:
    """Return the bytes stored below *path*, without following symlinks."""

    if not path:
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    stat = os.lstat(os.path.join(root, name))
                    total += stat.st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def reported_size_exceeds(status, budget: int) -> bool:
    """Whether yt-dlp's reported byte counters exceed ``budget``."""

    if not isinstance(status, dict):
        return False
    for key in ("downloaded_bytes", "total_bytes", "total_bytes_estimate"):
        try:
            value = int(status.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > budget:
            return True
    return False


def find_completed_audio(
    directory: str, expected_name: Optional[str] = None
) -> Optional[str]:
    """Return the one completed ``audio.*`` file, or ``None``.

    yt-dlp's fragment files use names such as ``audio.m4a.part-Frag0``;
    checking only a suffix would incorrectly accept one after a crash.  A
    caller-supplied final filename wins when it is a safe direct child, and
    an ambiguous directory is treated as incomplete rather than returning an
    arbitrary format that may be the wrong track.
    """

    try:
        entries = list(os.scandir(directory))
    except OSError:
        return None

    def _usable(entry):
        name = entry.name
        lower_name = name.lower()
        if not name.startswith(STAGING_AUDIO_BASENAME + "."):
            return False
        if any(marker in lower_name for marker in _TEMP_SUFFIXES):
            return False
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                return False
            return entry.stat(follow_symlinks=False).st_size > 0
        except OSError:
            return False

    candidates = [entry for entry in entries if _usable(entry)]
    if expected_name:
        expected_name = os.path.basename(str(expected_name))
        for entry in candidates:
            if entry.name == expected_name:
                return entry.path

    if len(candidates) == 1:
        return candidates[0].path
    return None


def is_staging_dir(path: str, roots: Optional[Iterable[str]] = None) -> bool:
    """Whether *path* is a direct child staging directory we may remove.

    When roots are supplied, destructive callers get a second ownership
    check: a nested directory with a tempting prefix is not ours merely
    because it happens to live below a root.
    """

    if not path:
        return False
    name = os.path.basename(os.path.normpath(path))
    if not name.startswith(STAGING_DIR_PREFIX):
        return False
    if not os.path.isdir(path) or os.path.islink(path):
        return False
    if roots is None:
        return True

    if isinstance(roots, (str, bytes)):
        roots = (roots,)
    candidate = os.path.realpath(os.path.abspath(path))
    for root in roots:
        if not root:
            continue
        real_root = os.path.realpath(os.path.abspath(root))
        try:
            if os.path.dirname(candidate) == real_root:
                return True
        except (OSError, ValueError):
            continue
    return False


def remove_staging_dir(path: str, roots: Optional[Iterable[str]] = None) -> bool:
    """Remove one owned staging directory; return whether it was removed."""

    # A root is mandatory for destructive use.  ``is_staging_dir`` can still
    # be used without one as a non-destructive predicate.
    if isinstance(roots, (str, bytes)):
        roots = (roots,)
    if not roots or not is_staging_dir(path, roots):
        return False
    # Re-check immediately before the destructive operation.  A live owner
    # may have acquired the lease since the initial ownership predicate ran.
    if staging_lease_state(path) == "held":
        return False
    try:
        if staging_lease_state(path) == "held":
            return False
        shutil.rmtree(path, ignore_errors=False)
        return True
    except OSError:
        return False


def sweep_staging_dirs(
    root: str,
    keep: Iterable[str] = (),
    min_age_seconds: float = DEFAULT_STALE_SECONDS,
) -> int:
    """Remove stale direct-child staging directories below *root*.

    ``keep`` may contain active directory paths or files inside them.  A
    directory with a live lease is never removed, and a recent legacy
    directory is left alone so a second VenTapes instance (or a job that has
    just been created) cannot be swept underneath its writer.
    """

    if not root:
        return 0
    keep_real = set()
    for path in keep:
        if not path:
            continue
        absolute = os.path.abspath(path)
        real = os.path.realpath(absolute)
        keep_real.add(real)
        # Callers sometimes only have the completed audio file.  Keep its
        # containing staging directory as well.
        if not os.path.isdir(absolute):
            keep_real.add(os.path.dirname(real))

    removed = 0
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        path = entry.path
        if not is_staging_dir(path, (root,)):
            continue
        real = os.path.realpath(os.path.abspath(path))
        if real in keep_real:
            continue
        try:
            lease_state = staging_lease_state(path)
            if lease_state == "held":
                # A live process owns this directory even if its directory
                # mtime is old because it is still writing an existing file.
                continue
            activity = staging_activity_mtime(path)
            # New-style crashed jobs leave a released lease marker and can be
            # reclaimed promptly.  Legacy directories have no owner metadata,
            # so retain the age grace period to avoid touching an older app
            # instance that may still be writing one.
            if (
                min_age_seconds > 0
                and lease_state == "missing"
                and now - activity < min_age_seconds
            ):
                continue
            if lease_state == "released":
                if (
                    min_age_seconds <= 0
                    or now - activity >= STAGING_LEASE_GRACE_SECONDS
                ):
                    if staging_lease_state(path) == "held":
                        continue
                    shutil.rmtree(path, ignore_errors=False)
                    removed += 1
                continue
            if min_age_seconds > 0 and now - activity < min_age_seconds:
                continue
            if staging_lease_state(path) == "held":
                continue
            shutil.rmtree(path, ignore_errors=False)
            removed += 1
        except OSError:
            continue
    return removed
