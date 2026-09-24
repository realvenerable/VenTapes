"""User preferences for the lyrics pipeline.

Covers four things, all stored in the shared ``prefs.json``:

- ``lyrics_provider_order`` — the search queue. An ordered list of
  provider display names; the chain tries them top-down.
- ``lyrics_providers_disabled`` — names the user switched off. Kept as a
  separate list (rather than dropping them from the order) so toggling a
  provider back on restores its position instead of appending it last.
- ``lyrics_match_mode`` — ``quality`` walks past a provider that only has
  plain unsynced text when a later one might have synced lyrics, falling
  back to the plain hit if nothing better turns up. ``strict`` takes the
  first provider that returns anything at all.
- ``lyrics_second_line`` / ``lyrics_effects`` — display options consumed
  by the lyrics widget.

A provider added in a later release isn't in the saved order, so
:func:`provider_order` appends unknown-but-known-to-the-app names at the
end rather than losing them.

Reads use the shared atomic preference store. The fetch chain asks for the
order on every track change from a worker thread, and the widget asks per row
build; merge-based writes keep those readers from losing one another's keys.
"""

import os
import threading

from ui.preferences import (
    get_bool,
    get_float,
    read_prefs,
    update_prefs,
    user_prefs_path,
)


# Canonical provider names, in the order the chain used before the queue
# was configurable. api/client.py maps these to fetchers; keep the two
# lists in sync.
DEFAULT_PROVIDER_ORDER = [
    "Apple Music",
    "BetterLyrics",
    "BiniLyrics",
    "NetEase",
    "LRCLIB",
    "YouTube Music",
]

MATCH_QUALITY = "quality"
MATCH_STRICT = "strict"

SECOND_LINE_MODES = ("off", "auto", "romanization", "translation", "background")
SECOND_LINE_DEFAULT = "auto"
EFFECTS_LEVELS = ("off", "subtle", "full")
EFFECTS_DEFAULT = "full"

# Multiplier on the lyric column's resting type size.
FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_DEFAULT = 0.5, 1.45, 1.0
# How much bigger the active line is drawn than the resting ones. The
# row's own height never changes, and a line with no slack to grow into
# is capped to what fits, so this can be assertive by default.
ACTIVE_SCALE_MIN, ACTIVE_SCALE_MAX, ACTIVE_SCALE_DEFAULT = 1.0, 1.3, 1.2

_lock = threading.Lock()
_cache = None
_cache_mtime = -1.0
_cache_stat_key = None


def _path():
    return user_prefs_path()


def _stat_key(path):
    try:
        stat = os.stat(path)
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return None


def _read():
    """Return a consistent prefs snapshot for lyric workers.

    The shared preference store performs an atomic, merge-based write.  Keep
    a small stat-keyed cache for row construction (the widget asks for the
    same handful of settings repeatedly), while the inode/size/mtime key
    still notices a cross-process atomic replacement even on filesystems
    with coarse timestamps.
    """
    global _cache, _cache_mtime, _cache_stat_key
    path = _path()
    stat_key = _stat_key(path)
    with _lock:
        if _cache is not None and stat_key is not None and stat_key == _cache_stat_key:
            return dict(_cache)
    before_stat_key = _stat_key(path)
    try:
        data = dict(read_prefs(path, {}))
    except Exception as exc:
        print(f"[LYRICS-PREFS] read failed: {exc}")
        data = {}
        after_stat_key = None
    else:
        after_stat_key = _stat_key(path)
    # Keep the key from the same read window.  Re-statting after taking the
    # cache lock can associate an older snapshot with a newer file when
    # another thread/process commits between those operations.  If the file
    # changed while it was being read, leave the cache cold so the next
    # caller retries rather than pinning a stale snapshot to the new key.
    read_stat_key = (
        after_stat_key
        if before_stat_key is not None
        and before_stat_key == after_stat_key
        else None
    )
    with _lock:
        _cache = data
        _cache_stat_key = read_stat_key
        try:
            _cache_mtime = os.path.getmtime(path)
        except OSError:
            _cache_mtime = -1.0
        return dict(data)


def _write(key, value):
    global _cache, _cache_mtime, _cache_stat_key
    path = _path()
    try:
        update_prefs(path, {key: value})
    except Exception as exc:
        print(f"[LYRICS-PREFS] write failed: {exc}")
        return
    # Do not install the snapshot returned by update_prefs directly here.
    # Another writer may commit before this thread reaches the lock; keeping
    # a complete (but potentially older) dict would then masquerade as the
    # newest file.  Invalidation is cheap and the next row build repopulates
    # the cache with a stat-keyed read.
    with _lock:
        _cache = None
        _cache_stat_key = None
        _cache_mtime = -1.0


