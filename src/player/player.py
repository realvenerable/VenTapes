# VenTapes (modified 2026-09-24) is based on Mixtapes and remains GPL-3.0-or-later.
# See ../../NOTICE.md and ../../CREDITS.md.

import gi
import sys
import threading
import random
import os
import shutil
import json
import queue
import time


class _YdlLogger:
    """Custom yt-dlp logger that mutes the noisy, harmless PO-Token provider
    chatter. We mint tokens via rustypipe-botguard; the built-in bgutil:http
    provider is an unused fallback that pings a localhost server nobody runs
    (`http://127.0.0.1:4416/ping`) and warns on every extraction. Everything
    else passes through unchanged."""

    _MUTE = (
        "bgutil",
        "127.0.0.1:4416",
        "No request handlers configured",
    )

    def _muted(self, msg):
        return any(m in msg for m in self._MUTE)

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if not self._muted(msg):
            print(msg)

    def error(self, msg):
        print(msg)


def _find_botguard_bin():
    """Locate the rustypipe-botguard binary used by the yt-dlp PO-Token
    provider (yt-dlp-get-pot-rustypipe). Some inherited packaging recipes and
    local builds may ship the binary, so we check the bundled spots first,
    then PATH, then the usual manual-install locations. Desktop launchers /
    frozen builds often start with a PATH that omits ~/.cargo/bin, so we hand yt-dlp an
    explicit path. Returns None if it isn't found anywhere — playback still
    works, only PO-Token-gated formats (e.g. seekable Opus) stay out of reach."""
    win = sys.platform == "win32"
    name = "rustypipe-botguard.exe" if win else "rustypipe-botguard"

    def _usable(p):
        return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)

    # 1) Bundled alongside the app. Look relative to the app root — this file
    #    lives at <root>/src/player/player.py, so three levels up is <root>
    #    (Windows installer drops it in <root>/windows) — and, for frozen
    #    Nuitka builds, relative to the running executable.
    roots = []
    try:
        roots.append(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
    except NameError:
        pass
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    for root in roots:
        for sub in ("", "bin", "windows"):
            cand = os.path.join(root, sub, name)
            if _usable(cand):
                return cand

    # 2) On PATH — covers the Flatpak sandbox (/app/bin) and a manual
    #    `cargo install rustypipe-botguard` that landed on PATH.
    found = shutil.which(name)
    if _usable(found):
        return found

    # 3) Well-known install locations that are commonly off a launcher's PATH.
    if win:
        candidates = [
            os.path.expanduser("~/.cargo/bin/" + name),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", name),
        ]
    else:
        candidates = [
            "/usr/lib/ventapes/bin/" + name,   # Optional package's private libdir
            os.path.expanduser("~/.cargo/bin/" + name),
            "/usr/local/bin/" + name,
            "/usr/bin/" + name,
        ]
    for c in candidates:
        if _usable(c):
            return c
    return None

gi.require_version("Gst", "1.0")
gi.require_version("GstAudio", "1.0")
from gi.repository import Gst, GstAudio, GObject, GLib, GdkPixbuf
import glob
from yt_dlp import YoutubeDL
from ui.utils import get_high_res_url, get_ytimg_fallbacks
from player.cache import StreamCache
from player.downloads import DownloadManager
from player.staging import (
    DEFAULT_STAGING_LIMIT_MB,
    STAGING_DIR_PREFIX,
    StagingCancelled,
    StagingLimitExceeded,
    acquire_staging_lease,
    acquire_staging_lock,
    directory_size,
    find_completed_audio,
    is_staging_dir,
    limit_bytes_from_mb,
    refresh_staging_lease,
    remove_staging_dir,
    reported_size_exceeds,
    staging_lease_state,
    sweep_staging_dirs,
)
from api.client import MusicClient

HAS_MPRIS = False
HAS_SMTC = False
if sys.platform == "win32":
    try:
        from player.smtc import SMTCAdapter
        HAS_SMTC = True
    except ImportError:
        pass
else:
    try:
        from player.mpris import VenTapesMprisAdapter, VenTapesServer, VenTapesEventAdapter
        HAS_MPRIS = True
    except ImportError:
        pass

from player.discord_rpc import DiscordRPCAdapter
from player.scrobbler import ScrobblerAdapter
from ui.preferences import get_bool, read_prefs, user_prefs_path


def _extract_spectrum_bands(structure):
    """Pull a list[float] of magnitudes out of a GStreamer `spectrum`
    bus-message Structure. Returns None if no path yields data.

    PyGObject's surface for GstValueList varies by version:
      - `structure.get_list("magnitude")` returns (True, Gst.ValueArray)
        on modern builds. The ValueArray is NOT iterable but exposes
        `.n_values` + `.get_nth(i)`.
      - `structure.get_value("magnitude")` may return a plain Python
        list, a GValueArray-like with `.n_values`, or raise
        `TypeError: unknown type GstValueList` if PyGObject doesn't
        have a converter registered for the inner GType.
    Try both APIs and walk whatever shape comes back.
    """
    def _walk(obj):
        if obj is None:
            return None
        if isinstance(obj, (list, tuple)):
            try:
                return [float(v) for v in obj]
            except Exception:
                return None
        if hasattr(obj, "n_values") and hasattr(obj, "get_nth"):
            try:
                out = []
                for i in range(obj.n_values):
                    v = obj.get_nth(i)
                    out.append(
                        float(v.get_float()) if hasattr(v, "get_float") else float(v)
                    )
                return out
            except Exception:
                return None
        try:
            return [float(v) for v in obj]
        except Exception:
            return None

    try:
        ok, mags = structure.get_list("magnitude")
        if ok:
            bands = _walk(mags)
            if bands:
                return bands
    except Exception:
        pass

    try:
        raw = structure.get_value("magnitude")
    except Exception:
        raw = None
    return _walk(raw)


def _is_manifest_protocol(protocol):
    value = str(protocol or "").lower()
    return (
        "m3u8" in value
        or "dash" in value
        or "hls" in value
        or "manifest" in value
    )


def _yt_dlp_final_filename(result):
    """Best-effort extraction of yt-dlp's final output path."""

    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        for item in reversed(result):
            found = _yt_dlp_final_filename(item)
            if found:
                return found
        return None
    if not isinstance(result, dict):
        return None
    for key in ("filepath", "_filename", "filename"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("requested_downloads", "entries"):
        found = _yt_dlp_final_filename(result.get(key))
        if found:
            return found
    return None


def _parse_track_duration(track):
    """Return a positive duration in seconds for `track`, or 0 if unknown.

    ytmusicapi populates `duration_seconds` for most surfaces, but uploaded
    songs from get_library_upload_songs() only carry `duration` as a "M:SS"
    (or "H:MM:SS") string. Check both so the seek-bar fallback works for
    uploads too.
    """
    secs = track.get("duration_seconds")
    if isinstance(secs, (int, float)) and secs > 0:
        return int(secs)
    if isinstance(secs, str) and secs.isdigit():
        n = int(secs)
        if n > 0:
            return n
    dur = track.get("duration")
    if isinstance(dur, str):
        parts = dur.strip().split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            pass
    return 0


class Player(GObject.Object):
    __gsignals__ = {
        "state-changed": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),  # playing, paused, stopped
        "progression": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (float, float),
        ),  # position, duration (seconds) -> Changed to float
        "metadata-changed": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, str, str, str, str),
        ),  # title, artist, thumbnail_url, video_id, like_status
        "volume-changed": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (float, bool),
        ),  # volume, muted
        "track-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, str, str),
        ),  # video_id, title, error_message
    }

    def __init__(self):
        super().__init__()
        GLib.set_application_name("VenTapes")
        Gst.init(None)
        self.client = MusicClient()
        self.player = Gst.ElementFactory.make("playbin", "player")

        # Disable video output using playbin flags (unsetting GST_PLAY_FLAG_VIDEO)
        # GST_PLAY_FLAG_VIDEO is 1 << 0
        flags = self.player.get_property("flags")
        self.player.set_property("flags", flags & ~(1 << 0))

        # The spectrum analyzer is deliberately optional.  It is an audio
        # filter, so leaving it attached even when the bars are hidden makes
        # every track pay for FFT work that cannot be seen.  The widget calls
        # set_visualizer_enabled() when the preference changes, which also
        # makes this work for users who enable it later without restarting.
        prefs = read_prefs(user_prefs_path(), {})
        self._low_power_mode = get_bool(prefs, "low_power_mode", False)
        self._reduce_motion_pref_enabled = get_bool(
            prefs, "reduce_motion", False
        )
        self._reduce_motion = (
            self._reduce_motion_pref_enabled or self._low_power_mode
        )
        self._visualizer_pref_enabled = get_bool(
            prefs, "visualizer_enabled", True
        )
        self._visualizer_enabled = self._visualizer_pref_enabled and not self._low_power_mode
        self._visualizer_spectrum = None
        self._visualizer_bands = 64
        self._visualizer_threshold_db = -80.0
        # Position-keyed queue of (stream_time_ns, bands) entries fed by
        # the spectrum bus message and drained by pull_visualizer_bands.
        # Sized for ~3s of buffer at the spectrum element's 20Hz tick.
        from collections import deque
        self._viz_queue = deque(maxlen=64)
        self._visualizer_active_keys = set()

        # Inject auth cookies + User-Agent into the HTTP source on every
        # source-setup. Helps for non-upload streams that need cookies on
        # follow-up range requests; uploads still go via tmpfs (below).
        self.player.connect("source-setup", self._on_source_setup)

        # Tracks the current /dev/shm-backed local file for upload playback.
        # Deleted when a new track loads or the app shuts down so we don't
        # leak hundreds of MB into RAM across a long session.
        self._current_tmpfs_path = None
        # A local fallback remains open until the serialized pipeline worker
        # has actually reached NULL.  Removing it earlier can unlink the file
        # underneath a still-playing GStreamer source.
        self._pending_tmpfs_cleanups = []
        self._staging_paths = set()
        # A completed file is leased until the GTK thread adopts it (or
        # cleanup explicitly releases it).  Keep ready paths separate from
        # active download jobs so shutdown cannot mistake the handoff window
        # for abandoned work.
        self._staging_leases = {}
        self._staging_ready_paths = {}
        self._staging_budget_lock_path = None

        # Videos whose stream can't be seeked (e.g. YouTube only offers a
        # progressive m4a whose container has no usable seek index, so
        # qtdemux rejects mid-stream seeks). Once detected, we play them the
        # same way as upload-locker tracks: download into tmpfs and play the
        # local file, which *is* seekable. Seeded from disk so a track only
        # has to fail once, ever.
        self._noseek_vids = set()
        self._load_noseek_vids()
        # Position to apply once a (re)loaded stream finishes prerolling —
        # set by the seek-fallback so we resume where the user aimed.
        self._seek_after_load = None
        self._pending_seek = None
        # Guards against firing multiple tmpfs downloads for one failed seek.
        self._seek_fallback_active = False
        self._seek_fallback_generation = None
        self._pending_seek_fallback = None

        self.ydl_opts = {
            "js_runtimes": {"node": {}},
            # Prefer a *progressive HTTPS* audio stream (itag 251 opus / 140
            # m4a). Those honor byte-Range requests, which is what GStreamer's
            # souphttpsrc uses to seek. When YouTube gates the progressive
            # formats (PO-Token / tv-DRM / SABR-only experiments), a plain
            # "bestaudio" silently falls back to HLS (m3u8_native) or DASH —
            # GStreamer reports those seekable=True but rejects the actual
            # seek ("seek rejected by pipeline"). So explicitly avoid m3u8/dash
            # here, and only fall back to them as a last resort so at least
            # playback still works. format_sort biases the same way.
            # Explicitly prefer Opus, then any progressive HTTPS audio.
            "format": (
                "bestaudio[acodec=opus]/bestaudio[protocol=https]/"
                "bestaudio[protocol=http]/bestaudio/best"
            ),
            # proto:https keeps us on byte-range-seekable progressive streams;
            # acodec:opus then prefers WebM/Opus (itag 251) over MP4/AAC (itag
            # 140). That matters because GStreamer's qtdemux can't reliably
            # seek YouTube's progressive m4a (no usable mfra/sidx index) while
            # matroskademux seeks WebM/Opus fine via its Cues. When Opus is
            # gated behind a PO token, this falls back to m4a so playback
            # still works — only seeking suffers there.
            "format_sort": ["proto:https", "acodec:opus"],
            "quiet": True,
            "noplaylist": True,
            # Mutes the unused bgutil PO-Token provider's localhost-ping warning.
            "logger": _YdlLogger(),
            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "web_music",
                        "mweb",
                        "tv",
                        "web_safari",
                        "android_vr",
                        "android",
                        "ios",
                    ],
                }
            },
        }

        # PO-Token provider (yt-dlp-get-pot-rustypipe). When the rustypipe-
        # botguard binary is present, hand yt-dlp its explicit path so the
        # provider works even when ~/.cargo/bin isn't on PATH. This unlocks
        # the GVS-PO-Token-gated formats (notably seekable Opus / itag 251)
        # that YouTube now requires a token for. Absent the binary, yt-dlp
        # just skips the provider and we fall back to whatever's ungated.
        botguard_bin = _find_botguard_bin()
        if botguard_bin:
            self.ydl_opts["extractor_args"]["youtubepot-rustypipebotguard"] = {
                "rustypipe_bg_bin": [botguard_bin],
            }
            # Also surface it on PATH for the provider's own discovery / any
            # snapshot side files it writes next to the binary.
            bin_dir = os.path.dirname(botguard_bin)
            if bin_dir and bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            print(f"[POT] rustypipe-botguard found: {botguard_bin}")
        else:
            print(
                "[POT] rustypipe-botguard not found — PO-Token-gated formats "
                "(e.g. seekable Opus) unavailable. Install from "
                "https://codeberg.org/ThetaDev/rustypipe-botguard"
            )

        self.bus = self.player.get_bus()
        self.bus.add_signal_watch()
        self._bus_handler_id = self.bus.connect("message", self.on_message)

        # Gapless playback: about-to-finish fires when the current uri is
        # close to ending. If we hand playbin a new uri synchronously in
        # the handler, it switches without re-creating the pipeline — no
        # NULL transition, no preroll, no audible gap. Critical for album
        # listening where tracks are mastered to flow together.
        self.player.connect("about-to-finish", self._on_about_to_finish)
        self._pending_gapless_index = None  # set by about-to-finish, cleared by stream-start
        self._pending_gapless_generation = None

        # Listen for external volume changes (system mixer)
        self.player.connect("notify::volume", self._on_external_volume_change)
        self.player.connect("notify::mute", self._on_external_mute_change)
        self._internal_volume_change = False
        self._user_volume = None # initially set as none, and will depend on wireplumber's external volume change call to set its initial volume (from last session)
        self._track_started_at = 0.0

        self.current_video_id = None
        self._current_source_video_id = None

        # Queue State
        self.queue = []  # List of dicts: {id, title, artist, thumb, ...}
        self.current_queue_index = -1
        self.shuffle_mode = False
        self.original_queue = []  # Backup for un-shuffle
        self.load_generation = 0  # To handle race conditions in loading
        # Coordinates generation invalidation with the GStreamer
        # about-to-finish callback.  The callback may briefly hand playbin a
        # URI, but stop/load must be able to invalidate that plan atomically
        # before it can be applied on the main thread.
        self._generation_lock = threading.RLock()
        self.mpris_art_url = None
        self.current_url = None
        # Diagnostics for the "Stream Info (Debug)" panel. Filled at
        # resolution time (itag/protocol/ext from yt-dlp) and at source-setup
        # (the HTTP source element playbin picked); the rest is queried live.
        self._stream_debug = {}
        self._source_factory_name = None
        self._current_play_uri = None
        self._current_play_uri_generation = None
        self.last_seek_time = 0.0
        self.duration = -1
        self._is_loading = False
        self._current_logical_state = "stopped"
        # History sync: record a play to the user's YT account.
        # `_history_mode` is one of:
        #   - "immediate" (default, matches YT Music's behavior): record
        #     as soon as the track starts loading.
        #   - "after_30s": wait until the user has actually been playing
        #     it for 30 seconds.
        #   - "never": don't record.
        # Keyed by videoId so a single track is only reported once even
        # if the user seeks around inside it.
        self._history_recorded_for = None
        self._history_record_after_sec = 30.0
        self._history_mode = self._load_history_mode()

        # New modes
        self.repeat_mode = "none"  # none, track, all
        self.queue_source_id = None
        self.queue_is_infinite = False
        self._is_fetching_infinite = False

        # Audio snippet cache
        self.stream_cache = StreamCache()

        # Download manager for offline playback
        self.download_manager = DownloadManager(self.client)
        self._playing_from_cache = False
        self._pending_stream_url = None
        self._pending_stream_cache = None

        # The progress timer is started when a track is loaded and stopped
        # when the pipeline is idle.  A permanent 100 ms wake-up was pure
        # background CPU when the app was stopped or only browsing music.
        # (The actual interval is configured in _load_internal/state events.)
        # boolean checker if media api (MPRIS or SMTC) is loaded
        self.media_api_loaded = False

        # Discord Rich Presence is opt-in and requires a VenTapes-specific
        # application ID supplied through VENTAPES_DISCORD_APP_ID.
        try:
            self.discord_rpc = DiscordRPCAdapter(self)
            self.connect("state-changed", self._on_discord_state_changed)
            self.connect("metadata-changed", self._on_discord_metadata_changed)
        except Exception as e:
            print(f"Discord RPC init failed: {e}")
            self.discord_rpc = None

        # Scrobbling to Last.fm / ListenBrainz. Idle until the user connects
        # a service in Preferences.
        try:
            self.scrobbler = ScrobblerAdapter(self)
            self.connect("state-changed", self._on_scrobbler_state_changed)
        except Exception as e:
            print(f"Scrobbler init failed: {e}")
            self.scrobbler = None

        # All pipeline state changes are serialized through one worker.  The
        # old code started a fresh thread for each NULL transition and each
        # URI handoff; a late NULL from the previous track could therefore
        # win the race after the new track reached PLAYING.  The queue keeps
        # transitions ordered and lets us coalesce rapid skip/load bursts.
        self._pipeline_commands = queue.Queue()
        self._pipeline_worker = None
        self._pipeline_worker_lock = threading.Lock()
        self._pipeline_lock = threading.RLock()
        self._pipeline_generation = None
        self._stream_started_generation = None
        self._pipeline_started_at = 0.0
        self._source_generation_lock = threading.Lock()
        self._source_generations = {}
        self._last_source_id = None
        self._last_source_key = None
        self._pipeline_shutdown = False
        self._pipeline_shutdown_sentinel = threading.Event()
        self._pipeline_null_confirmed = threading.Event()
        self._staging_lock = threading.Lock()
        self._staging_jobs = {}
        self._staging_active = False
        # Staging is deliberately bounded.  A bad/live stream must not be
        # able to fill /dev/shm (or the disk fallback) while the user skips
        # through a queue.  Deployments can lower the cap without changing
        # code; the UI still falls back to ordinary streaming when the cap is
        # reached.
        self._staging_limit_bytes = limit_bytes_from_mb(
            os.environ.get("VENTAPES_STAGING_MAX_MB", DEFAULT_STAGING_LIMIT_MB)
        )
        self._position_timer_id = 0
        self._progress_interval_ms = 500 if self._low_power_mode else 250
        self._buffering_since = 0.0
        self._last_progress_at = 0.0
        self._last_progress_position = 0.0
        self._stall_recovery_active = False
        self._last_emitted_position = -1.0
        self._last_emitted_duration = -1.0
        self._last_position_seconds = 0.0
        self._last_duration_seconds = 0.0
        self._next_duration_probe = 0.0
        self._precache_enabled = get_bool(
            prefs, "precache_next", True
        ) and not self._low_power_mode
        self._precache_lock = threading.Lock()
        self._precache_epoch = 0
        self._precache_cancel = threading.Event()
        self._precache_downloaders = set()

        # Register only after all cleanup state exists; a failed constructor
        # must not leave an atexit hook that dereferences half-built fields.
        import atexit
        atexit.register(self._cleanup_all_tmpfs)
        # Reclaim dead owners from an earlier process during construction.
        # New leases make this safe even for recently-created directories;
        # legacy directories still use the age grace period.
        try:
            self._sweep_staging_roots()
        except Exception:
            pass

    def _enable_spectrum(self):
        """Attach the lightweight spectrum filter when the UI needs it."""

        if self._visualizer_spectrum is not None:
            return
        spectrum = Gst.ElementFactory.make("spectrum", "visualizer-spectrum")
        if spectrum is None:
            print(
                "[VISUALIZER] spectrum element NOT available "
                "(missing gst-plugins-good in this runtime) — bars will be inert"
            )
            return
        try:
            spectrum.set_property("post-messages", True)
            spectrum.set_property("message-magnitude", True)
            spectrum.set_property("message-phase", False)
            # 20 Hz is enough for a 30 FPS bar display and is much cheaper
            # than the old 30 Hz FFT on low-power CPUs.
            spectrum.set_property("interval", 50_000_000)
            spectrum.set_property("bands", self._visualizer_bands)
            spectrum.set_property("threshold", int(self._visualizer_threshold_db))
            spectrum.set_property("multi-channel", False)
            self.player.set_property("audio-filter", spectrum)
            self._visualizer_spectrum = spectrum
            print("[VISUALIZER] spectrum element loaded — bars should animate")
        except Exception as exc:
            print(f"[VISUALIZER] spectrum setup failed: {exc}")

    def _disable_spectrum(self):
        spectrum = self._visualizer_spectrum
        if spectrum is None:
            return
        try:
            self.player.set_property("audio-filter", None)
        except Exception:
            pass
        self._visualizer_spectrum = None
        self._viz_queue.clear()

    def set_visualizer_active(self, consumer, active):
        """Track visible visualizer consumers before attaching the FFT."""

        key = id(consumer)
        if active:
            self._visualizer_active_keys.add(key)
        else:
            self._visualizer_active_keys.discard(key)
        should_run = bool(self._visualizer_active_keys) and self._visualizer_enabled
        if should_run and self._visualizer_spectrum is None:
            self._enable_spectrum()
        elif not should_run and self._visualizer_spectrum is not None:
            self._disable_spectrum()

    def set_visualizer_enabled(self, enabled):
        self._visualizer_pref_enabled = bool(enabled)
        enabled = self._visualizer_pref_enabled and not self._low_power_mode
        changed = enabled != self._visualizer_enabled
        self._visualizer_enabled = enabled
        if enabled and self._visualizer_active_keys:
            self._enable_spectrum()
        elif not enabled:
            self._disable_spectrum()
        elif changed:
            self._enable_spectrum()

    def get_visualizer_enabled(self):
        return bool(self._visualizer_enabled)

    def has_visualizer_spectrum(self):
        return self._visualizer_spectrum is not None

    def set_low_power_mode(self, enabled):
        enabled = bool(enabled)
        if enabled == self._low_power_mode:
            return
        self._low_power_mode = enabled
        self._reduce_motion = (
            self._reduce_motion_pref_enabled or self._low_power_mode
        )
        self._cancel_staging_jobs("power mode changed")
        self.set_visualizer_enabled(self._visualizer_pref_enabled)
        self._progress_interval_ms = 500 if enabled else 250
        if hasattr(self, "_position_timer_id"):
            self._restart_position_timer()
        if enabled:
            self._precache_enabled = False
            self._cancel_precache("low-power enabled")
        else:
            prefs = read_prefs(user_prefs_path(), {})
            self._precache_enabled = get_bool(prefs, "precache_next", True)

    def get_low_power_mode(self):
        return bool(self._low_power_mode)

    def set_reduce_motion(self, enabled):
        self._reduce_motion_pref_enabled = bool(enabled)
        self._reduce_motion = (
            self._reduce_motion_pref_enabled or self._low_power_mode
        )

    def get_reduce_motion(self):
        return bool(self._reduce_motion)

    def _cancel_precache(self, reason="cancelled"):
        with self._precache_lock:
            self._precache_epoch += 1
            cancel = self._precache_cancel
            self._precache_cancel = threading.Event()
            downloaders = list(self._precache_downloaders)
        cancel.set()
        for downloader in downloaders:
            try:
                close = getattr(downloader, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass

    def _precache_still_current(self, epoch, cancel):
        return (
            not cancel.is_set()
            and self._precache_enabled
            and epoch == self._precache_epoch
        )

    def set_precache_enabled(self, enabled):
        new_value = bool(enabled) and not self._low_power_mode
        if new_value == self._precache_enabled:
            if not new_value:
                self._cancel_precache("pre-cache disabled")
            return
        self._precache_enabled = new_value
        if not new_value:
            self._cancel_precache("pre-cache disabled")

    def get_precache_enabled(self):
        return bool(self._precache_enabled)

    def _load_media_api(self):
        "Starts MPRIS or SMTC for Linux or Windows, loads once only when _start_playback is called"
        if self.media_api_loaded:
            return

        self.media_api_loaded = True

        # MPRIS Setup (Linux-only, requires D-Bus)
        if HAS_MPRIS:
            self.mpris_adapter = VenTapesMprisAdapter(self)
            self.mpris_server = VenTapesServer("VenTapes", adapter=self.mpris_adapter)
            self.mpris_events = VenTapesEventAdapter(
                self.mpris_server.root, self.mpris_server.player
            )
            self.mpris_server.set_event_adapter(self.mpris_events)
            self.mpris_server.loop(background=True)

            # Connect signals for MPRIS updates
            self.connect("state-changed", self._on_mpris_state_changed)
            self.connect("metadata-changed", self._on_mpris_metadata_changed)
            self.connect("progression", self._on_mpris_progression)
            self.connect("volume-changed", self._on_mpris_volume_changed)

        # SMTC Setup (Windows-only)
        if HAS_SMTC:
            try:
                self.smtc = SMTCAdapter(self)
                self.connect("state-changed", self._on_smtc_state_changed)
                self.connect("metadata-changed", self._on_smtc_metadata_changed)
                self.connect("progression", self._on_smtc_progression)
            except Exception as e:
                print(f"SMTC init failed: {e}")
                self.smtc = None

    def _on_discord_state_changed(self, obj, state):
        if getattr(self, "discord_rpc", None):
            self.discord_rpc.update()

    def _on_discord_metadata_changed(
        self, obj, title, artist, thumb, video_id, like_status
    ):
        if getattr(self, "discord_rpc", None):
            self.discord_rpc.update()

    def _on_scrobbler_state_changed(self, obj, state):
        if getattr(self, "scrobbler", None):
            self.scrobbler.on_state_changed(state)

    def _notify_scrobbler(self, video_id, title, artist, track=None):
        """Tell the scrobbler a new play started. Called from the same two
        places that reset the history gate, so a mid-load metadata re-emit
        (thumbnail fix, OMV to ATV swap) never restarts the clock."""
        scrobbler = getattr(self, "scrobbler", None)
        if not scrobbler:
            return
        album = ""
        duration = 0.0
        if track:
            album = track.get("album", "")
            if isinstance(album, dict):
                album = album.get("name", "")
            duration = float(_parse_track_duration(track) or 0)
        try:
            scrobbler.on_track_started(
                video_id, title, artist, str(album or ""), duration
            )
        except Exception as e:
            print(f"[SCROBBLE] track start failed: {e}")

    def _on_mpris_state_changed(self, obj, state):
        if hasattr(self, "mpris_events"):
            # Explicitly tell the server the PlaybackStatus changed
            self.mpris_events.on_playpause()
            # Update metadata because length or 'CanGoNext' might have changed
            self.mpris_events.on_player_all()

    def _on_mpris_metadata_changed(
        self, obj, title, artist, thumb, video_id, like_status
    ):
        if hasattr(self, "mpris_events"):
            # Trigger the 'Metadata' property update
            self.mpris_events.on_title()
            # Update UI-related flags like CanGoNext/Previous
            self.mpris_events.on_player_all()

    def _on_mpris_progression(self, obj, pos, dur):
        # We don't usually emit D-Bus signals for every progression tick
        # as it's too frequent, but mpris-server handles position queries.
        pass

    def _on_mpris_volume_changed(self, obj, volume, muted):
        self.mpris_events.on_volume()

    def _on_smtc_state_changed(self, obj, state):
        if hasattr(self, "smtc") and self.smtc:
            self.smtc.update_playback_status(state)
            can_next = self.current_queue_index + 1 < len(self.queue)
            can_prev = self.current_queue_index > 0
            self.smtc.update_controls(can_next=can_next, can_previous=can_prev)

    def _on_smtc_metadata_changed(self, obj, title, artist, thumb, video_id, like_status):
        if hasattr(self, "smtc") and self.smtc:
            self.smtc.update_metadata(title, artist, thumb)

    def _on_smtc_progression(self, obj, pos, dur):
        if hasattr(self, "smtc") and self.smtc:
            self.smtc.update_timeline(pos, dur)

    def load_video(
        self, video_id, title="Loading...", artist="Unknown", thumbnail_url=None
    ):
        """Legacy/Single-track load. Clears queue and plays this one."""
        track = {
            "videoId": video_id,
            "title": title,
            "artist": artist,  # String or list, normalized later
            "thumb": thumbnail_url,
        }
        self.set_queue([track])

    def play_tracks(self, tracks):
        """Sets the queue to the given tracks and starts playback of the first one."""
        self.set_queue(tracks, 0)

    @staticmethod
    def _normalize_watch_playlist_tracks(tracks):
        """watch_playlist results use `thumbnail` (singular); the rest of the
        app expects `thumbnails` and `thumb`. Without this, infinite-radio
        extensions past the first batch end up with no usable thumbnail url,
        which makes MPRIS reuse the previous track's cover."""
        for t in tracks:
            if "thumbnail" in t and "thumbnails" not in t:
                t["thumbnails"] = t["thumbnail"]
            if t.get("thumbnails") and not t.get("thumb"):
                thumbs = t["thumbnails"]
                if isinstance(thumbs, list) and thumbs:
                    t["thumb"] = thumbs[-1].get("url", "")

    def start_radio(self, video_id=None, playlist_id=None):
        """Start a radio (mix) from a song or playlist. Runs in background."""

        def _fetch():
            try:
                data = self.client.get_watch_playlist(
                    video_id=video_id, playlist_id=playlist_id, limit=50, radio=True
                )
                tracks = data.get("tracks", [])
                if tracks:
                    self._normalize_watch_playlist_tracks(tracks)
                    pid = data.get("playlistId")
                    GObject.idle_add(self.set_queue, tracks, 0, False, pid, True)
                else:
                    print("[RADIO] No tracks returned")
            except Exception as e:
                print(f"[RADIO] Error: {e}")

        threading.Thread(target=_fetch, daemon=True).start()

    def play_then_radio(self, tracks, start_index, seed_video_id):
        """Play `tracks` starting at `start_index`, then continue with a radio
        seeded from `seed_video_id` (typically the last track in the group).

        Used by the home feed so that activating a song from a section plays
        the rest of the section, then transitions into an infinite radio mix
        when the section runs out.
        """
        if not tracks or not seed_video_id:
            self.set_queue(tracks, start_index)
            return

        stamp = f"home-radio:{seed_video_id}:{id(tracks)}"
        self.set_queue(tracks, start_index, source_id=stamp)

        def _fetch():
            try:
                data = self.client.get_watch_playlist(
                    video_id=seed_video_id, limit=50, radio=True
                )
                radio_tracks = data.get("tracks", [])
                if not radio_tracks:
                    return
                self._normalize_watch_playlist_tracks(radio_tracks)
                pid = data.get("playlistId")

                def _apply():
                    # If the user already replaced the queue, drop the result.
                    if self.queue_source_id != stamp:
                        return False
                    existing = {
                        t.get("videoId") for t in self.queue if t.get("videoId")
                    }
                    new = [
                        t for t in radio_tracks
                        if t.get("videoId") and t.get("videoId") not in existing
                    ]
                    if new:
                        self.extend_queue(new)
                    # Switch the source over to the real radio playlist so the
                    # built-in infinite extender takes it from here.
                    if pid:
                        self.queue_source_id = pid
                        self.queue_is_infinite = True
                    return False

                GObject.idle_add(_apply)
            except Exception as e:
                print(f"[HOME-RADIO] failed: {e}")

        threading.Thread(target=_fetch, daemon=True).start()

    def set_queue(
        self, tracks, start_index=0, shuffle=False, source_id=None, is_infinite=False
    ):
        """
        Sets the global queue and plays the track at start_index.
        tracks: list of dicts with videoId, title, artist, thumb
        """
        if os.environ.get("VENTAPES_TRACE") == "1":
            import traceback as _tb
            _caller = "".join(_tb.format_stack(limit=4)[:-1])
            print(
                f"[JUMP-TRACE] set_queue n={len(tracks)} start_index={start_index}"
                f" shuffle={shuffle} source_id={source_id}\nCALLER:\n{_caller}",
                flush=True,
            )
        self.stop()
        self.queue = list(tracks)  # Copy for playing
        self.original_queue = list(tracks)  # Backup for un-shuffle
        self.shuffle_mode = shuffle  # Set mode based on request
        self.queue_source_id = source_id
        self.queue_is_infinite = is_infinite
        self._is_fetching_infinite = False

        target_track = (
            self.queue[start_index] if 0 <= start_index < len(self.queue) else None
        )

        if shuffle:
            import random

            # If start_index is valid, we want to play that track FIRST, then shuffle the rest.
            if target_track:
                # Remove target
                self.queue.remove(target_track)
                # Shuffle rest
                random.shuffle(self.queue)
                # Insert target at 0
                self.queue.insert(0, target_track)
                self.current_queue_index = 0
            else:
                random.shuffle(self.queue)
                self.current_queue_index = 0
            # Note: original_queue remains ordered as passed
        else:
            self.current_queue_index = start_index

        if self.current_queue_index >= 0 and self.current_queue_index < len(self.queue):
            self._play_current_index()
        else:
            self.stop()
        self.emit("state-changed", "queue-updated")

    def add_to_queue(self, track, next=False):
        """Adds a track to the queue. if next=True, inserts after current."""
        if next and self.current_queue_index >= 0:
            self.queue.insert(self.current_queue_index + 1, track)
            self.original_queue.insert(
                self.current_queue_index + 1, track
            )  # Keep sync roughly
        else:
            self.queue.append(track)
            self.original_queue.append(track)

        # If nothing is playing, play this
        if self.current_queue_index == -1:
            self.current_queue_index = 0
            self._play_current_index()

        self.emit("state-changed", "queue-updated")

    def add_tracks_to_queue(self, tracks, next=False):
        """Adds multiple tracks to the queue. If next=True, inserts after current."""
        if not tracks:
            return
        if next and self.current_queue_index >= 0:
            pos = self.current_queue_index + 1
            for i, t in enumerate(tracks):
                self.queue.insert(pos + i, t)
                self.original_queue.insert(pos + i, t)
        else:
            self.queue.extend(tracks)
            self.original_queue.extend(tracks)

        if self.current_queue_index == -1:
            self.current_queue_index = 0
            self._play_current_index()

        self.emit("state-changed", "queue-updated")

    def remove_from_queue(self, index):
        if 0 <= index < len(self.queue):
            pop = self.queue.pop(index)
            # Adjust current index
            if index < self.current_queue_index:
                self.current_queue_index -= 1
            elif index == self.current_queue_index:
                # We removed the playing track. Play next?
                if self.current_queue_index < len(self.queue):
                    self._play_current_index()
                else:
                    self.stop()
                    self.current_queue_index = -1

            # Remove from original if present (simplified)
            if pop in self.original_queue:
                self.original_queue.remove(pop)

            self.emit("state-changed", "queue-updated")

    def move_queue_item(self, old_index, new_index):
        if 0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue):
            # Adjust index when moving down to insert before target, accounting for the list shift from popping.

            insert_index = new_index
            if old_index < new_index:
                insert_index -= 1

            item = self.queue.pop(old_index)
            self.queue.insert(insert_index, item)

            # Update current_queue_index
            # This is tricky. Let's just re-find the playing track if possible, or simple math.
            # The Simple math in question:
            if self.current_queue_index == old_index:
                self.current_queue_index = insert_index
            elif old_index < self.current_queue_index <= insert_index:
                self.current_queue_index -= 1
            elif insert_index <= self.current_queue_index < old_index:
                self.current_queue_index += 1

            # Notify UI
            self.emit("state-changed", "queue-updated")
            return True
        return False

    def clear_queue(self):
        self.stop()
        # Bump the load generation so any in-flight _fetch_and_play /
        # yt-dlp resolution that hasn't reached _start_playback yet
        # sees the new generation and aborts. Without this, clicking
        # Clear while a track was loading would let the resolution
        # finish and start playback into an empty queue.
        with self._generation_lock:
            self.load_generation += 1
        self._cancel_staging_jobs("queue cleared")
        self._cancel_precache("queue cleared")
        self.queue = []
        self.original_queue = []
        self.current_queue_index = -1
        self.current_video_id = None
        self._current_source_video_id = None
        self.emit("state-changed", "stopped")
        self.emit("metadata-changed", "", "", "", "", "INDIFFERENT")

    def play_queue_index(self, index):
        if os.environ.get("VENTAPES_TRACE") == "1":
            import traceback as _tb
            _caller = "".join(_tb.format_stack(limit=4)[:-1])
            print(
                f"[JUMP-TRACE] play_queue_index({index}) queue_len={len(self.queue)}"
                f" current_idx={self.current_queue_index}\nCALLER:\n{_caller}",
                flush=True,
            )
        if 0 <= index < len(self.queue):
            self.stop()
            self.current_queue_index = index
            self._play_current_index()
            self._maybe_extend_infinite()

    def next(self):
        if self.current_queue_index + 1 < len(self.queue):
            self.current_queue_index += 1
            self._play_current_index()
            self._maybe_extend_infinite()
        else:
            if self.repeat_mode == "all" and self.queue:
                self.current_queue_index = 0
                self._play_current_index()
            elif (
                self.queue_is_infinite
                and self.queue_source_id
                and self.client
                and self.queue
            ):
                # Queue actually ran out on an infinite source. The standard
                # halfway-trigger should normally hide this, but YT sometimes
                # returns the same radio batch for the same seed videoId and
                # our dedup filter ends up wiping the whole response. Kick
                # off a final extension keyed on the currently-playing track
                # before declaring the queue dead.
                self._force_radio_extend()
            else:
                self.stop()  # End of queue
                self.current_queue_index = -1

    # ── Gapless handlers ──────────────────────────────────────────────────────

    def _compute_next_gapless_index(self):
        """Decide which queue index the current track should flow into,
        applying repeat / end-of-queue rules. Returns None if no gapless
        candidate exists (let the normal EOS path handle it)."""
        if self.repeat_mode == "track":
            return self.current_queue_index
        cur = self.current_queue_index
        if 0 <= cur and cur + 1 < len(self.queue):
            return cur + 1
        if self.repeat_mode == "all" and self.queue:
            return 0
        # Infinite radio queues *can* gapless-into the next track once
        # the extend lands, but if the queue genuinely ran out at this
        # moment we let EOS trigger the force-extend fallback.
        return None

    def _on_about_to_finish(self, _playbin):
        """Runs on the GStreamer streaming thread. Must complete fast —
        playbin uses whatever URI is set when this returns. Only verified
        local files take this path; remote URLs go through the serialized
        loader on EOS so a stalled signed URL cannot bypass recovery."""
        nxt = self._compute_next_gapless_index()
        if nxt is None or nxt >= len(self.queue):
            return

        track = self.queue[nxt]
        if not track:
            return
        # Upload tracks need to be staged onto tmpfs before they're
        # playable — defer to the normal _load_internal path on EOS.
        if track.get("entityId"):
            return

        vid = track.get("videoId")
        if not vid:
            return

        # Only local files take the gapless fast path. A cached remote URL
        # can be a stale/manifest URL that stalls before its first buffer;
        # assigning it directly here bypassed the serialized loader and was
        # able to leave the next track silent until a seek. EOS will use the
        # normal generation-aware load path for remote sources.
        if vid in self._noseek_vids:
            return
        local_path = self.download_manager.get_local_path(vid)
        if not local_path:
            return
        try:
            uri = GLib.filename_to_uri(os.path.abspath(local_path), None)
        except Exception:
            uri = None
        if not uri:
            return

        # about-to-finish runs on GStreamer's streaming thread.  It must not
        # race a normal URI/state transaction; if the worker is busy, decline
        # the fast path and let EOS use the serialized loader instead.
        with self._generation_lock:
            generation = self.load_generation
        if self._pipeline_shutdown:
            return
        if not self._pipeline_lock.acquire(blocking=False):
            return
        try:
            # Stop/load invalidates the generation under the same lock.  Keep
            # this check and the pending assignment together so a stop cannot
            # clear the index and then have this callback restore it.
            with self._generation_lock:
                if (
                    self._pipeline_shutdown
                    or generation != self.load_generation
                ):
                    return
                self._stream_started_generation = None
                self._pipeline_started_at = time.monotonic()
                self.player.set_property("uri", uri)
                # Gapless bypasses _start_playback(), so keep the recovery/play
                # path's notion of the active URI in sync with playbin.
                self._current_play_uri = uri
                self._current_play_uri_generation = generation
                self._pending_gapless_index = nxt
                self._pending_gapless_generation = generation
                print(
                    f"[GAPLESS] queued next uri for index={nxt} vid={vid} "
                    f"({'local' if local_path else 'unknown'})"
                )
        except Exception as e:
            print(f"[GAPLESS] failed to set next uri: {e}")
            self._pending_gapless_index = None
            self._pending_gapless_generation = None
        finally:
            self._pipeline_lock.release()

    def _apply_gapless_transition(self, expected_generation=None):
        """Main-thread finisher for a gapless track swap. Mirrors the
        post-load housekeeping in _load_internal — queue index, history,
        metadata signal, MPRIS, precache — but skips everything pipeline-
        related since playbin already handed the new uri off."""
        nxt = self._pending_gapless_index
        pending_generation = self._pending_gapless_generation
        self._pending_gapless_index = None
        self._pending_gapless_generation = None
        with self._generation_lock:
            if (
                self._pipeline_shutdown
                or pending_generation is None
                or (
                    expected_generation is not None
                    and pending_generation != expected_generation
                )
                or pending_generation != self.load_generation
                or nxt is None
                or nxt < 0
                or nxt >= len(self.queue)
            ):
                return False
            current_gen = self.load_generation + 1
            self.load_generation = current_gen
            self._pipeline_generation = current_gen
            self._stream_started_generation = current_gen
            self._current_play_uri_generation = current_gen
            self._pipeline_null_confirmed.clear()
            # Gapless playback has no fresh STATE_CHANGED(PLAYING) edge.
            # Start the stale-EOS guard at the URI handoff so a late EOS from
            # the old source is still rejected.
            self._track_started_at = time.time()
            self._buffering_since = 0.0
            self._last_progress_at = 0.0
            self._last_progress_position = 0
            self._last_position_seconds = 0.0
            self._last_duration_seconds = 0.0
            self._next_duration_probe = 0.0
            self._used_cached_url = False
            self._fallback_stream_url = None
            self._cache_failed_waiting = False
            self._pending_stream_cache = None
            self._stream_retry_count = 0
            self._stall_recovery_active = False

        self.current_queue_index = nxt
        track = self.queue[nxt]
        # Use the same normalization path as _play_current_index — without
        # this, freshly-queued tracks (where the dict only has `artists`
        # and `thumbnails` lists, not yet the normalized `artist`/`thumb`
        # strings) ship empty values to the UI and the user sees the
        # artist disappear and covers stop loading on every gapless
        # transition.
        video_id, title, artist, thumb, like_status = (
            self._normalize_track_metadata(track)
        )

        self.current_video_id = video_id
        self._current_source_video_id = video_id
        self.duration = -1
        # Spectrum stream-times restart at 0 for the new uri, so the queue
        # would otherwise be polluted by the previous track's tail.
        self._viz_queue.clear()
        self._notify_scrobbler(video_id, title, artist, track)
        # New track → fresh history-record gate.
        self._history_recorded_for = None
        if (
            self._history_mode == "immediate"
            and video_id
            and self._history_recorded_for != video_id
        ):
            self._history_recorded_for = video_id
            try:
                self.client.add_history_item_async(video_id)
            except Exception as e:
                print(f"[HISTORY] gapless immediate record failed: {e}")

        # A gapless local transition has no NULL phase; keep the old file
        # until the next serialized stop/play transaction can release it.
        if self._current_tmpfs_path:
            self._defer_tmpfs_cleanup(self._current_tmpfs_path)

        self._cancel_staging_jobs("gapless transition")
        self._cancel_precache("gapless transition")
        with self._source_generation_lock:
            if self._last_source_id is not None:
                self._source_generations[self._last_source_id] = current_gen
            if self._last_source_key is not None:
                try:
                    self._source_generations[self._last_source_key] = current_gen
                except (TypeError, AttributeError):
                    pass

        self.emit(
            "metadata-changed",
            title, artist, thumb, video_id, like_status,
        )
        if thumb and hasattr(self, "mpris_events"):
            self._sync_mpris_art(thumb, video_id)
        self._update_logical_state()
        if hasattr(self, "mpris_events"):
            try:
                self.mpris_events.on_player_all()
            except Exception as e:
                print(f"mpris ERROR: {e}")

        # Top up the cache for whatever comes after this newly-current track.
        if self._precache_enabled:
            threading.Thread(
                target=self._precache_next,
                args=(current_gen,),
                kwargs={"max_count": 1},
                daemon=True,
            ).start()
        return False

    def _maybe_extend_infinite(self):
        """Trigger background fetch of more radio tracks when the queue is
        running low. Called from manual skip, next(), and seek paths.

        We fire earlier than the old "halfway" trigger — extending at
        halfway leaves no headroom if the fetch is slow OR if dedup kills
        the response. Firing when fewer than 15 tracks remain (or we're
        past halfway, whichever comes first) gives us multiple shots."""
        if not (self.queue_is_infinite and self.queue_source_id and self.client):
            return
        if self._is_fetching_infinite:
            return
        if self.current_queue_index < 0 or not self.queue:
            return

        remaining = len(self.queue) - 1 - self.current_queue_index
        past_halfway = self.current_queue_index >= len(self.queue) // 2
        if remaining <= 15 or past_halfway:
            self._start_infinite_fetch()

    def _force_radio_extend(self):
        """Last-ditch radio extension when the queue is empty and the
        normal infinite fetch hasn't bought us new tracks. Seeds the
        watch_playlist from the currently/last-played track instead of the
        queue's tail (which has typically already produced a deduped
        response for radios with a fixed first batch). If even this
        comes back with nothing new, we accept the response as-is rather
        than silently stopping playback."""
        if self._is_fetching_infinite:
            return
        self._is_fetching_infinite = True

        seed_vid = self.current_video_id
        if not seed_vid and self.queue:
            seed_vid = self.queue[-1].get("videoId")
        if not seed_vid:
            self._is_fetching_infinite = False
            self.stop()
            self.current_queue_index = -1
            return

        def fetch():
            try:
                data = self.client.get_watch_playlist(
                    video_id=seed_vid, limit=50, radio=True
                )
                tracks = data.get("tracks", []) or []
                self._normalize_watch_playlist_tracks(tracks)
                existing = {t.get("videoId") for t in self.queue if t.get("videoId")}
                new_tracks = [
                    t for t in tracks
                    if t.get("videoId") and t.get("videoId") not in existing
                ]
                # If every result was a dupe, fall back to appending the
                # response anyway (skipping just the seed itself). Replaying
                # a few songs is a better outcome than silent stop.
                if not new_tracks and tracks:
                    new_tracks = [
                        t for t in tracks
                        if t.get("videoId") and t.get("videoId") != seed_vid
                    ]

                def _apply():
                    self._is_fetching_infinite = False
                    if not new_tracks:
                        self.stop()
                        self.current_queue_index = -1
                        self.emit("state-changed", "queue-updated")
                        return False
                    start_idx = len(self.queue)
                    self.extend_queue(new_tracks)
                    self.current_queue_index = start_idx
                    self._play_current_index()
                    self.emit("state-changed", "queue-updated")
                    return False

                GObject.idle_add(_apply)
            except Exception as e:
                print(f"[RADIO-EXTEND-FORCED] failed: {e}")

                def _give_up():
                    self._is_fetching_infinite = False
                    self.stop()
                    self.current_queue_index = -1
                    self.emit("state-changed", "queue-updated")
                    return False

                GObject.idle_add(_give_up)

        threading.Thread(target=fetch, daemon=True).start()

    def previous(self):
        # If > 5 seconds in, restart song
        try:
            pos = self.player.query_position(Gst.Format.TIME)[1]
            if pos > 5 * Gst.SECOND:
                self.seek(0)
                return
        except:
            pass

        if self.current_queue_index > 0:
            self.current_queue_index -= 1
            self._play_current_index()
        else:
            # Restart current if at 0 through the same guarded path used by
            # the seek bar, including the non-seekable-source fallback.
            self.seek(0)

    def shuffle_queue(self):
        if not self.shuffle_mode:
            # Enable Shuffle
            self.shuffle_mode = True
            if self.queue:
                current = (
                    self.queue[self.current_queue_index]
                    if self.current_queue_index >= 0
                    else None
                )

                # Shuffle the list
                remaining = [
                    t for i, t in enumerate(self.queue) if i != self.current_queue_index
                ]
                random.shuffle(remaining)

                if current:
                    self.queue = [current] + remaining
                    self.current_queue_index = 0
                else:
                    self.queue = remaining
                    self.current_queue_index = -1
        else:
            # Disable Shuffle (Restore original order)
            self.shuffle_mode = False
            # Try to find current track in original queue
            if self.current_queue_index >= 0 and self.current_queue_index < len(
                self.queue
            ):
                current = self.queue[self.current_queue_index]
                self.queue = list(self.original_queue)
                # Restore index
                try:
                    self.current_queue_index = self.queue.index(current)
                except ValueError:
                    self.current_queue_index = 0  # Fallback
            else:
                self.queue = list(self.original_queue)

        # Emit signal to update UI
        self.emit("state-changed", "queue-updated")

    def set_repeat_mode(self, mode):
        if mode in ["none", "track", "all"]:
            self.repeat_mode = mode
            self.emit("state-changed", "repeat-updated")
            if hasattr(self, "mpris_events"):
                self.mpris_events.on_options()

    def _normalize_track_metadata(self, track):
        """Resolve a queue entry's ytmusicapi-shaped fields into the
        normalized strings the UI / MPRIS / Discord all consume."""
        video_id = str(track.get("videoId") or "")
        title = str(track.get("title") or "Unknown")
        artist = track.get("artist", "")
        thumb = track.get("thumb")
        like_status = str(track.get("likeStatus") or "INDIFFERENT")

        if hasattr(self.client, "get_known_like_status") and hasattr(self.client, "set_known_like_status"):
            known = self.client.get_known_like_status(video_id)
            if known is not None:
                like_status = known
                track["likeStatus"] = known
            else:
                self.client.set_known_like_status(video_id, like_status)

        if not artist and track.get("artists"):
            artist = ", ".join(
                [str(a.get("name", "")) for a in track.get("artists") if a]
            )
        if isinstance(artist, list):
            artist = ", ".join([str(a.get("name", "")) for a in artist])
        artist = str(artist or "")

        if not thumb and track.get("thumbnails"):
            thumbs = track.get("thumbnails")
            if thumbs:
                thumb = thumbs[-1]["url"]
        thumb = str(thumb or "")
        if "ytimg.com" in thumb:
            thumb = get_high_res_url(thumb)

        track["artist"] = artist
        track["title"] = title
        track["thumb"] = thumb

        return video_id, title, artist, thumb, like_status

    def _play_current_index(self):
        if 0 <= self.current_queue_index < len(self.queue):
            track = self.queue[self.current_queue_index]
            video_id, title, artist, thumb, like_status = (
                self._normalize_track_metadata(track)
            )
            self._load_internal(video_id, title, artist, thumb, like_status)

    def _load_internal(
        self, video_id, title, artist, thumbnail_url, like_status="INDIFFERENT"
    ):
        self.current_video_id = video_id
        # Remember the videoId we were asked to play *before* any
        # OMV→ATV swap kicks in. Pages that show the original videoId
        # (album/playlist track rows) compare against this so the
        # currently-playing highlight still matches their row even
        # after the player swaps to the audio version.
        self._current_source_video_id = video_id

        # Claim the load generation before any idle callback or worker can
        # publish metadata.  Every asynchronous completion carries this
        # token and is ignored once a newer track has claimed the pipeline.
        with self._generation_lock:
            previous_generation = self.load_generation
            self.load_generation += 1
            current_gen = self.load_generation
            self._pending_gapless_index = None
            self._pending_gapless_generation = None
        self._pipeline_generation = None
        self._stream_started_generation = None
        self._pipeline_started_at = 0.0
        self._pipeline_null_confirmed.clear()
        self._cancel_staging_jobs("new track")
        self._cancel_precache("new track")
        # Do not let play() mistake the previous track's URI for this new
        # generation while yt-dlp/staging is still resolving it.
        self._current_play_uri = None
        self._current_play_uri_generation = None
        self._is_loading = True
        self._current_logical_state = "loading"
        self.emit("state-changed", "loading")
        self._start_position_timer()
        # The new stream restarts running-time, so anything still queued
        # from the previous track is now nonsense — drop it before the
        # next visualizer tick pulls.
        self._viz_queue.clear()
        # Any in-flight gapless plan was invalidated with the generation
        # claim above. Don't let a late stream-start apply the obsolete index.
        # A new track invalidates any parked seek-fallback target.
        self._seek_after_load = None
        self._pending_seek = None
        # The pipeline worker performs the NULL → URI → PLAYING sequence in
        # order.  Keeping the transition here would reintroduce the old
        # race with a late NULL transition from the previous track.
        # Keep the previous local fallback until the serialized pipeline
        # worker reaches NULL; a fast track change must not unlink a file
        # that playbin is still reading.
        if self._current_tmpfs_path:
            self._defer_tmpfs_cleanup(self._current_tmpfs_path)

        # Flush the old source immediately instead of leaving it audible
        # while URL resolution/staging runs in the background.  Mark the
        # command with the *previous* generation so its bookkeeping cannot
        # clear the seek/fallback state belonging to this new load.
        self._queue_pipeline_command("stop", previous_generation)
        self.current_video_id = video_id
        self.duration = -1
        self._last_emitted_position = -1.0
        self._last_emitted_duration = -1.0
        self._last_position_seconds = 0.0
        self._last_duration_seconds = 0.0
        self._next_duration_probe = 0.0
        self._buffering_since = 0.0
        self._last_progress_at = 0.0
        self._last_progress_position = 0
        self.last_seek_time = 0.0
        self._track_started_at = 0.0
        self.emit("progression", 0.0, 0.0)
        self._notify_scrobbler(
            video_id,
            title,
            artist,
            self.queue[self.current_queue_index]
            if 0 <= self.current_queue_index < len(self.queue)
            else None,
        )
        # New track → fresh history-record gate.
        if self._history_recorded_for != video_id:
            self._history_recorded_for = None
        # Immediate mode: record the play now, same as YT Music itself.
        if (
            self._history_mode == "immediate"
            and video_id
            and self._history_recorded_for != video_id
        ):
            self._history_recorded_for = video_id
            print(f"[HISTORY] immediate record for {video_id}")
            try:
                self.client.add_history_item_async(video_id)
            except Exception as e:
                print(f"[HISTORY] immediate record failed: {e}")

        GLib.idle_add(
            self._emit_metadata_if_current,
            current_gen,
            str(title),
            str(artist),
            str(thumbnail_url if thumbnail_url else ""),
            str(video_id),
            str(like_status),
        )

        # Trigger MPRIS art sync in background
        if thumbnail_url and hasattr(self, "mpris_events"):
            self._sync_mpris_art(thumbnail_url, video_id)

        GLib.idle_add(self._update_logical_state)

        if hasattr(self, "mpris_events"):
            try:
                self.mpris_events.on_player_all()
            except Exception as e:
                print(f"mpris ERROR: {e}")

        # Check for local download - instant offline playback, skip yt-dlp entirely
        local_path = self.download_manager.get_local_path(video_id)
        if local_path:
            print(f"[OFFLINE] Playing local file: {local_path}")
            file_uri = GLib.filename_to_uri(os.path.abspath(local_path), None)
            self._used_cached_url = False
            self._stream_debug = {
                "source": "local download",
                "video_id": video_id,
                "path": local_path,
            }
            GLib.idle_add(self._start_playback, file_uri, current_gen)
            return

        # Check stream URL cache - skip yt-dlp if we have a valid cached URL
        self._playing_from_cache = False
        self._pending_stream_url = None
        self._waiting_for_stream = False
        self._swap_seek_target = None
        self._used_cached_url = False
        self._fallback_stream_url = None
        self._pending_stream_cache = None
        self._cache_failed_waiting = False
        # Per-track retry counter. If a URL 503s mid-play we re-resolve
        # via yt-dlp (up to this many times) — googlevideo CDNs rotate
        # and a fresh extraction usually picks a healthier host.
        self._stream_retry_count = 0
        self._stream_retry_max = 2

        # Upload tracks (entityId set by ytmusicapi's library-upload surfaces)
        # always go through the tmpfs path in _fetch_and_play. Using the
        # cached stream URL here would start streaming first, then the tmpfs
        # download would restart playback when it finished — visible to the
        # user as "the song jumped back to 0 when I tried to seek".
        cur_track = (
            self.queue[self.current_queue_index]
            if 0 <= self.current_queue_index < len(self.queue)
            else {}
        )
        is_upload = bool(cur_track.get("entityId"))
        # If the queued track is a music video (OMV/UGC), `_fetch_and_play`
        # will swap to the audio (ATV) version before yt-dlp resolves —
        # skip the early cache check here because it would key on the
        # OMV videoId and start playing the video stream before the swap
        # can happen.
        vtype_upper = (cur_track.get("videoType") or "").upper()
        will_swap = (
            vtype_upper.startswith("MUSIC_VIDEO_TYPE_")
            and vtype_upper != "MUSIC_VIDEO_TYPE_ATV"
        )

        # Known non-seekable streams go straight to the tmpfs-download path
        # (handled in _fetch_and_play, same as uploads). Skip the early cache
        # stream so we don't briefly play the unseekable URL before swapping.
        is_noseek = video_id in self._noseek_vids

        if not is_upload and not will_swap and not is_noseek:
            cached_url = self.stream_cache.get(video_id)
            if cached_url:
                print(f"[CACHE] Using cached stream URL for {video_id}")
                self._used_cached_url = True
                self._stream_debug = {
                    "source": "stream (cached URL)",
                    "video_id": video_id,
                }
                GLib.idle_add(self._start_playback, cached_url, current_gen)

        thread = threading.Thread(
            target=self._fetch_and_play,
            args=(video_id, title, artist, thumbnail_url, like_status, current_gen),
        )
        thread.daemon = True
        thread.start()

        # Eagerly precache *only* the immediate next track — that's the
        # one the user is most likely to skip to. yt-dlp does heavy
        # Python-side work (JSON parsing, JS interpretation) under the
        # GIL; precaching 6 neighbours up front demonstrably starves the
        # GTK main loop while the user opens a playlist or scrolls. The
        # remaining neighbours are still pre-cached, but only after the
        # current track's _fetch_and_play completes (see the trailing
        # _precache_next call inside that method).
        if self._precache_enabled:
            threading.Thread(
                target=self._precache_next,
                args=(current_gen,),
                kwargs={"max_count": 1},
                daemon=True,
            ).start()

    def extend_queue(self, tracks):
        """Appends new tracks to the queue (and original_queue)."""
        if not tracks:
            return

        # Append to original queue always
        self.original_queue.extend(tracks)

        if self.shuffle_mode:
            # Smart Shuffle: Mix new tracks with UPCOMING tracks
            # We don't want to touch history or current song.

            current_idx = self.current_queue_index

            # Assume valid index; fallback handling can be added if needed.
            if 0 <= current_idx < len(self.queue):
                history_and_current = self.queue[: current_idx + 1]
                upcoming = self.queue[current_idx + 1 :]

                combined = upcoming + tracks
                import random

                random.shuffle(combined)

                self.queue = history_and_current + combined
                # current_queue_index stays same
            else:
                # Queue empty or invalid index, just shuffle all
                self.queue.extend(tracks)
                import random

                random.shuffle(self.queue)
                # If we were playing, index might be -1.
                # If we were stopped, index -1.

                if self.current_queue_index == -1 and self.queue:
                    self.current_queue_index = 0

        else:
            self.queue.extend(tracks)

        self.emit("state-changed", "queue-updated")

    def update_track_thumbnail(self, video_id, working_url):
        """
        Updates the thumbnail URL for a track if a better/working one is found.
        This is called by UI components (AsyncPicture/AsyncImage) when they
        successfully resolve a fallback URL.
        """
        if not video_id or not working_url:
            return

        def _same_cover(a, b):
            # Treat ytimg thumbnails of the same video as the same cover even
            # when they differ in quality (maxresdefault vs sddefault, ...).
            # Otherwise the UI (which upgrades to maxresdefault) and the MPRIS
            # art job (which downgrades to whatever actually fetched) keep
            # overwriting each other's URL, and each flip re-emits
            # metadata-changed — an infinite loop that also re-triggers a
            # lyrics fetch every iteration (LRCLIB 429 storm).
            if a == b:
                return True
            import re

            ma = re.search(r"i\.ytimg\.com/vi/([^/]+)/", a or "")
            mb = re.search(r"i\.ytimg\.com/vi/([^/]+)/", b or "")
            return bool(ma and mb and ma.group(1) == mb.group(1))

        changed = False
        # Update in current queue
        for track in self.queue:
            if track.get("videoId") == video_id:
                if not _same_cover(track.get("thumb"), working_url):
                    track["thumb"] = working_url
                    changed = True

        # Update in original queue
        for track in self.original_queue:
            if track.get("videoId") == video_id:
                if not _same_cover(track.get("thumb"), working_url):
                    track["thumb"] = working_url

        if changed:
            # If this is the currently playing track, re-emit metadata to update MPRIS
            current_track = (
                self.queue[self.current_queue_index]
                if 0 <= self.current_queue_index < len(self.queue)
                else None
            )
            if current_track and current_track.get("videoId") == video_id:
                print(
                    f"[PLAYER] Updating working thumbnail for {video_id}: {working_url}"
                )
                # Re-emit metadata changed to trigger MPRIS update
                self.emit(
                    "metadata-changed",
                    current_track.get("title", ""),
                    current_track.get("artist", ""),
                    working_url,
                    video_id,
                    current_track.get("likeStatus", "INDIFFERENT"),
                )
                self._sync_mpris_art(working_url, video_id)

    def _start_infinite_fetch(self):
        self._is_fetching_infinite = True
        limit = 50

        last_video_id = None
        if self.queue:
            last_video_id = self.queue[-1].get("videoId")
        playlist_id = self.queue_source_id
        # Sources stamped by play_then_radio aren't real playlist IDs (they
        # use a "home-radio:…" prefix as a queue-identity stamp). Don't pass
        # them as playlist_id — that confuses watch_playlist. The video seed
        # is enough to get a fresh radio.
        if playlist_id and ":" in playlist_id:
            playlist_id = None

        def fetch_job():
            try:
                data = self.client.get_watch_playlist(
                    video_id=last_video_id,
                    playlist_id=playlist_id,
                    limit=limit,
                    radio=True,
                )
                tracks = data.get("tracks", []) or []
                self._normalize_watch_playlist_tracks(tracks)
                existing_ids = {
                    t.get("videoId") for t in self.queue if t.get("videoId")
                }
                new_tracks = [
                    t for t in tracks
                    if t.get("videoId") and t.get("videoId") not in existing_ids
                ]

                # Dedup-rescue: radios with a deterministic first batch will
                # return the same tracks for the same seed, so the first
                # `existing_ids` filter wipes the entire response. Retry
                # once with the currently-playing track as the seed (which
                # is typically a different position in the radio than the
                # queue's tail).
                if (
                    not new_tracks
                    and self.current_video_id
                    and self.current_video_id != last_video_id
                ):
                    retry = self.client.get_watch_playlist(
                        video_id=self.current_video_id,
                        limit=limit,
                        radio=True,
                    )
                    retry_tracks = retry.get("tracks", []) or []
                    self._normalize_watch_playlist_tracks(retry_tracks)
                    new_tracks = [
                        t for t in retry_tracks
                        if t.get("videoId") and t.get("videoId") not in existing_ids
                    ]

                if new_tracks:
                    GObject.idle_add(self._on_infinite_fetch_complete, new_tracks)
                else:
                    self._is_fetching_infinite = False
            except Exception as e:
                print(f"Error fetching infinite queue: {e}")
                self._is_fetching_infinite = False

        thread = threading.Thread(target=fetch_job)
        thread.daemon = True
        thread.start()

    def _on_infinite_fetch_complete(self, new_tracks):
        self.extend_queue(new_tracks)
        self._is_fetching_infinite = False

    def _create_cookie_file(self, headers):
        """Creates a temporary Netscape format cookie file from headers."""
        import tempfile
        import time

        cookie_str = headers.get("Cookie", "")
        if not cookie_str:
            return None

        # Netscape format requires specific tab-separated columns
        fd, path = tempfile.mkstemp(suffix=".txt", text=True)
        with os.fdopen(fd, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")

            now = int(time.time()) + 3600 * 24 * 365  # 1 year validity

            parts = cookie_str.split(";")
            for part in parts:
                if "=" in part:
                    # Handle potential whitespace around parts
                    pair = part.strip().split("=", 1)
                    if len(pair) != 2:
                        continue
                    key, value = pair

                    # Use .youtube.com for everything - proven effective for locking tracks in sweep
                    f.write(f".youtube.com\tTRUE\t/\tTRUE\t{now}\t{key}\t{value}\n")

        return path

    def _tmpfs_root(self, low_power=None):
        """Pick storage for seek fallback buffers.

        Normal mode uses /dev/shm for fast local playback.  Low-power mode
        deliberately uses the disk cache instead: a few hundred MB of
        upload-locker audio must not count as application RAM on a small
        device.  The directory name is unchanged so cleanup remains safe.
        ``low_power`` is optional so shutdown can sweep both storage roots
        even after the effective mode has changed.
        """
        import tempfile

        if low_power is None:
            low_power = self._low_power_mode
        if low_power:
            disk_root = os.path.join(
                GLib.get_user_cache_dir(), "ventapes", "stream-fallback"
            )
            try:
                os.makedirs(disk_root, exist_ok=True)
                return disk_root
            except OSError:
                pass

        candidates = ["/dev/shm"]
        getuid = getattr(os, "getuid", None)
        if getuid is not None:
            try:
                candidates.append("/run/user/{}".format(getuid()))
            except (OSError, TypeError, ValueError):
                pass
        for candidate in candidates:
            if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
                return candidate
        return tempfile.gettempdir()

    def _staging_roots(self):
        """Return both possible staging roots, de-duplicated by real path."""

        roots = []
        seen = set()
        for low_power in (False, True):
            try:
                root = self._tmpfs_root(low_power=low_power)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            real = os.path.realpath(os.path.abspath(root))
            if real not in seen:
                seen.add(real)
                roots.append(root)
        return roots

    def _get_staging_budget_lock_path(self):
        """Return the cross-process lock guarding aggregate reservations."""

        if self._staging_budget_lock_path:
            return self._staging_budget_lock_path
        try:
            base = GLib.get_user_cache_dir()
        except Exception:
            base = None
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".cache")
        self._staging_budget_lock_path = os.path.join(
            base, "ventapes", "staging-budget.lock"
        )
        return self._staging_budget_lock_path

    def _staging_keep_paths(self):
        """Paths that a stale-directory sweep must not remove."""

        keep = []
        with self._staging_lock:
            for job in self._staging_jobs.values():
                if job.get("path"):
                    keep.append(job["path"])
            keep.extend(self._pending_tmpfs_cleanups)
            keep.extend(self._staging_paths)
            keep.extend(self._staging_ready_paths)
            current = self._current_tmpfs_path
        if current:
            keep.append(os.path.dirname(current))
        return keep

    def _sweep_staging_roots(self):
        """Remove abandoned staging directories from RAM and disk roots."""

        keep = self._staging_keep_paths()
        removed = 0
        for root in self._staging_roots():
            removed += sweep_staging_dirs(root, keep=keep)
        return removed

    def _staging_used_bytes(self):
        """Return bytes currently occupied by our staging directories."""

        total = 0
        for root in self._staging_roots():
            try:
                entries = list(os.scandir(root))
            except OSError:
                continue
            for entry in entries:
                if is_staging_dir(entry.path, (root,)):
                    total += directory_size(entry.path)
        return total

    def _staging_cancelled(self, job):
        if self._pipeline_shutdown:
            return True
        if job["cancel"].is_set():
            return True
        with self._generation_lock:
            current_generation = self.load_generation
        if job["generation"] != current_generation:
            job["cancel"].set()
            return True
        return False

    def _release_staging_lease(self, path):
        with self._staging_lock:
            lease = self._staging_leases.pop(path, None)
            self._staging_ready_paths.pop(path, None)
        if lease is not None:
            lease.close()

    def _transfer_staging_lease(self, job, path):
        """Move a completed job's directory lease to the ready-path map."""

        with self._staging_lock:
            lease = job.get("lease")
            if lease is not None:
                self._staging_leases[path] = lease
                job["lease"] = None
                self._staging_ready_paths[path] = time.monotonic()
                refresh_staging_lease(lease)

    def _register_staging_job(self, video_id, generation):
        """Claim the single staging slot, or return ``None`` if busy."""

        with self._staging_lock:
            if (
                self._pipeline_shutdown
                or self._staging_active
                or generation != self.load_generation
            ):
                return None
            job_id = object()
            job = {
                "id": job_id,
                "video_id": video_id,
                "generation": generation,
                "cancel": threading.Event(),
                "done": threading.Event(),
                "path": None,
                "root": None,
                "ydl": None,
                "lease": None,
                "budget_lease": None,
                "thread": threading.current_thread(),
                "last_size_check": 0.0,
            }
            self._staging_jobs[job_id] = job
            self._staging_active = True
            return job

    def _finish_staging_job(self, job):
        with self._staging_lock:
            if self._staging_jobs.get(job["id"]) is job:
                self._staging_jobs.pop(job["id"], None)
                self._staging_active = bool(self._staging_jobs)

    def _cancel_staging_jobs(self, reason="cancelled", wait=False, timeout=0.25):
        """Ask in-flight yt-dlp jobs to stop and return their records.

        Progress hooks provide the hard cancellation point.  Closing the
        downloader as well makes a blocked HTTP read unwind on runtimes that
        expose ``YoutubeDL.close()``; the short wait is opt-in so a GTK
        callback never blocks on network teardown.
        """

        with self._staging_lock:
            jobs = list(self._staging_jobs.values())
            downloaders = [job.get("ydl") for job in jobs]
        for job in jobs:
            if not job["cancel"].is_set():
                print(f"[STAGING] cancelling {job['video_id']} ({reason})")
                job["cancel"].set()
        for downloader in downloaders:
            if downloader is None:
                continue
            try:
                close = getattr(downloader, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
        if wait:
            deadline = time.monotonic() + max(0.0, timeout)
            for job in jobs:
                remaining = max(0.0, deadline - time.monotonic())
                if not job["done"].wait(remaining):
                    break
        return jobs

    def _download_upload_to_tmpfs(self, video_id, generation):
        """Download one bounded local playback file with yt-dlp.

        Upload-locker tracks and manifest-only streams use this path when a
        remote URL cannot be seeked reliably.  A single staging slot keeps
        concurrent downloads from multiplying memory/disk use; a progress
        hook enforces both cancellation and a hard byte budget.  Returning
        ``None`` is intentionally recoverable: callers fall back to normal
        streaming when staging is busy, stale, or too large.
        """
        import tempfile

        if (
            not video_id
            or self._pipeline_shutdown
            or generation != self.load_generation
        ):
            return None

        # Sweep directories left by a crash or a previous process before
        # charging their bytes to this job's budget.  The budget lease below
        # serializes the observe-and-create window across application
        # processes; without it two instances could each see the same free
        # baseline and jointly exceed the aggregate cap.
        self._sweep_staging_roots()
        budget_lease = acquire_staging_lock(
            self._get_staging_budget_lock_path(), blocking=False
        )
        if budget_lease is None:
            # Another instance is actively staging.  Falling back to the
            # ordinary stream is safer than multiplying a large temporary
            # file, and the caller already has that recovery path.
            return None
        job = self._register_staging_job(video_id, generation)
        if job is None:
            # A just-skipped upload may still be unwinding its cancellation.
            # Give that worker a short, non-UI-thread window to release the
            # single staging slot instead of immediately falling back to an
            # unreliable remote stream.
            deadline = time.monotonic() + 0.5
            while job is None and generation == self.load_generation:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
                job = self._register_staging_job(video_id, generation)
            if job is None:
                budget_lease.close()
                return None

        job["budget_lease"] = budget_lease
        tmp_dir = None
        cookie_file = None
        result_path = None
        download_result = None
        keep_dir = False
        try:
            staging_root = self._tmpfs_root()
            tmp_dir = tempfile.mkdtemp(
                prefix=STAGING_DIR_PREFIX,
                dir=staging_root,
            )
            dir_lease = acquire_staging_lease(tmp_dir, blocking=False)
            if dir_lease is None:
                raise StagingLimitExceeded("could not claim staging directory")
            job["lease"] = dir_lease
            with self._staging_lock:
                job["path"] = tmp_dir
                self._staging_paths.add(tmp_dir)
                job["root"] = staging_root
                base_bytes = self._staging_used_bytes() - directory_size(tmp_dir)
                available = self._staging_limit_bytes - max(0, base_bytes)
                try:
                    free_bytes = shutil.disk_usage(job["root"]).free
                    # Keep a small reserve for the filesystem itself rather
                    # than consuming the final blocks with a staging file.
                    available = min(
                        available,
                        max(0, free_bytes - 4 * 1024 * 1024),
                    )
                except OSError:
                    pass
                job["base_bytes"] = max(0, base_bytes)
                job["budget"] = max(0, available)
            if job["budget"] <= 0:
                raise StagingLimitExceeded("staging budget or free space is exhausted")

            outtmpl = os.path.join(tmp_dir, "audio.%(ext)s")
            opts = self.ydl_opts.copy()
            opts["outtmpl"] = outtmpl
            opts["quiet"] = True
            opts["noprogress"] = True
            opts["writethumbnail"] = False
            opts["writeinfojson"] = False
            # This is a backstop for formats that report their size before
            # the first progress callback.  The progress hook below remains
            # authoritative because downloaded_bytes is available even when
            # total size is unknown.
            opts["max_filesize"] = max(1, job["budget"])
            opts["socket_timeout"] = 20
            opts["match_filter"] = self._compose_match_filter(
                self.ydl_opts.get("match_filter"),
                self._staging_match_filter(job),
            )
            existing_progress = self.ydl_opts.get("progress_hooks") or []
            if callable(existing_progress):
                existing_progress = [existing_progress]
            opts["progress_hooks"] = list(existing_progress) + [
                self._staging_progress_hook(job)
            ]
            existing_post = self.ydl_opts.get("postprocessor_hooks") or []
            if callable(existing_post):
                existing_post = [existing_post]
            opts["postprocessor_hooks"] = list(existing_post) + [
                self._staging_progress_hook(job, postprocessor=True)
            ]

            if self.client.is_authenticated() and self.client.api:
                request_headers = dict(self.client.api.headers or {})
                cookie_file = self._create_cookie_file(request_headers)
                if cookie_file:
                    opts["cookiefile"] = cookie_file
                ua = request_headers.get("User-Agent")
                if ua:
                    opts["user_agent"] = ua
                # yt-dlp's HTTP source does not inherit the API client's
                # Authorization/X-Goog headers automatically.  Copy the
                # non-cookie headers so private upload/media URLs work on
                # the staging path as they do in the normal resolver.
                http_headers = dict(opts.get("http_headers") or {})
                http_headers.update({
                    str(key): str(value)
                    for key, value in request_headers.items()
                    if (
                        str(key).lower() != "cookie" or not cookie_file
                    ) and value is not None
                })
                opts["http_headers"] = http_headers

            if self._staging_cancelled(job):
                raise StagingCancelled()
            url = f"https://music.youtube.com/watch?v={video_id}"
            with YoutubeDL(opts) as ydl:
                with self._staging_lock:
                    job["ydl"] = ydl
                if self._staging_cancelled(job):
                    try:
                        ydl.close()
                    except Exception:
                        pass
                    raise StagingCancelled()
                try:
                    download_result = ydl.download([url])
                finally:
                    with self._staging_lock:
                        if job.get("ydl") is ydl:
                            job["ydl"] = None

            if self._staging_cancelled(job):
                raise StagingCancelled("staging job was superseded")

            expected_name = _yt_dlp_final_filename(download_result)
            result_path = find_completed_audio(
                tmp_dir,
                expected_name=os.path.basename(expected_name) if expected_name else None,
            )
            if result_path is None:
                return None
            if directory_size(tmp_dir) > job["budget"]:
                raise StagingLimitExceeded("staged file exceeded its budget")
            keep_dir = True
            return result_path
        except StagingCancelled:
            return None
        except StagingLimitExceeded as exc:
            print(f"[STAGING] bounded download stopped for {video_id}: {exc}")
            return None
        except Exception as exc:
            print(f"[PLAYER] tmpfs download error for {video_id}: {exc}")
            return None
        finally:
            if cookie_file:
                try:
                    os.remove(cookie_file)
                except OSError:
                    pass
            if keep_dir and tmp_dir:
                # Keep the lease while the completed file waits for the GTK
                # handoff.  _adopt_staged_path() transfers that ownership to
                # the current playback path.
                self._transfer_staging_lease(job, tmp_dir)
            elif tmp_dir:
                # The directory lease belongs to this job, not to a live
                # playback source.  Release it before asking the ownership-
                # checked remover to delete the directory; a failed removal
                # is re-leased below so another process cannot race us.
                lease = job.get("lease")
                job["lease"] = None
                if lease is not None:
                    lease.close()
                if not self._remove_owned_staging_dir(tmp_dir):
                    retained = acquire_staging_lease(tmp_dir, blocking=False)
                    if retained is not None:
                        with self._staging_lock:
                            self._staging_leases[tmp_dir] = retained
                            self._staging_ready_paths.setdefault(
                                tmp_dir, time.monotonic()
                            )
            budget_lease = job.get("budget_lease")
            job["budget_lease"] = None
            if budget_lease is not None:
                budget_lease.close()
            self._finish_staging_job(job)
            job["done"].set()

    def _staging_progress_hook(self, job, postprocessor=False):
        """Build a yt-dlp progress callback tied to one staging job."""

        def _hook(status):
            if self._staging_cancelled(job):
                raise StagingCancelled()
            refresh_staging_lease(job.get("lease"))
            if not isinstance(status, dict):
                return status

            if reported_size_exceeds(status, job.get("budget", 0)):
                raise StagingLimitExceeded("reported download size exceeds budget")

            # Progress metadata can be absent for fragmented downloads, and
            # postprocessors can create additional temporary files.  Check
            # the actual directory periodically, plus whenever we are close
            # to the hard limit, so those paths cannot evade the cap.
            now = time.monotonic()
            try:
                downloaded = int(status.get("downloaded_bytes") or 0)
            except (TypeError, ValueError):
                downloaded = 0
            if (
                postprocessor
                or downloaded >= job.get("budget", 0) * 0.9
                or now - job.get("last_size_check", 0.0) >= 0.25
            ):
                actual = directory_size(job.get("path"))
                if actual > job.get("budget", 0):
                    raise StagingLimitExceeded("staging directory exceeded budget")
                job["last_size_check"] = now

            root = job.get("root")
            if root:
                try:
                    if shutil.disk_usage(root).free < 4 * 1024 * 1024:
                        raise StagingLimitExceeded("staging filesystem is full")
                except OSError:
                    pass
            return status

        return _hook

    @staticmethod
    def _compose_match_filter(existing, added):
        """Preserve a caller-supplied format filter before ours."""

        if not existing:
            return added
        filters = (
            list(existing)
            if isinstance(existing, (list, tuple))
            else [existing]
        )

        def _filter(info, **kwargs):
            for filter_fn in filters:
                result = filter_fn(info, **kwargs)
                if result is not None:
                    return result
            return added(info, **kwargs)

        return _filter

    @staticmethod
    def _staging_match_filter(job):
        """Reject an oversized format before yt-dlp starts downloading it."""

        def _filter(info, **_kwargs):
            size = None
            for key in ("filesize", "filesize_approx"):
                value = info.get(key) if isinstance(info, dict) else None
                try:
                    if value is not None:
                        size = int(value)
                        break
                except (TypeError, ValueError):
                    continue
            if size is not None and size > job.get("budget", 0):
                raise StagingLimitExceeded("format is larger than staging budget")
            return None

        return _filter

    @staticmethod
    def _rm_tmpfs_dir(path, roots=None):
        """Compatibility wrapper for the historical private helper.

        Older integrations called ``Player._rm_tmpfs_dir(path)`` as a
        class/static helper.  Keep that call shape best-effort; all current
        in-tree cleanup goes through ``_remove_owned_staging_dir`` so an
        untrusted path cannot bypass the root/lease checks.
        """

        if not path:
            return False
        try:
            if staging_lease_state(path) == "held":
                return False
            if roots is None:
                shutil.rmtree(path, ignore_errors=False)
                return True
            return remove_staging_dir(path, roots)
        except OSError:
            return not os.path.lexists(path)

    def _remove_owned_staging_dir(self, path):
        # Release this process's lease before the ownership check.  A lease
        # held by another process remains held and makes removal fail safely.
        self._release_staging_lease(path)
        roots = self._staging_roots()
        try:
            removed = remove_staging_dir(path, roots)
        except Exception:
            removed = False
        confirmed = removed or not os.path.lexists(path)
        if confirmed:
            with self._staging_lock:
                self._staging_paths.discard(path)
                self._staging_ready_paths.pop(path, None)
        elif os.path.lexists(path):
            # Keep ownership if removal failed so a later retry cannot be
            # mistaken for abandoned work by another process.
            retained = acquire_staging_lease(path, blocking=False)
            if retained is not None:
                with self._staging_lock:
                    self._staging_leases[path] = retained
                    self._staging_ready_paths.setdefault(
                        path, time.monotonic()
                    )
        return confirmed

    def _cleanup_tmpfs_path(self, path):
        """Remove a single staging file and its owned parent directory."""

        if not path:
            return False
        parent = os.path.dirname(path)
        confirmed = False
        if parent and is_staging_dir(parent, self._staging_roots()):
            confirmed = self._remove_owned_staging_dir(parent)
        elif not os.path.lexists(path):
            confirmed = True
        elif os.path.isfile(path) and not os.path.islink(path):
            # Preserve cleanup for paths produced by the pre-staging helper;
            # current new paths always take the directory branch above.
            try:
                os.remove(path)
                confirmed = True
            except OSError as exc:
                print(f"[PLAYER] tmpfs cleanup failed for {path}: {exc}")
        if confirmed:
            with self._staging_lock:
                self._staging_paths.discard(path)
                self._staging_paths.discard(parent)
                self._staging_ready_paths.pop(parent, None)
        return confirmed

    def _cleanup_all_tmpfs(self):
        """Best-effort process-exit cleanup for staged local sources.

        ``atexit`` can run without ``shutdown()`` (or while the pipeline
        worker is still stopping), so a current source is only removed after
        a confirmed NULL transition.  Ready-but-unclaimed paths are kept
        until that confirmation; their leases are released when the process
        exits and the next startup sweep can reclaim them safely.
        """

        jobs = self._cancel_staging_jobs("shutdown", wait=True, timeout=0.5)
        null_confirmed = self._pipeline_null_confirmed.is_set()
        current = getattr(self, "_current_tmpfs_path", None)
        if current and null_confirmed:
            if self._cleanup_tmpfs_path(current):
                with self._staging_lock:
                    if self._current_tmpfs_path == current:
                        self._current_tmpfs_path = None

        if null_confirmed:
            try:
                with self._staging_lock:
                    pending = list(self._pending_tmpfs_cleanups)
                    self._pending_tmpfs_cleanups.clear()
                for path in pending:
                    self._cleanup_tmpfs_path(path)
            except Exception:
                pass

        try:
            with self._staging_lock:
                owned = list(self._staging_paths)
                ready = dict(self._staging_ready_paths)
                active_paths = {
                    job.get("path")
                    for job in jobs
                    if job.get("path") and not job["done"].is_set()
                }
            for path in owned:
                if path in active_paths:
                    continue
                if not null_confirmed:
                    # Until the serialized worker has confirmed NULL, every
                    # tracked directory may still back the active/deferred
                    # source.  The old path==current check compared a
                    # directory with a file path and could unlink live audio.
                    # Keep the whole set for the next handoff/cleanup pass.
                    continue
                ready_at = ready.get(path)
                if ready_at is not None and not null_confirmed:
                    # The download worker has finished, but the GTK handoff
                    # callback may be next in the loop.  Without a confirmed
                    # NULL, leave every ready path for the next startup
                    # sweep rather than guessing that the handoff is stale.
                    continue
                self._remove_owned_staging_dir(path)
            keep = self._staging_keep_paths()
            # A downloader that ignored cancellation is still allowed to
            # finish; never unlink its directory underneath an open handle.
            keep.extend(active_paths)
            for root in self._staging_roots():
                sweep_staging_dirs(root, keep=keep)
        except Exception:
            # atexit runs while GLib may already be shutting down; cleanup is
            # best effort and must never mask the process exit.
            pass

    def _defer_tmpfs_cleanup(self, path=None):
        """Release a local fallback after the pipeline reaches NULL."""

        if not path:
            return
        with self._staging_lock:
            if path == self._current_tmpfs_path:
                self._current_tmpfs_path = None
            if path not in self._pending_tmpfs_cleanups:
                self._pending_tmpfs_cleanups.append(path)

    def _cleanup_pending_tmpfs(self):
        with self._staging_lock:
            paths = list(self._pending_tmpfs_cleanups)
            self._pending_tmpfs_cleanups.clear()
        remaining = []
        for path in paths:
            self._cleanup_tmpfs_path(path)
            parent = os.path.dirname(path)
            if os.path.lexists(path) or (parent and os.path.lexists(parent)):
                remaining.append(path)
        if remaining:
            with self._staging_lock:
                for path in remaining:
                    if path not in self._pending_tmpfs_cleanups:
                        self._pending_tmpfs_cleanups.append(path)

    def _adopt_staged_path(self, path, video_id, generation, source="local staged"):
        """Adopt a completed download on the GTK thread or discard it."""

        if not path or self._pipeline_shutdown:
            self._cleanup_tmpfs_path(path)
            return False
        with self._generation_lock:
            current_generation = self.load_generation
        if generation != current_generation:
            self._cleanup_tmpfs_path(path)
            return False
        if self.current_video_id != video_id or not os.path.isfile(path):
            self._cleanup_tmpfs_path(path)
            return False
        with self._staging_lock:
            previous = self._current_tmpfs_path
            self._current_tmpfs_path = path
            self._staging_ready_paths.pop(path, None)
        if previous and previous != path:
            self._defer_tmpfs_cleanup(previous)
        try:
            file_uri = GLib.filename_to_uri(os.path.abspath(path), None)
        except Exception:
            if self._cleanup_tmpfs_path(path):
                with self._staging_lock:
                    if self._current_tmpfs_path == path:
                        self._current_tmpfs_path = None
            return False
        self._stream_debug = {
            "source": source,
            "video_id": video_id,
            "path": path,
        }
        try:
            source_id = GObject.idle_add(
                self._start_playback, file_uri, generation
            )
            if not source_id:
                raise RuntimeError("could not schedule staged playback")
        except Exception:
            if self._cleanup_tmpfs_path(path):
                with self._staging_lock:
                    if self._current_tmpfs_path == path:
                        self._current_tmpfs_path = None
            return False
        return False

    def _queue_staged_playback(self, path, video_id, generation, source):
        try:
            source_id = GObject.idle_add(
                self._adopt_staged_path, path, video_id, generation, source
            )
            if not source_id:
                self._cleanup_tmpfs_path(path)
            return source_id
        except Exception:
            self._cleanup_tmpfs_path(path)
            return False

    def _noseek_vids_path(self):
        return os.path.join(
            GLib.get_user_data_dir(), "ventapes", "noseek_vids.json"
        )

    def _load_noseek_vids(self):
        try:
            path = self._noseek_vids_path()
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._noseek_vids = set(data)
        except Exception:
            pass

    def _save_noseek_vids(self):
        try:
            path = self._noseek_vids_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                # Cap the file so a long-lived install can't grow it forever.
                json.dump(list(self._noseek_vids)[-500:], f)
        except Exception as e:
            print(f"[PLAYER] failed to persist noseek list: {e}")

    def _mark_noseek(self, video_id):
        """Remember that `video_id` can't be seeked while streamed, so future
        plays go straight to the tmpfs-download path."""
        if video_id and video_id not in self._noseek_vids:
            self._noseek_vids.add(video_id)
            self._save_noseek_vids()

    def _fetch_and_play(
        self,
        video_id,
        title_hint,
        artist_hint,
        thumb_hint,
        like_status_hint,
        generation,
    ):
        if generation != self.load_generation:
            print(
                f"Stale load generation {generation} (current {self.load_generation}). Aborting."
            )
            return
        import os

        # If the queued track is YT Music's music-video version of a song,
        # swap to the audio (ATV) version BEFORE yt-dlp resolves so we
        # stream the cleaner album audio and get clean metadata. We do
        # this in the worker thread (not in _load_internal) because the
        # lookup is a network round-trip and shouldn't block the UI.
        # find_audio_version() short-circuits cheaply when the source is
        # already ATV (returns None), so we can call it unconditionally
        # — needed because YT Music's album/single endpoint doesn't
        # always populate `videoType`, and gating on it here meant the
        # swap never fired for those cases. Caching the result on the
        # track ensures we only pay the API cost on first play.
        track = (
            self.queue[self.current_queue_index]
            if 0 <= self.current_queue_index < len(self.queue)
            else {}
        )
        already_checked = track.get("_swap_checked")
        if (
            not already_checked
            and track.get("videoId") == video_id
            and not track.get("entityId")  # uploads have no counterpart
        ):
            try:
                swap_info = self.client.find_audio_version(video_id)
            except Exception as e:
                print(f"[swap-version] lookup failed: {e}")
                swap_info = None
            if generation != self.load_generation:
                return
            swapped = (
                swap_info.get("videoId") if isinstance(swap_info, dict) else None
            )
            # Only memoize success — a transient API failure shouldn't
            # permanently pin this track to the music-video version.
            if swapped:
                track["_swap_checked"] = True
            if swapped and swapped != video_id:
                print(
                    f"[swap-version] {video_id} → {swapped} ({track.get('title')})"
                )
                track["videoId"] = swapped
                track["videoType"] = "MUSIC_VIDEO_TYPE_ATV"
                # Pull the album-cover thumbnail from the swap result so
                # the player bar / queue / MPRIS art stop showing the
                # music-video still. Upgrade ytimg URLs to the same
                # high-res form the rest of the player uses.
                new_thumb = swap_info.get("thumb") or ""
                if new_thumb and "ytimg.com" in new_thumb:
                    new_thumb = get_high_res_url(new_thumb) or new_thumb
                if new_thumb:
                    track["thumb"] = new_thumb
                    thumb_hint = new_thumb
                new_title = swap_info.get("title")
                if new_title:
                    track["title"] = new_title
                    title_hint = new_title
                new_artists = swap_info.get("artists")
                if isinstance(new_artists, list) and new_artists:
                    first = new_artists[0]
                    new_artist_name = (
                        first.get("name", "") if isinstance(first, dict) else str(first)
                    )
                    if new_artist_name:
                        track["artist"] = new_artist_name
                        artist_hint = new_artist_name
                video_id = swapped
                self.current_video_id = swapped
                # Scrobble the audio version's title, not the music
                # video's. This is the same play, so the clock keeps
                # running.
                if getattr(self, "scrobbler", None):
                    self.scrobbler.refine_current_track(
                        swapped, title_hint, artist_hint
                    )
                GObject.idle_add(
                    self._emit_metadata_if_current,
                    generation,
                    str(title_hint),
                    str(artist_hint),
                    str(thumb_hint or ""),
                    str(swapped),
                    str(like_status_hint),
                )

        # Upload-locker tracks: YT's range-request handling on those URLs
        # silently breaks seeking even with cookies attached. Sidestep the
        # whole streaming pipeline by downloading the file into tmpfs
        # (/dev/shm — RAM-backed on Linux) and playing from there. The
        # local file is deleted as soon as the user moves to a new track,
        # so nothing accumulates on disk.
        # Upload-locker tracks always need this; tracks previously detected as
        # non-seekable (m4a-only with no usable seek index) get the same
        # treatment so the user can scrub them.
        if track.get("entityId") or video_id in self._noseek_vids:
            tmpfs_path = self._download_upload_to_tmpfs(video_id, generation)
            if generation != self.load_generation:
                # The download can finish in the small window after its
                # generation check.  Callers own the returned path, so clean
                # it here rather than leaving a completed orphan staged.
                self._cleanup_tmpfs_path(tmpfs_path)
                return
            if tmpfs_path:
                self._used_cached_url = False
                source = (
                    "local staged (non-seekable stream fallback)"
                    if video_id in self._noseek_vids and not track.get("entityId")
                    else "local staged (upload)"
                )
                final_title = title_hint or track.get("title") or "Unknown"
                final_artist = artist_hint or track.get("artist") or "Unknown"
                final_thumb = thumb_hint or track.get("thumb") or ""
                GObject.idle_add(
                    self._emit_metadata_if_current,
                    generation,
                    final_title,
                    final_artist,
                    final_thumb,
                    video_id,
                    like_status_hint,
                )
                self._queue_staged_playback(
                    tmpfs_path, video_id, generation, source
                )
                return
            # tmpfs download failed — fall through to the normal streaming
            # path. Seek won't work, but at least playback won't be blocked.
            print(f"[PLAYER] tmpfs download failed for upload {video_id}, falling back to streaming")

        url = f"https://music.youtube.com/watch?v={video_id}"

        # Use a local copy of options to prevent race conditions
        opts = self.ydl_opts.copy()

        cookie_file = None
        try:
            # Inject headers/cookies if authenticated
            if self.client.is_authenticated() and self.client.api:
                # Create Netscape cookie file
                cookie_file = self._create_cookie_file(self.client.api.headers)
                if cookie_file:
                    opts["cookiefile"] = cookie_file

                # CRITICAL: User-Agent MUST match the cookies for them to be accepted by YouTube
                ua = self.client.api.headers.get("User-Agent")
                if ua:
                    opts["user_agent"] = ua
                    # Also set it in http_headers for good measure
                    opts["http_headers"] = {"User-Agent": ua}

                # Still pass Authorization if available
                auth = self.client.api.headers.get("Authorization")
                if auth:
                    if "http_headers" not in opts:
                        opts["http_headers"] = {}
                    opts["http_headers"]["Authorization"] = auth
            else:
                pass

            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # HLS/DASH URLs often report seekable while their source
                # stalls before the first buffer. Stage those formats locally
                # instead of handing an unreliable manifest to playbin.
                if _is_manifest_protocol(
                    info.get("protocol") or info.get("url") or info.get("manifest_url")
                ):
                    self._mark_noseek(video_id)
                    staged = self._download_upload_to_tmpfs(video_id, generation)
                    if staged:
                        self._queue_staged_playback(
                            staged,
                            video_id,
                            generation,
                            "local staged manifest fallback",
                        )
                        return
                stream_url = info["url"]

                # Snapshot the resolved format for the debug panel. This is
                # the single most useful signal when seeking breaks: an HLS
                # (m3u8_native) / DASH protocol or a muxed itag means the
                # stream isn't byte-range seekable even though GStreamer
                # answers the SEEKING query optimistically.
                self._stream_debug = {
                    "source": "stream (yt-dlp)",
                    "video_id": video_id,
                    "itag": info.get("format_id"),
                    "protocol": info.get("protocol"),
                    "ext": info.get("ext"),
                    "acodec": info.get("acodec"),
                    "abr": info.get("abr"),
                    "container": info.get("container"),
                    "format_note": info.get("format_note"),
                }

                # Extract only what we need, then drop the large info dict
                fetched_title = info.get("title", "Unknown")
                fetched_artist = info.get("uploader", "Unknown")
                fetched_thumb = info.get("thumbnail")
                del info  # Free 100KB+ of format/subtitle data

                # If hints are placeholders, try to get better metadata from ytmusicapi
                if (not title_hint or title_hint == "Loading...") or (
                    not artist_hint or artist_hint == "Unknown"
                ):
                    try:
                        song_details = self.client.get_song(video_id)
                        if song_details:
                            v_details = song_details.get("videoDetails", {})
                            if "title" in v_details:
                                fetched_title = v_details["title"]
                            if "author" in v_details:
                                fetched_artist = v_details["author"]

                            # Use high-res thumbnail from get_song if available
                            if (
                                not thumb_hint
                                and "thumbnail" in v_details
                                and "thumbnails" in v_details["thumbnail"]
                            ):
                                thumbs = v_details["thumbnail"]["thumbnails"]
                                if thumbs:
                                    fetched_thumb = thumbs[-1]["url"]

                    except Exception as e:
                        print(f"Error fetching metadata from ytmusicapi: {e}")

                final_title = (
                    title_hint
                    if title_hint and title_hint != "Loading..."
                    else fetched_title
                )
                final_artist = (
                    artist_hint
                    if artist_hint and artist_hint != "Unknown"
                    else fetched_artist
                )

                print(f"Playing: {final_title} by {final_artist}")

                final_thumb = thumb_hint or fetched_thumb or ""
                if "ytimg.com" in final_thumb:
                    final_thumb = get_high_res_url(final_thumb)

                # Update the queue track if possible so subsequent refreshes find it
                if 0 <= self.current_queue_index < len(self.queue):
                    track = self.queue[self.current_queue_index]
                    if track.get("videoId") == video_id:
                        track["title"] = final_title
                        track["artist"] = final_artist
                        track["thumb"] = final_thumb

                        # Fetch album if missing (needed for Discord RPC)
                        if not track.get("album"):
                            try:
                                wp = self.client.get_watch_playlist(
                                    video_id=video_id, limit=1
                                )
                                wp_tracks = wp.get("tracks", [])
                                if wp_tracks and wp_tracks[0].get("album"):
                                    track["album"] = wp_tracks[0]["album"]
                                    if getattr(self, "discord_rpc", None):
                                        self.discord_rpc.update()
                            except Exception:
                                pass

                # Check generation again before playing
                if generation != self.load_generation:
                    print(
                        f"Stale load generation {generation} before playbin set. Aborting."
                    )
                    if cookie_file and os.path.exists(cookie_file):
                        os.remove(cookie_file)
                    return

                # Do not persist a signed URL until GStreamer confirms the
                # source reached PLAYING.  Caching a URL that merely looked
                # valid to yt-dlp is what made a bad CDN response survive
                # across reloads.
                self._pending_stream_cache = (generation, video_id, stream_url)

                if getattr(self, "_cache_failed_waiting", False):
                    # Cached URL failed earlier, yt-dlp just finished - play now
                    print("[CACHE] yt-dlp finished, playing after cache failure")
                    self._cache_failed_waiting = False
                    GObject.idle_add(self._start_playback, stream_url, generation)
                elif self._used_cached_url:
                    # Store the fresh URL as fallback in case cached URL fails
                    self._fallback_stream_url = stream_url
                else:
                    GObject.idle_add(self._start_playback, stream_url, generation)

                GObject.idle_add(
                    self._emit_metadata_if_current,
                    generation,
                    final_title,
                    final_artist,
                    final_thumb,
                    video_id,
                    like_status_hint,
                )

                # Pre-cache next songs in queue
                if self._precache_enabled:
                    self._precache_next(generation)
        except Exception as e:
            # yt-dlp throws DownloadError / ExtractorError when a video is
            # unavailable, region-locked, removed, etc. The previous code
            # silently swallowed this — the player just sat in "loading"
            # forever. Skip to the next track and surface the error to the
            # UI via the new track-error signal.
            print(f"Error fetching URL for {video_id}: {e}")
            if generation == self.load_generation:
                msg = self._summarize_yt_dlp_error(e)
                failed_title = title_hint or "Track"
                GObject.idle_add(
                    self.emit, "track-error", video_id, failed_title, msg
                )
                # Auto-advance if there's something to advance to. If we're
                # already at the end of the queue, just stop instead of
                # looping back.
                if self.current_queue_index + 1 < len(self.queue):
                    GObject.idle_add(self.next)
                else:
                    GObject.idle_add(self.stop)
        finally:
            if cookie_file and os.path.exists(cookie_file):
                try:
                    os.remove(cookie_file)
                except:
                    pass

    def _summarize_yt_dlp_error(self, exc):
        """Pull a short, user-readable reason out of a yt-dlp exception.
        yt-dlp's str(exc) is usually a long path + verbose traceback line;
        we only want the bit a human would put in a toast."""
        msg = str(exc) if exc else ""
        # Prefer the first 'ERROR: ...' chunk if yt-dlp emitted one.
        if "ERROR:" in msg:
            msg = msg.split("ERROR:", 1)[1].strip()
        # Strip the ': <verbose>' tail past the first sentence so the toast
        # doesn't wrap forever.
        for sep in (". ", "; "):
            if sep in msg:
                msg = msg.split(sep, 1)[0]
                break
        msg = msg.strip(" .")
        if not msg:
            msg = "Could not load this track"
        return msg[:140]

    def _precache_next(self, generation, max_count=None):
        """Pre-cache stream URLs for nearby queue entries, cancellably."""

        with self._precache_lock:
            epoch = self._precache_epoch
            cancel = self._precache_cancel
        if not self._precache_still_current(epoch, cancel):
            return
        if generation != self.load_generation:
            return

        current = self.current_queue_index
        queue_len = len(self.queue)
        indices = []
        for offset in range(1, 4):
            if current + offset < queue_len:
                indices.append(current + offset)
            if current - offset >= 0:
                indices.append(current - offset)
        if max_count is not None:
            indices = indices[:max_count]
        if not indices:
            return

        from yt_dlp import YoutubeDL

        for i, idx in enumerate(indices):
            if not self._precache_still_current(epoch, cancel):
                return
            # Throttle between extractions so the GIL stays free for the
            # UI thread between batches.  Event.wait also makes disabling
            # pre-cache or changing tracks interrupt the sleep promptly.
            if i > 0 and cancel.wait(0.25):
                return
            if not self._precache_still_current(epoch, cancel):
                return
            if generation != self.load_generation:
                return

            track = self.queue[idx]
            vid = track.get("videoId")
            if not vid or self.stream_cache.get(vid):
                continue
            # Upload tracks are staged locally, so their remote URL is not
            # useful background work.
            if track.get("entityId"):
                continue

            cookie_file = None
            ydl = None
            try:
                url = f"https://music.youtube.com/watch?v={vid}"
                opts = self.ydl_opts.copy()
                opts["quiet"] = True
                opts.pop("verbose", None)
                if self.client.is_authenticated() and self.client.api:
                    cookie_file = self._create_cookie_file(self.client.api.headers)
                    if cookie_file:
                        opts["cookiefile"] = cookie_file
                    ua = self.client.api.headers.get("User-Agent")
                    if ua:
                        opts["user_agent"] = ua

                ydl = YoutubeDL(opts)
                with self._precache_lock:
                    if not self._precache_still_current(epoch, cancel):
                        try:
                            ydl.close()
                        except Exception:
                            pass
                        return
                    self._precache_downloaders.add(ydl)
                with ydl:
                    if not self._precache_still_current(epoch, cancel):
                        continue
                    info = ydl.extract_info(url, download=False)
                    if _is_manifest_protocol(
                        info.get("protocol")
                        or info.get("url")
                        or info.get("manifest_url")
                    ):
                        continue
                    stream_url = info["url"]
                    del info
                if not self._precache_still_current(epoch, cancel):
                    continue
                self.stream_cache.put(vid, stream_url)
                print(f"[CACHE] Pre-cached stream URL for song {idx}: {vid}")
            except Exception as exc:
                if not cancel.is_set():
                    print(f"[CACHE] Pre-cache error for {vid}: {exc}")
            finally:
                if ydl is not None:
                    with self._precache_lock:
                        self._precache_downloaders.discard(ydl)
                if cookie_file and os.path.exists(cookie_file):
                    try:
                        os.remove(cookie_file)
                    except OSError:
                        pass

    def _commit_pending_stream_cache(self):
        pending = self._pending_stream_cache
        if not pending:
            return
        # The tuple is generation-aware so a late resolver cannot cache a
        # URL that belongs to a track/source which has already been replaced.
        if len(pending) == 3:
            generation, video_id, stream_url = pending
        else:  # tolerate a tuple left by an older in-process callback
            video_id, stream_url = pending
            generation = self.load_generation
        if generation != self.load_generation or video_id != self.current_video_id:
            self._pending_stream_cache = None
            return
        # A cached URL can be playing while the background resolver has just
        # produced a different fallback URL.  Keep the latter pending until
        # that exact URI reaches PLAYING; otherwise the first source's
        # STATE_CHANGED event would incorrectly persist the untried fallback.
        if not self._current_play_uri or stream_url != self._current_play_uri:
            return
        self._pending_stream_cache = None
        self.stream_cache.put(video_id, stream_url)

    def _on_source_setup(self, playbin, source):
        """Configure the HTTP source element playbin just created. We push
        the signed-in client's Cookie + User-Agent onto every request so
        YT's upload-locker URL honors byte-range requests (i.e. seeking)
        the same way the web player does."""
        if not source:
            return
        # Only relevant for HTTP-based sources (souphttpsrc, curlhttpsrc).
        # file:// playback gets a filesrc which doesn't have these props.
        try:
            name = source.get_factory().get_name()
        except Exception:
            name = ""
        self._source_factory_name = name or None
        # Bus errors are asynchronous and do not carry a load generation.
        # Keep a small source-id map so an error from the previous source
        # cannot be applied to the track that replaced it.
        self._last_source_id = id(source)
        self._last_source_key = source
        if self._pipeline_generation is not None:
            generation = self._pipeline_generation
            with self._source_generation_lock:
                self._source_generations[self._last_source_id] = generation
                try:
                    self._source_generations[source] = generation
                except (TypeError, AttributeError):
                    pass
                if len(self._source_generations) > 64:
                    for old_id in list(self._source_generations)[:-64]:
                        self._source_generations.pop(old_id, None)
        if name not in ("souphttpsrc", "curlhttpsrc"):
            return

        try:
            if self.client and self.client.is_authenticated() and self.client.api:
                headers = self.client.api.headers or {}
                ua = headers.get("User-Agent")
                cookie = headers.get("Cookie")
                if ua:
                    try:
                        source.set_property("user-agent", ua)
                    except Exception:
                        pass

                # extra-headers is a Gst.Structure of arbitrary HTTP
                # headers — this is what gets sent on EVERY request the
                # source makes (initial GET + each Range follow-up).
                if cookie:
                    extra = Gst.Structure.new_empty("extra-headers")
                    extra.set_value("Cookie", cookie)
                    auth = headers.get("Authorization")
                    if auth:
                        extra.set_value("Authorization", auth)
                    try:
                        source.set_property("extra-headers", extra)
                    except Exception as e:
                        print(f"[PLAYER] set extra-headers failed ({type(e).__name__}).")
        except Exception as e:
            print(f"[PLAYER] source-setup hook error ({type(e).__name__}).")

    def _emit_metadata_if_current(self, generation, title, artist, thumb, video_id, like_status):
        """Publish metadata only if its load still owns the player."""

        if generation != self.load_generation:
            return GLib.SOURCE_REMOVE
        self.emit(
            "metadata-changed", title, artist, thumb, video_id, like_status
        )
        return GLib.SOURCE_REMOVE

    def _ensure_pipeline_worker_locked(self):
        # Several UI and bus callbacks can request a transition at once.  A
        # check-then-start without a lock can create two workers, defeating
        # the serialization this class is built around.
        if self._pipeline_shutdown:
            return
        worker = self._pipeline_worker
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(
            target=self._pipeline_loop,
            name="ventapes-pipeline",
            daemon=True,
        )
        self._pipeline_worker = worker
        try:
            worker.start()
        except Exception:
            self._pipeline_worker = None
            raise

    def _ensure_pipeline_worker(self):
        with self._pipeline_worker_lock:
            self._ensure_pipeline_worker_locked()

    def _queue_pipeline_command(self, action, generation=None, uri=None):
        # Keep the shutdown check, worker creation, and queue insertion under
        # one mutex.  Otherwise shutdown can append its sentinel in the gap
        # and a late play command can be stranded behind it (or win the
        # sentinel batch).
        with self._pipeline_worker_lock:
            if self._pipeline_shutdown:
                return
            self._ensure_pipeline_worker_locked()
            self._pipeline_commands.put((action, generation, uri))

    def shutdown(self):
        """Stop the serialized worker and release local playback files."""

        with self._pipeline_worker_lock:
            if self._pipeline_shutdown:
                return
            worker = self._pipeline_worker
            self._pipeline_shutdown = True
            # Publish the terminal commands while holding the same mutex used
            # by _queue_pipeline_command().  Once the flag is set, no new
            # command can be inserted after this sentinel.
            if worker is not None:
                self._pipeline_commands.put(("stop", None))
                self._pipeline_commands.put(None)
                self._pipeline_shutdown_sentinel.set()

        try:
            if getattr(self, "scrobbler", None) is not None:
                self.scrobbler.stop()
        except Exception as exc:
            print(f"[SCROBBLE] shutdown failed: {exc}")

        # The terminal worker will clean deferred paths after NULL.  Queue the
        # current source now so a slow/stuck worker cannot leave it leased
        # after shutdown returns.
        if self._current_tmpfs_path:
            self._defer_tmpfs_cleanup(self._current_tmpfs_path)

        # Invalidate resolver/staging callbacks before asking the worker to
        # stop.  Otherwise a late completion can enqueue a new URI after the
        # shutdown sentinel and keep a staged file alive indefinitely.
        with self._generation_lock:
            self.load_generation += 1
            self._pipeline_generation = None
            self._stream_started_generation = None
            self._pending_gapless_index = None
            self._pending_gapless_generation = None
            self._is_loading = False
        self._pipeline_null_confirmed.clear()
        self._stop_position_timer()
        self._cancel_staging_jobs("shutdown", wait=True, timeout=0.5)
        self._cancel_precache("shutdown")
        if worker is not None and worker.is_alive():
            if worker is not threading.current_thread():
                worker.join(timeout=0.75)
        else:
            with self._pipeline_lock:
                result = self._set_pipeline_state(Gst.State.NULL, 300)
                if result != Gst.StateChangeReturn.FAILURE:
                    self._pipeline_null_confirmed.set()
                    self._cleanup_pending_tmpfs()
        try:
            self.bus.remove_signal_watch()
            if getattr(self, "_bus_handler_id", None):
                self.bus.disconnect(self._bus_handler_id)
                self._bus_handler_id = None
        except Exception:
            pass
        # Only unlink local sources after the serialized worker has confirmed
        # NULL.  If it is still unwinding, leave tracked paths for the atexit
        # pass (which is intentionally conservative) rather than unlinking
        # under playbin.
        if self._pipeline_null_confirmed.is_set():
            self._cleanup_all_tmpfs()

    @staticmethod
    def _coalesce_pipeline_commands(commands):
        """Keep rapid transitions short without dropping a final transport edge.

        A play command already performs its own NULL -> URI -> PLAYING
        transaction, so older stops/controls before the newest play are
        redundant.  A pause/resume *after* that play is not redundant: users
        can press Pause while a new URI is still resolving.  A stop after a
        play is terminal and supersedes the play and any later-looking stale
        control edge.
        """

        if not commands:
            return []
        last_play = -1
        for index, command in enumerate(commands):
            if command[0] == "play":
                last_play = index
        if last_play >= 0:
            trailing = commands[last_play + 1 :]
            for index in range(len(commands) - 1, last_play, -1):
                if commands[index][0] == "stop":
                    return [commands[index]]
            result = [commands[last_play]]
            for command in reversed(trailing):
                if command[0] in ("pause", "resume"):
                    result.append(command)
                    break
            return result

        for command in reversed(commands):
            if command[0] == "stop":
                return [command]
        return [commands[-1]]

    def _pipeline_loop(self):
        """Serialize GStreamer state changes and collapse command bursts."""

        while True:
            try:
                command = self._pipeline_commands.get(timeout=0.5)
            except queue.Empty:
                if (
                    self._pipeline_shutdown
                    and self._pipeline_shutdown_sentinel.is_set()
                ):
                    return
                continue
            if command is None:
                return

            batch = [command]
            terminal = False
            while True:
                try:
                    newer = self._pipeline_commands.get_nowait()
                except queue.Empty:
                    break
                if newer is None:
                    # Do not let a command inserted after the shutdown
                    # sentinel win a coalescing batch.  The enqueue mutex
                    # normally makes this impossible, but keeping the barrier
                    # explicit makes the worker safe for legacy direct callers.
                    terminal = True
                    break
                batch.append(newer)

            for action, generation, uri in self._coalesce_pipeline_commands(batch):
                try:
                    if action == "stop":
                        # Even an old stop must flush the old source, but it
                        # must not clear state belonging to a newer load.
                        self._execute_pipeline_stop(generation)
                    elif action == "play":
                        self._execute_pipeline_play(generation, uri)
                    elif action == "resume":
                        self._execute_pipeline_resume(generation)
                    elif action == "pause":
                        self._execute_pipeline_pause(generation)
                except Exception as exc:
                    print(f"[PLAYBACK] pipeline {action} failed: {exc}")
            if terminal:
                return

    def _set_pipeline_state(self, state, wait_ms=0):
        try:
            result = self.player.set_state(state)
            if wait_ms:
                # Waiting happens on this worker, never on the GTK thread.
                # It prevents a URI handoff from overtaking a still-pending
                # NULL transition without imposing a long wait on a broken
                # network source.
                _, reached, _pending = self.player.get_state(wait_ms * 1_000_000)
                reached_nick = getattr(reached, "value_nick", reached)
                if state == Gst.State.NULL and reached != Gst.State.NULL:
                    print(
                        f"[PLAYBACK] pipeline did not reach NULL "
                        f"(state={reached_nick})"
                    )
                    return Gst.StateChangeReturn.FAILURE
                if state == Gst.State.PLAYING and reached != Gst.State.PLAYING:
                    # ASYNC_DONE/STATE_CHANGED will finish a normal async
                    # preroll, but a timeout that is still READY/PAUSED (or
                    # has no state yet) is not a successful transition.
                    # Report it so the caller can retry or surface a failure
                    # instead of waiting forever for a buffering message that
                    # may never arrive.
                    print(
                        f"[PLAYBACK] pipeline did not reach PLAYING "
                        f"(state={reached_nick})"
                    )
                    return Gst.StateChangeReturn.FAILURE
            return result
        except Exception as exc:
            print(f"[PLAYBACK] state change to {state} failed: {exc}")
            return Gst.StateChangeReturn.FAILURE

    def _execute_pipeline_stop(self, generation=None):
        with self._pipeline_lock:
            self._pipeline_null_confirmed.clear()
            result = self._set_pipeline_state(Gst.State.NULL, 300)
            if result != Gst.StateChangeReturn.FAILURE:
                self._pipeline_null_confirmed.set()
                self._cleanup_pending_tmpfs()
            # The NULL operation is useful even for an old generation, but
            # state bookkeeping belongs only to the generation that requested
            # it.  A new load can begin while get_state() is waiting.
            with self._generation_lock:
                owns_generation = (
                    generation is None or generation == self.load_generation
                )
                if owns_generation:
                    self._pending_gapless_index = None
                    self._pending_gapless_generation = None
            if owns_generation:
                self._pipeline_generation = None
                self._is_loading = False
                self._seek_after_load = None
                self._pending_seek = None

    def _execute_pipeline_play(self, generation, uri):
        if self._pipeline_shutdown:
            return
        if generation is None or generation != self.load_generation:
            return
        with self._pipeline_lock:
            if self._pipeline_shutdown or generation != self.load_generation or not uri:
                return
            self._current_play_uri = uri
            self._current_play_uri_generation = generation
            self._is_loading = True
            self._pipeline_null_confirmed.clear()
            null_result = self._set_pipeline_state(Gst.State.NULL, 1000)
            if null_result == Gst.StateChangeReturn.FAILURE:
                # A stuck source should not receive a new URI on top of it.
                # Give the flush one short retry, then surface a stopped
                # state so the user can explicitly retry instead of leaving
                # the UI in an unbreakable loading spinner.
                null_result = self._set_pipeline_state(Gst.State.NULL, 300)
            if null_result == Gst.StateChangeReturn.FAILURE:
                if generation == self.load_generation:
                    self._is_loading = False
                    self._pipeline_generation = None
                    self._current_logical_state = "stopped"
                    GLib.idle_add(self.emit, "state-changed", "stopped")
                return
            self._cleanup_pending_tmpfs()
            if generation != self.load_generation:
                return
            self._pipeline_generation = generation
            self._stream_started_generation = None
            self._pipeline_started_at = time.monotonic()
            self.player.set_property("uri", uri)
            if generation != self.load_generation:
                return
            result = self._set_pipeline_state(Gst.State.PLAYING, 900)
            if result == Gst.StateChangeReturn.FAILURE:
                # A few HTTP/source implementations report a transient
                # failure while their socket is being replaced.  Retry once
                # on the same serialized worker; never start a second
                # transition thread here.
                import time as _time
                _time.sleep(0.08)
                if generation == self.load_generation:
                    retry_result = self._set_pipeline_state(
                        Gst.State.PLAYING, 500
                    )
                    if retry_result == Gst.StateChangeReturn.FAILURE:
                        self._is_loading = False
                        self._update_logical_state()

    def _execute_pipeline_resume(self, generation=None):
        if self._pipeline_shutdown:
            return
        if generation is not None and generation != self.load_generation:
            return
        with self._pipeline_lock:
            if self._pipeline_shutdown:
                return
            self._set_pipeline_state(Gst.State.PLAYING, 500)

    def _execute_pipeline_pause(self, generation=None):
        if self._pipeline_shutdown:
            return
        if generation is not None and generation != self.load_generation:
            return
        with self._pipeline_lock:
            if self._pipeline_shutdown:
                return
            self._set_pipeline_state(Gst.State.PAUSED, 300)

    def _start_playback(self, uri, generation=None, cookie_file=None):
        if not uri or self._pipeline_shutdown:
            return False
        # Before generation-aware playback, the second positional argument
        # was the optional cookie-file path.  Keep that private-call shape
        # working for integrations while all in-tree callers pass an int
        # generation.
        if generation is not None and not isinstance(generation, int):
            if cookie_file is None:
                cookie_file = generation
            generation = None
        if generation is None:
            generation = self.load_generation
        if generation != self.load_generation:
            return False
        self._current_play_uri = uri
        self._current_play_uri_generation = generation
        self._is_loading = True
        self._start_position_timer()
        self._load_media_api()
        if hasattr(self, "mpris_events"):
            idx = self.current_queue_index
            if 0 <= idx < len(self.queue):
                track = self.queue[idx]
                if track.get("thumb"):
                    self._sync_mpris_art(track.get("thumb"), track.get("videoId"))
        if hasattr(self, "mpris_server"):
            self.mpris_server.publish()
        if self._pipeline_shutdown:
            self._is_loading = False
            return GLib.SOURCE_REMOVE
        self._queue_pipeline_command("play", generation, uri)
        return GLib.SOURCE_REMOVE

    def play(self):
        # A new track may still be resolving while the old pipeline is being
        # flushed.  Replaying the old URI (or starting another load) during
        # that window races the generation-aware resolver.
        if self._is_loading:
            return
        # If a previous asynchronous load left playbin at NULL, setting
        # PLAYING alone cannot recover because the URI handoff may not have
        # completed.  Re-submit the complete serialized transaction instead.
        try:
            state = self.player.get_state(0)[1]
        except Exception:
            state = Gst.State.NULL
        if state == Gst.State.NULL:
            if (
                self.current_video_id
                and 0 <= self.current_queue_index < len(self.queue)
                and self._current_play_uri
                and getattr(self, "_current_play_uri_generation", None)
                == self.load_generation
            ):
                self._queue_pipeline_command(
                    "play", self.load_generation, self._current_play_uri
                )
            elif self.queue:
                self._play_current_index()
            else:
                return
        else:
            self._queue_pipeline_command("resume", self.load_generation)
        self._update_logical_state()

    def pause(self):
        self._queue_pipeline_command("pause", self.load_generation)
        self._update_logical_state()

    def stop(self):
        if hasattr(self, "mpris_server"):
            self.mpris_server.unpublish()

        # Invalidate every outstanding URL/worker callback before queuing
        # the flush.  Without this, a late yt-dlp completion from the old
        # track could enqueue PLAYING again after the user pressed stop.
        with self._generation_lock:
            self.load_generation += 1
            stop_generation = self.load_generation
            self._pipeline_generation = None
            self._stream_started_generation = None
            self._pending_gapless_index = None
            self._pending_gapless_generation = None
        self._pipeline_started_at = 0.0
        self._pipeline_null_confirmed.clear()
        self._cancel_staging_jobs("stop")
        self._cancel_precache("stop")
        if self._current_tmpfs_path:
            self._defer_tmpfs_cleanup(self._current_tmpfs_path)
        self._queue_pipeline_command("stop", stop_generation)
        self._stop_position_timer()
        self._is_loading = False
        self._seek_after_load = None
        self._pending_seek = None
        self._pending_stream_cache = None
        self._last_position_seconds = 0.0
        self._last_duration_seconds = 0.0
        self._track_started_at = 0.0
        self._used_cached_url = False
        self._fallback_stream_url = None
        self._cache_failed_waiting = False
        # Force stopped state immediately; the worker will finish the actual
        # pipeline flush off the UI thread.
        if self._current_logical_state != "stopped":
            self._current_logical_state = "stopped"
            self.emit("state-changed", "stopped")

    def _update_logical_state(self):
        new_state = "stopped"
        if self.player:
            state = self.player.get_state(0)[1]
            if state == Gst.State.PLAYING:
                new_state = "playing"
            elif state == Gst.State.PAUSED:
                new_state = "paused"

        # During a load, the old pipeline may still report PLAYING/PAUSED
        # while the serialized worker is flushing it.  Do not let that stale
        # state overwrite the explicit loading state; STATE_CHANGED/
        # BUFFERING will clear _is_loading when the new source is ready.
        if self._is_loading:
            return
        if new_state != self._current_logical_state:
            self._current_logical_state = new_state
            try:
                GLib.idle_add(self.emit, "state-changed", new_state)
            except Exception:
                pass

    def _dispatch_spectrum_message(self, structure):
        """Queue the per-band magnitudes keyed by the audio's stream-time
        (position within the current track). Visualizer widgets pull the
        latest entry whose stream-time the audio sink has actually
        reached — this gives free pipeline-clock sync without needing the
        user to calibrate sink latency.

        Stream-time, not running-time: after a seek, running-time keeps
        advancing monotonically but `query_position(Gst.Format.TIME)`
        returns the new (post-seek) stream-time. Mixing the two would
        leave the bars stuck on stale data until the running-time delta
        caught up. Both sides have to agree on the same clock.

        GStreamer's `spectrum` reports `magnitude` as a GstValueList in
        modern (≥1.20) builds and as a GValueArray on older systems.
        `_extract_spectrum_bands` walks all three shapes.
        """
        if not getattr(self, "_visualizer_first_msg_logged", False):
            self._visualizer_first_msg_logged = True
            print("[VISUALIZER] first spectrum message received — data flowing")

        bands = _extract_spectrum_bands(structure)
        if not bands:
            return

        try:
            st_ok, stream_time_ns = structure.get_clock_time("stream-time")
        except Exception:
            st_ok = False
            stream_time_ns = 0
        if not st_ok:
            # Older spectrum builds don't tag stream-time. Mark the entry
            # with -1 so pull_visualizer_bands knows to return it ASAP
            # (no sync available).
            stream_time_ns = -1

        self._viz_queue.append((int(stream_time_ns), bands))
        # The deque is already bounded; keep this guard for safety if a
        # caller replaces it with a plain list in a test.
        while len(self._viz_queue) > 64:
            self._viz_queue.popleft()

    def pull_visualizer_bands(self):
        """Return the spectrum bands the audio sink is currently playing,
        or None. Driven by visualizer widgets on their UI tick — non-
        destructive so multiple widgets (main + settings preview) can
        share the same queue.

        Returns None while paused / stopped so the widget's gravity loop
        lets the bars fall to zero. If we returned the latest queued
        entry instead, _ingest_magnitudes would re-snap the levels up to
        it every tick and the bars would visibly freeze.

        Strategy: walk the queue, return the most recent entry whose
        stream_time ≤ current sink position, and drop entries that have
        fallen >1s behind so the deque stays small.
        """
        if not self._viz_queue:
            return None
        try:
            state = self.player.get_state(0)[1]
        except Exception:
            state = None
        if state != Gst.State.PLAYING:
            return None
        try:
            pos_ok, pos_ns = self.player.query_position(Gst.Format.TIME)
        except Exception:
            pos_ok, pos_ns = False, 0
        if not pos_ok or pos_ns < 0:
            # No clock to sync against (pre-roll, between tracks). Hand
            # back the latest available; it'll be approximately right and
            # the next tick will correct.
            return self._viz_queue[-1][1]

        latest = None
        for rt, bands in self._viz_queue:
            if rt < 0 or rt <= pos_ns:
                latest = bands
            else:
                break

        # Trim entries the play-head is more than a second past — old
        # spectrum frames the sink can never reach again after the
        # play-head moved on (e.g. after a seek forward or just normal
        # advance).
        stale_threshold = pos_ns - 1_000_000_000
        while self._viz_queue and 0 <= self._viz_queue[0][0] < stale_threshold:
            self._viz_queue.popleft()

        return latest

    def _ready_message_is_too_early(self):
        """Whether a PLAYING/ASYNC_DONE message predates this URI."""
        if self._stream_started_generation == self.load_generation:
            return False
        if self._stream_started_generation is not None:
            return True
        started = self._pipeline_started_at
        return bool(started and time.monotonic() - started < 2.0)

    def on_message(self, bus, message):
        if self._pipeline_shutdown:
            return
        t = message.type
        # Bus messages can outlive the source that produced them.  Source
        # elements are tagged during source-setup; an old tagged message must
        # not consume the current track's retry/cache state.
        try:
            with self._source_generation_lock:
                message_generation = self._source_generations.get(message.src)
                if message_generation is None:
                    message_generation = self._source_generations.get(
                        id(message.src), self._pipeline_generation
                    )
        except Exception:
            message_generation = self._pipeline_generation
        if (
            message_generation is not None
            and message_generation != self.load_generation
        ):
            return
        if (
            t in (
                Gst.MessageType.STATE_CHANGED,
                Gst.MessageType.ASYNC_DONE,
                Gst.MessageType.EOS,
            )
            and message.src == self.player
            and self._pipeline_generation is None
            and self._is_loading
        ):
            return
        if t == Gst.MessageType.STREAM_START:
            self._stream_started_generation = (
                message_generation
                if message_generation is not None
                else self.load_generation
            )
            self._pipeline_started_at = 0.0
            # Fired when playbin starts a new stream — for gapless this is
            # the precise moment the pipeline switched to the uri we set
            # in _on_about_to_finish. Catch up our state on the main thread.
            if self._pending_gapless_index is not None:
                print(
                    f"[GAPLESS] stream-start for pending index="
                    f"{self._pending_gapless_index}",
                    flush=True,
                )
                GLib.idle_add(
                    self._apply_gapless_transition,
                    self._pending_gapless_generation,
                )
                return
        if t == Gst.MessageType.BUFFERING:
            try:
                percent = int(message.parse_buffering())
            except Exception:
                percent = 100
            if percent < 100:
                if not self._buffering_since:
                    self._buffering_since = time.monotonic()
                if not self._is_loading:
                    self._current_logical_state = "loading"
                    self.emit("state-changed", "loading")
                self._is_loading = True
                self._start_position_timer()
            else:
                self._buffering_since = 0.0
                self._is_loading = False
                self._next_duration_probe = 0.0
                self._update_logical_state()
            return
        if t == Gst.MessageType.EOS:
            # Ignore EOS that arrives mid-load. When the user skips rapidly,
            # GStreamer can emit EOS for the *previous* stream as it tears
            # down — acting on it would queue an extra next() and over-advance
            # the queue, eventually wrapping to 0 under repeat=all.
            if self._is_loading:
                print("EOS during load — ignoring (stale stream).", flush=True)
                return
            # Bus messages are async — a stale EOS from the previous pipeline
            # can land *after* the new track has already reached PLAYING.
            # Reject EOS that arrives within the first second of a new track.
            import time as _time
            if (
                self._track_started_at
                and _time.time() - self._track_started_at < 1.0
            ):
                print(
                    "EOS within 1s of track start — ignoring (stale stream).",
                    flush=True,
                )
                return
            print("EOS Reached. Advancing to next track.", flush=True)
            self.stop()
            if self.repeat_mode == "track":
                GObject.idle_add(self._play_current_index)
            else:
                GObject.idle_add(self.next)
        elif t == Gst.MessageType.ASYNC_DONE:
            if self._ready_message_is_too_early():
                return
            # The stream is actually loaded and ready.  Reset recovery state
            # here, not on the earlier PLAYING state transition: playbin can
            # report PLAYING before it has produced a decodable buffer.
            self._next_duration_probe = 0.0
            self._stream_retry_count = 0
            self._stall_recovery_active = False
            self._is_loading = False
            self._commit_pending_stream_cache()
            self._start_position_timer()
            if hasattr(self, "mpris_events"):
                self.mpris_events.on_player_all()  # Refresh duration and status
            # The seek-fallback parked a target here: now that the local file
            # has prerolled (and is seekable), apply it. Pop first so the
            # flushing seek's own ASYNC_DONE doesn't re-trigger.
            if self._seek_after_load is not None:
                pos = self._seek_after_load
                self._seek_after_load = None
                GLib.idle_add(self.seek, pos)
            pending = self._pending_seek
            if pending is not None:
                pending_generation, pos, flush = pending
                self._pending_seek = None
                if pending_generation == self.load_generation:
                    GLib.idle_add(self.seek, pos, flush)
        elif t == Gst.MessageType.ELEMENT:
            # Spectrum analyzer posts magnitude data here on every interval.
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "spectrum":
                self._dispatch_spectrum_message(structure)
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}, {debug}")
            error_generation = (
                message_generation
                if message_generation is not None
                else self.load_generation
            )

            # If cached URL failed, try the fresh yt-dlp resolved URL
            if self._used_cached_url:
                if self._current_tmpfs_path:
                    self._defer_tmpfs_cleanup(self._current_tmpfs_path)
                fallback = getattr(self, "_fallback_stream_url", None)
                self._used_cached_url = False
                if fallback:
                    print("[CACHE] Cached URL failed, using fresh URL")
                    self._fallback_stream_url = None
                    if self.current_video_id:
                        self._pending_stream_cache = (
                            error_generation,
                            self.current_video_id,
                            fallback,
                        )
                    self._start_playback(fallback, generation=error_generation)
                    return
                else:
                    # yt-dlp hasn't finished yet - flag so it plays when ready
                    print("[CACHE] Cached URL failed, waiting for yt-dlp...")
                    self._cache_failed_waiting = True
                    self._queue_pipeline_command("stop", error_generation)
                    return

            # Fresh yt-dlp URLs can still 503 because googlevideo rotates
            # hosts and the format we picked sometimes sits behind a
            # flaky one. Invalidate the cache entry and re-resolve —
            # yt-dlp usually lands on a different host the second time.
            # Capped at `_stream_retry_max` so a genuinely dead video
            # can't loop forever.
            vid = self.current_video_id
            if (
                vid
                and getattr(self, "_stream_retry_count", 0)
                < getattr(self, "_stream_retry_max", 2)
            ):
                self._stream_retry_count = getattr(self, "_stream_retry_count", 0) + 1
                print(
                    f"[PLAYER] stream error (attempt "
                    f"{self._stream_retry_count}/{self._stream_retry_max}), "
                    f"re-resolving {vid}"
                )
                try:
                    self.stream_cache.invalidate(vid)
                except Exception:
                    pass
                if self._current_tmpfs_path:
                    self._defer_tmpfs_cleanup(self._current_tmpfs_path)
                # Kick off a fresh yt-dlp resolution on a background
                # thread; when it lands, `_fetch_and_play` will call
                # _start_playback with the new URL.
                idx = self.current_queue_index
                if 0 <= idx < len(self.queue):
                    track = self.queue[idx]
                    self._is_loading = True
                    with self._generation_lock:
                        self.load_generation += 1
                        retry_generation = self.load_generation
                    self._pipeline_generation = None
                    self._stream_started_generation = None
                    self._pipeline_started_at = 0.0
                    self._pipeline_null_confirmed.clear()
                    self._current_play_uri = None
                    self._current_play_uri_generation = None
                    self._cancel_staging_jobs("stream retry")
                    self._cancel_precache("stream retry")
                    # Flush the failed source under its old generation so
                    # the worker cannot clear the new retry's loading state.
                    self._queue_pipeline_command("stop", error_generation)
                    gen = retry_generation
                    threading.Thread(
                        target=self._fetch_and_play,
                        args=(
                            vid,
                            track.get("title", ""),
                            track.get("artist", ""),
                            track.get("thumb"),
                            track.get("likeStatus", "INDIFFERENT"),
                            gen,
                        ),
                        daemon=True,
                    ).start()
                    return

            if self._current_tmpfs_path:
                self._defer_tmpfs_cleanup(self._current_tmpfs_path)
            self._queue_pipeline_command("stop", error_generation)
            self._is_loading = False
            self._update_logical_state()
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.player:
                old, new, pending = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    if self._ready_message_is_too_early():
                        return
                    if self._user_volume == None:
                        self._user_volume = self.get_volume()

                    if abs(self.get_volume() - self._user_volume) > 0.001:
                        linear = GstAudio.StreamVolume.convert_volume(
                            GstAudio.StreamVolumeFormat.CUBIC,
                            GstAudio.StreamVolumeFormat.LINEAR,
                            self._user_volume,
                        )
                        self._internal_volume_change = True
                        self.player.set_property("volume", linear)
                        self._internal_volume_change = False
                    self._is_loading = False
                    self._commit_pending_stream_cache()
                    self._buffering_since = 0.0
                    self._last_progress_at = 0.0
                    self._last_progress_position = 0
                    self._start_position_timer()
                    self._next_duration_probe = 0.0
                    import time as _time
                    self._track_started_at = _time.time()
                    if getattr(self, "discord_rpc", None):
                        self.discord_rpc.update()
                elif new == Gst.State.PAUSED:
                    self._stop_position_timer()
                self._update_logical_state()
        # Buffering is handled above so a source that never reaches 100% can
        # be recovered instead of leaving the player apparently stuck.

    def get_state_string(self):
        """Returns the current logical player state."""
        return self._current_logical_state

    def get_position_snapshot(self):
        """Return the last emitted position/duration for a newly mapped view."""
        return (
            float(getattr(self, "_last_position_seconds", 0.0)),
            float(
                max(
                    getattr(self, "_last_duration_seconds", 0.0),
                    getattr(self, "duration", 0.0),
                )
            ),
        )

    def _publish_mpris_art_pixbuf(self, pixbuf, video_id):
        """Center-crop a pixbuf to a square and upscale small art before
        saving it as the MPRIS cover.

        Covers come in all shapes (video thumbnails are 16:9) and sizes, but
        MPRIS wants a square — and some clients render low-res art poorly — so
        we crop the centre square and upscale anything below MIN_ART_SIZE.
        Saves to the art cache and points mpris_art_url at it. Returns the
        saved path, or None if there was no pixbuf to save."""
        if not pixbuf:
            return None

        MIN_ART_SIZE = 512

        w = pixbuf.get_width()
        h = pixbuf.get_height()
        size = min(w, h)
        pixbuf = pixbuf.new_subpixbuf(
            (w - size) // 2, (h - size) // 2, size, size
        )

        if size < MIN_ART_SIZE:
            pixbuf = pixbuf.scale_simple(
                MIN_ART_SIZE, MIN_ART_SIZE, GdkPixbuf.InterpType.BILINEAR
            )

        cache_dir = os.path.join(GLib.get_user_cache_dir(), "ventapes")
        os.makedirs(cache_dir, exist_ok=True)

        # Cleanup old art files to prevent bloat and cache issues
        for old_art in glob.glob(os.path.join(cache_dir, "mpris_art_*.jpg")):
            try:
                os.remove(old_art)
            except OSError:
                pass

        # Use unique filename per track to bypass MPRIS client caching
        safe_video_id = video_id.replace("-", "_").replace(".", "_")
        target_path = os.path.join(cache_dir, f"mpris_art_{safe_video_id}.jpg")

        pixbuf.savev(target_path, "jpeg", ["quality"], ["90"])
        self.mpris_art_url = f"file://{target_path}"
        return target_path

    def _sync_mpris_art(self, url, video_id):
        """Download/crop artwork only when an MPRIS server needs it."""

        if not hasattr(self, "mpris_events"):
            return
        def job(current_url, fallbacks=None):
            # Try local cover first (works offline). Downloads embed the cover
            # in the audio file's tags (no sidecar cover.jpg), so reuse the
            # UI's extractor which pulls the embedded art into the cover cache
            # and returns a file:// URL. The cold path reads the audio file +
            # writes a JPEG, so it must stay off the main thread.
            if video_id and self.current_video_id == video_id:
                from ui.utils import resolve_local_cover

                local_cover = resolve_local_cover(video_id)
                if local_cover:
                    try:
                        path = local_cover[len("file://"):]
                        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
                        if self._publish_mpris_art_pixbuf(pixbuf, video_id):
                            if hasattr(self, "mpris_events"):
                                GLib.idle_add(self.mpris_events.on_player_all)
                            return
                    except Exception as e:
                        # Fall through to the network path on any failure.
                        print(f"[PLAYER] MPRIS local art failed: {e}")

            if not current_url or self.current_video_id != video_id:
                return

            try:
                # 1. Ensure we use clean high-res URL if not already provided
                if fallbacks is None:
                    clean_url = get_high_res_url(current_url)
                    fallbacks = get_ytimg_fallbacks(clean_url)
                    if current_url != clean_url and current_url not in fallbacks:
                        fallbacks.append(current_url)
                    fetch_url = clean_url
                else:
                    fetch_url = current_url

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }
                if self.client and self.client.is_authenticated():
                    # Use cookies for YouTube related domains to support private covers
                    if any(
                        d in fetch_url
                        for d in [
                            "youtube.com",
                            "ytimg.com",
                            "googleusercontent.com",
                            "ggpht.com",
                        ]
                    ):
                        cookie = self.client.api.headers.get("Cookie")
                        if cookie:
                            headers["Cookie"] = cookie

                import requests

                resp = requests.get(fetch_url, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.content

                # 2. Load, crop and upscale
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()

                if self._publish_mpris_art_pixbuf(pixbuf, video_id):
                    # 4. Notify MPRIS to refresh metadata with the NEW local URL
                    if hasattr(self, "mpris_events"):
                        GLib.idle_add(self.mpris_events.on_player_all)

                    # 5. Propagate the working URL back to the track so
                    # Discord RPC (which sends the URL string to Discord
                    # for it to fetch) and any other consumer of
                    # track["thumb"] stop showing the dead maxresdefault.
                    # update_track_thumbnail is a no-op when the URL
                    # already matches, so this is safe to call
                    # unconditionally.
                    if fetch_url and fetch_url != url:
                        GLib.idle_add(
                            self.update_track_thumbnail, video_id, fetch_url
                        )

            except Exception as e:
                if fallbacks:
                    next_url = fallbacks.pop(0)
                    print(f"[PLAYER] MPRIS art fallback to: {next_url}")
                    job(next_url, fallbacks)
                else:
                    print(f"[PLAYER] MPRIS art sync failed: {e}")

        thread = threading.Thread(target=job, args=(url,), daemon=True)
        thread.start()

    def _recover_stalled_stream(self, reason="stalled"):
        """Re-resolve a source that never produced a moving audio clock."""

        if self._stall_recovery_active or self.current_video_id is None:
            return
        if self._current_logical_state == "paused":
            return
        idx = self.current_queue_index
        if not (0 <= idx < len(self.queue)):
            return
        track = self.queue[idx]
        vid = self.current_video_id
        self._stall_recovery_active = True
        if getattr(self, "_stream_retry_count", 0) >= getattr(
            self, "_stream_retry_max", 2
        ):
            self._stall_recovery_active = False
            if self._current_tmpfs_path:
                self._defer_tmpfs_cleanup(self._current_tmpfs_path)
            self._queue_pipeline_command("stop", self.load_generation)
            self._is_loading = False
            self._current_logical_state = "stopped"
            GLib.idle_add(self.emit, "state-changed", "stopped")
            return
        self._stream_retry_count = getattr(self, "_stream_retry_count", 0) + 1
        print(
            f"[PLAYBACK] {reason}; re-resolving {vid} "
            f"(attempt {self._stream_retry_count})"
        )
        try:
            self.stream_cache.invalidate(vid)
        except Exception:
            pass

        if self._current_tmpfs_path:
            self._defer_tmpfs_cleanup(self._current_tmpfs_path)
        with self._generation_lock:
            previous_generation = self.load_generation
            self.load_generation += 1
            generation = self.load_generation
        self._pipeline_generation = None
        self._stream_started_generation = None
        self._pipeline_started_at = 0.0
        self._pipeline_null_confirmed.clear()
        self._current_play_uri = None
        self._current_play_uri_generation = None
        self._cancel_staging_jobs("stall recovery")
        self._cancel_precache("stall recovery")
        self._is_loading = True
        self._buffering_since = 0.0
        self._last_progress_at = 0.0
        self._queue_pipeline_command("stop", previous_generation)
        self._start_position_timer()

        def _resolve():
            try:
                self._fetch_and_play(
                    vid,
                    track.get("title", ""),
                    track.get("artist", ""),
                    track.get("thumb"),
                    track.get("likeStatus", "INDIFFERENT"),
                    generation,
                )
            finally:
                self._stall_recovery_active = False

        threading.Thread(target=_resolve, name="ventapes-stream-recovery", daemon=True).start()

    def _start_position_timer(self):
        if self._position_timer_id or self._pipeline_shutdown:
            return
        self._position_timer_id = GObject.timeout_add(
            self._progress_interval_ms, self.update_position
        )

    def _stop_position_timer(self):
        source = self._position_timer_id
        self._position_timer_id = 0
        if source:
            try:
                GLib.source_remove(source)
            except Exception:
                pass

    def _restart_position_timer(self):
        self._stop_position_timer()
        self._start_position_timer()

    def update_position(self):
        import time

        now = time.time()
        monotonic_now = time.monotonic()
        if self._is_loading:
            if self._buffering_since and monotonic_now - self._buffering_since > 12.0:
                self._recover_stalled_stream("buffering timeout")
            return True
        if now - self.last_seek_time < 0.8:
            return True

        ret, state, pending = self.player.get_state(0)
        if state not in (Gst.State.PLAYING, Gst.State.PAUSED):
            # No reason to wake the main loop while stopped.  A later
            # STATE_CHANGED/PLAYING event starts the timer again.
            self._position_timer_id = 0
            return False

        if state == Gst.State.PLAYING:
            try:
                progress_ok, progress_ns = self.player.query_position(Gst.Format.TIME)
            except Exception:
                progress_ok, progress_ns = False, 0
            if self._buffering_since and monotonic_now - self._buffering_since > 12.0:
                self._recover_stalled_stream("buffering timeout")
                return True
            if (
                progress_ok
                and self._last_progress_at
                and abs(progress_ns - self._last_progress_position) < 100_000
                and monotonic_now - self._last_progress_at > 8.0
            ):
                self._recover_stalled_stream("position stalled")
                return True
            if progress_ok:
                if not self._last_progress_at or abs(
                    progress_ns - self._last_progress_position
                ) >= 100_000:
                    self._last_progress_at = monotonic_now
                    self._last_progress_position = progress_ns

        # Querying duration on every 100 ms tick is surprisingly expensive
        # for streaming sources.  It changes only when a source prerolls, so
        # once per second is sufficient; ASYNC_DONE/state changes reset the
        # probe immediately.
        monotonic_now = time.monotonic()
        if monotonic_now >= self._next_duration_probe:
            self._next_duration_probe = monotonic_now + 1.0
            new_dur = None
            success_dur, dur_nanos = self.player.query_duration(Gst.Format.TIME)
            if success_dur and dur_nanos > 0:
                new_dur = dur_nanos / Gst.SECOND

            if new_dur is not None:
                if abs(new_dur - self.duration) > 0.1:
                    self.duration = new_dur
                    if hasattr(self, "mpris_events"):
                        self.mpris_events.on_title()
                    if getattr(self, "discord_rpc", None):
                        self.discord_rpc.update()
            elif self.duration <= 0:
                # GStreamer doesn't know the length yet — use the track's
                # metadata so the seek bar has a range to drag inside.
                if 0 <= self.current_queue_index < len(self.queue):
                    track = self.queue[self.current_queue_index]
                    meta_dur = _parse_track_duration(track)
                    if meta_dur > 0:
                        self.duration = float(meta_dur)
                        if hasattr(self, "mpris_events"):
                            self.mpris_events.on_title()

        success_pos, pos_nanos = self.player.query_position(Gst.Format.TIME)
        if not success_pos:
            return True
        current_time = pos_nanos / Gst.SECOND
        duration = self.duration if self.duration > 0 else 0.0
        self._last_position_seconds = float(current_time)
        self._last_duration_seconds = float(duration)

        # The seek bar only needs a few updates per second.  Avoid waking
        # every connected view when the position has not materially moved.
        if (
            abs(current_time - self._last_emitted_position) >= 0.08
            or abs(duration - self._last_emitted_duration) >= 0.1
        ):
            self._last_emitted_position = current_time
            self._last_emitted_duration = duration
            if hasattr(self, "mpris_adapter"):
                self.mpris_adapter._last_pos = pos_nanos // 1000
            self.emit("progression", float(current_time), float(duration))

        # The scrobbler uses the real pipeline state and should still see
        # every timer tick; it performs no network work unless a threshold
        # or now-playing refresh is due.
        if getattr(self, "scrobbler", None):
            self.scrobbler.on_progress(
                float(current_time), float(duration), state == Gst.State.PLAYING
            )

        vid = getattr(self, "current_video_id", None)
        if (
            self._history_mode == "after_30s"
            and vid
            and self._history_recorded_for != vid
            and state == Gst.State.PLAYING
            and current_time >= self._history_record_after_sec
        ):
            self._history_recorded_for = vid
            print(
                f"[HISTORY] {self._history_record_after_sec}s threshold "
                f"hit for {vid} — recording play"
            )
            try:
                self.client.add_history_item_async(vid)
            except Exception as e:
                print(f"[HISTORY] failed to record {vid}: {e}")

        return True

    def _load_history_mode(self):
        """Read the user's history-recording preference. Defaults to
        "immediate" so we match YT Music's own behavior out of the box."""
        try:
            mode = read_prefs(user_prefs_path(), {}).get(
                "history_mode", "immediate"
            )
            return mode if mode in ("immediate", "after_30s", "never") else "immediate"
        except Exception:
            return "immediate"


    def set_history_mode(self, mode):
        """Update the history-recording mode at runtime so the
        preferences switch takes effect on the next track without
        needing a restart."""
        if mode not in ("immediate", "after_30s", "never"):
            return
        self._history_mode = mode

    def seek(self, position, flush=True):
        """Seek to position in seconds. Returns True on success, False on
        failure (e.g. the stream doesn't support range requests — common
        for YT Music upload-locker URLs)."""

        import time

        try:
            position = max(0.0, float(position))
        except (TypeError, ValueError):
            return False
        if self.duration > 0:
            position = min(position, float(self.duration))
        generation = self.load_generation
        state = self.player.get_state(0)[1]
        if state == Gst.State.NULL or self._is_loading:
            # A seek made while a new URI is prerolling is valid; park it
            # for the matching ASYNC_DONE instead of dropping it on the
            # floor.  The generation prevents it leaking onto a later track.
            self._pending_seek = (generation, position, bool(flush))
            return False

        self.last_seek_time = time.time()
        # Drop any spectrum entries queued before this seek — their
        # stream-times are now in the past (forward seek) or the future
        # (backward seek), either way they mislead pull_visualizer_bands.
        self._viz_queue.clear()

        seekable = False
        try:
            q = Gst.Query.new_seeking(Gst.Format.TIME)
            if self.player.query(q):
                _, seekable, _, _ = q.parse_seeking()
        except Exception:
            seekable = True  # be permissive — try anyway

        if not seekable:
            self._begin_seek_fallback(position)
            return False

        flags = Gst.SeekFlags.ACCURATE
        if flush:
            flags |= Gst.SeekFlags.FLUSH

        ok = self.player.seek_simple(
            Gst.Format.TIME,
            flags,
            int(position * Gst.SECOND),
        )

        # ACCURATE seeks sometimes get rejected by sources that would accept
        # a keyframe-aligned seek. Fall back to KEY_UNIT so we still move.
        if not ok:
            ok = self.player.seek_simple(
                Gst.Format.TIME,
                (Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.FLUSH) if flush else Gst.SeekFlags.KEY_UNIT,
                int(position * Gst.SECOND),
            )

        if not ok:
            print(
                f"[PLAYER] seek to {position:.1f}s rejected by pipeline "
                f"(seekable={seekable}, vid={self.current_video_id})"
            )
            self._begin_seek_fallback(position)
            return False

        self._pending_seek = None
        if hasattr(self, "mpris_events"):
            self.mpris_events.on_seek(int(position * 1_000_000))
        return True

    def _begin_seek_fallback(self, position):
        """Download the current track locally after a rejected seek."""

        vid = self.current_video_id
        if not vid:
            return
        # Already playing from a local file? Then the seek failure isn't about
        # range support and a re-download won't help — don't loop.
        if self._current_tmpfs_path:
            return
        gen = self.load_generation
        if self._seek_fallback_active:
            if self._seek_fallback_generation == gen:
                # A download is already in flight; keep the newest target.
                self._seek_after_load = position
            else:
                # The old download is being cancelled by the track change.
                # Do not lose this newer seek while its worker unwinds.
                self._pending_seek_fallback = (vid, position, gen)
                self._seek_after_load = position
            return

        self._start_seek_fallback(vid, position, gen)

    def _start_seek_fallback(self, vid, position, gen):
        if gen != self.load_generation or self._current_tmpfs_path:
            return
        self._mark_noseek(vid)
        self._seek_fallback_active = True
        self._seek_fallback_generation = gen
        self._seek_after_load = position
        self._pending_seek = None
        print(f"[PLAYER] seek fallback: downloading {vid} to local staging")

        def _worker():
            try:
                tmpfs_path = self._download_upload_to_tmpfs(vid, gen)
                if tmpfs_path and gen == self.load_generation:
                    GObject.idle_add(self._swap_to_tmpfs, tmpfs_path, vid, gen)
                elif tmpfs_path:
                    self._cleanup_tmpfs_path(tmpfs_path)
            finally:
                self._seek_fallback_active = False
                self._seek_fallback_generation = None
                pending = self._pending_seek_fallback
                self._pending_seek_fallback = None
                if pending and pending[2] == self.load_generation:
                    GObject.idle_add(
                        self._begin_seek_fallback, pending[1]
                    )

        threading.Thread(target=_worker, daemon=True).start()

    def _swap_to_tmpfs(self, tmpfs_path, vid, gen):
        """Main-thread adoption of a completed seek-fallback download."""

        return self._adopt_staged_path(
            tmpfs_path,
            vid,
            gen,
            source="local staged (non-seekable stream fallback)",
        )

    def get_stream_debug(self, full=False):
        """Build a human-readable snapshot of the current stream + pipeline
        for the "Stream Info (Debug)" panel. Everything pipeline-related is
        queried live at call time; the resolved-format fields come from the
        last load. The key seeking diagnostic is the SEEKING query's
        (seekable, start, end): if `end` is below the current position, or
        the protocol/source isn't byte-range capable, that's why a seek gets
        rejected even though `seekable=True`."""
        lines = []

        def _fmt_ns(ns):
            if ns is None or ns < 0:
                return "unknown"
            s = ns / Gst.SECOND
            return f"{int(s // 60)}:{int(s % 60):02d} ({s:.1f}s)"

        d = self._stream_debug or {}
        lines.append(f"Track:     {self.current_video_id or '—'}")
        lines.append(f"Source:    {d.get('source', 'unknown')}")
        if d.get("itag"):
            lines.append(
                f"Format:    itag {d.get('itag')} · {d.get('protocol')} · "
                f"{d.get('ext')} · {d.get('acodec')} · {d.get('abr')}kbps"
            )
        elif d.get("path"):
            lines.append(f"File:      {d.get('path')}")

        # Source element playbin actually built (souphttpsrc / filesrc / …).
        lines.append(f"Src elem:  {self._source_factory_name or 'unknown'}")

        uri = self._current_play_uri or ""
        if uri:
            # Truncated for the on-screen panel (these signed URLs are ~600
            # chars); the Copy button passes full=True for the whole thing.
            shown = uri if (full or len(uri) <= 96) else uri[:96] + "…"
            lines.append(f"URI:       {shown}")

        # Live pipeline state.
        try:
            _, state, _pending = self.player.get_state(0)
            lines.append(f"State:     {state.value_nick}")
        except Exception:
            lines.append("State:     unknown")

        pos_ns = dur_ns = None
        try:
            ok_p, pos_ns = self.player.query_position(Gst.Format.TIME)
            if not ok_p:
                pos_ns = None
        except Exception:
            pos_ns = None
        try:
            ok_d, dur_ns = self.player.query_duration(Gst.Format.TIME)
            if not ok_d:
                dur_ns = None
        except Exception:
            dur_ns = None
        lines.append(f"Position:  {_fmt_ns(pos_ns)}")
        lines.append(f"Duration:  {_fmt_ns(dur_ns)}")

        # The decisive seek diagnostic.
        try:
            q = Gst.Query.new_seeking(Gst.Format.TIME)
            if self.player.query(q):
                _fmt, seekable, seg_start, seg_end = q.parse_seeking()
                lines.append(f"Seekable:  {bool(seekable)}")
                lines.append(
                    f"Seek range: {_fmt_ns(seg_start)}  →  {_fmt_ns(seg_end)}"
                )
            else:
                lines.append("Seekable:  query failed")
        except Exception as e:
            lines.append(f"Seekable:  error ({e})")

        return "\n".join(lines)

    def get_volume(self):
        """Get volume in cubic (perceptual) scale 0.0-1.0, matching system mixer."""
        linear = self.player.get_property("volume")
        return GstAudio.StreamVolume.convert_volume(
            GstAudio.StreamVolumeFormat.LINEAR,
            GstAudio.StreamVolumeFormat.CUBIC,
            linear,
        )

    def set_volume(self, value):
        """Set volume from cubic (perceptual) scale 0.0-1.0."""
        self._user_volume = float(value)
        linear = GstAudio.StreamVolume.convert_volume(
            GstAudio.StreamVolumeFormat.CUBIC,
            GstAudio.StreamVolumeFormat.LINEAR,
            float(value),
        )
        self._internal_volume_change = True
        self.player.set_property("volume", linear)
        self._internal_volume_change = False
        if value > 0 and self.get_mute():
            self.set_mute(False)
        else:
            GLib.idle_add(self.emit, "volume-changed", float(value), self.get_mute())

    def get_mute(self):
        return self.player.get_property("mute")

    def set_mute(self, is_muted):
        self._internal_volume_change = True
        self.player.set_property("mute", is_muted)
        self._internal_volume_change = False
        GLib.idle_add(self.emit, "volume-changed", self.get_volume(), is_muted)

    def _on_external_volume_change(self, element, param):
        """Called when volume changes externally (system mixer)."""
        if self._internal_volume_change:
            return

        # wireplumber is an external call, which should set the volume of last session
        # ensure volume value from external volume change is always listened to if _user_volume is still not set
        # just storing the value from the call, it will be used whenever
        if self._user_volume == None:
            linear = float(element.get_property("volume"))
            self._user_volume = GstAudio.StreamVolume.convert_volume(
                GstAudio.StreamVolumeFormat.LINEAR,
                GstAudio.StreamVolumeFormat.CUBIC,
                linear,
            )

        # During track loads, playbin can rebuild its audio sink and briefly
        # report the new sink's default volume. Ignore those spurious notifies
        # so the UI doesn't snap to 100%; the real value is restored once the
        # pipeline reaches PLAYING (see on_message).
        if self._is_loading:
            return
        GLib.idle_add(self.emit, "volume-changed", self.get_volume(), self.get_mute())

    def _on_external_mute_change(self, element, param):
        """Called when mute changes externally."""
        if self._internal_volume_change:
            return
        GLib.idle_add(self.emit, "volume-changed", self.get_volume(), self.get_mute())