def invalidate():
    """Drop the local stat cache after an external preference change."""
    global _cache, _cache_mtime, _cache_stat_key
    with _lock:
        _cache = None
        _cache_mtime = -1.0
        _cache_stat_key = None


def full_provider_order():
    """Every known provider in the user's order, disabled ones included.
    Saved names the app no longer ships are dropped; providers the app
    ships that aren't in the saved order are appended in catalog order."""
    saved = _read().get("lyrics_provider_order")
    if not isinstance(saved, list):
        return list(DEFAULT_PROVIDER_ORDER)
    known = set(DEFAULT_PROVIDER_ORDER)
    out = []
    for name in saved:
        if isinstance(name, str) and name in known and name not in out:
            out.append(name)
    for name in DEFAULT_PROVIDER_ORDER:
        if name not in out:
            out.append(name)
    return out


def disabled_providers():
    saved = _read().get("lyrics_providers_disabled")
    if not isinstance(saved, list):
        return set()
    return {n for n in saved if isinstance(n, str)}


def provider_order():
    """The search queue: enabled providers only, in user order. Never
    returns an empty list — switching every provider off would leave the
    lyrics view permanently blank with no way to tell why, so an
    all-disabled config falls back to the catalog default."""
    disabled = disabled_providers()
    order = [n for n in full_provider_order() if n not in disabled]
    return order or list(DEFAULT_PROVIDER_ORDER)


def set_provider_order(order):
    if not isinstance(order, (list, tuple)):
        order = []
    known = set(DEFAULT_PROVIDER_ORDER)
    _write(
        "lyrics_provider_order",
        [name for name in order if isinstance(name, str) and name in known],
    )


def set_provider_enabled(name, enabled):
    if not isinstance(name, str) or name not in DEFAULT_PROVIDER_ORDER:
        return
    disabled = disabled_providers()
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    _write("lyrics_providers_disabled", sorted(disabled))


def match_mode():
    val = _read().get("lyrics_match_mode", MATCH_QUALITY)
    return val if val in (MATCH_QUALITY, MATCH_STRICT) else MATCH_QUALITY


def set_match_mode(mode):
    if mode not in (MATCH_QUALITY, MATCH_STRICT):
        mode = MATCH_QUALITY
    _write("lyrics_match_mode", mode)


def second_line_mode():
    val = _read().get("lyrics_second_line", SECOND_LINE_DEFAULT)
    return val if val in SECOND_LINE_MODES else SECOND_LINE_DEFAULT


def set_second_line_mode(mode):
    # An unknown mode renders a blank second line with nothing checked in
    # the picker, which reads as "off". Normalize instead of storing it.
    if mode not in SECOND_LINE_MODES:
        mode = SECOND_LINE_DEFAULT
    _write("lyrics_second_line", mode)


def ensure_second_line_mode():
    """Write the default out when the key is missing or unusable, so the
    setting is never left implicit. The shared preference store makes this
    safe alongside lyric workers reading the same file."""
    val = _read().get("lyrics_second_line")
    if val not in SECOND_LINE_MODES:
        _write("lyrics_second_line", SECOND_LINE_DEFAULT)
    return second_line_mode()


def line_sweep():
    """Whether a line-synced source gets synthesized per-word timing so
    its highlight travels across the line."""
    return get_bool(_read(), "lyrics_line_sweep", True)


def set_line_sweep(enabled):
    _write("lyrics_line_sweep", bool(enabled))


def _clamped_float(key, default, low, high):
    return get_float(_read(), key, default, low, high)


def font_scale():
    return _clamped_float(
        "lyrics_font_scale", FONT_SCALE_DEFAULT, FONT_SCALE_MIN, FONT_SCALE_MAX
    )


def set_font_scale(value):
    _write("lyrics_font_scale", float(value))


def active_scale():
    return _clamped_float(
        "lyrics_active_scale", ACTIVE_SCALE_DEFAULT,
        ACTIVE_SCALE_MIN, ACTIVE_SCALE_MAX,
    )


def set_active_scale(value):
    _write("lyrics_active_scale", float(value))


def effects_level():
    val = _read().get("lyrics_effects", EFFECTS_DEFAULT)
    return val if val in EFFECTS_LEVELS else EFFECTS_DEFAULT


def set_effects_level(level):
    _write("lyrics_effects", level)
