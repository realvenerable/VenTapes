"""Scrobbling to Last.fm and ListenBrainz.

All network I/O runs on one worker thread. Scrobbles that fail are kept on
disk and retried, so a dropped connection does not lose plays.
"""

import hashlib
import json
import os
import queue
import threading
import time
import tempfile
from urllib.parse import urlencode

from version import APP_VERSION
from ui.preferences import get_bool, read_prefs, user_prefs_path


# Last.fm requires client credentials. VenTapes deliberately does not reuse
# the upstream Mixtapes key/secret; register your own app and provide both
# values at runtime. ListenBrainz needs no app credentials and remains usable.
LASTFM_API_KEY = os.environ.get("VENTAPES_LASTFM_API_KEY", "").strip()
LASTFM_API_SECRET = os.environ.get("VENTAPES_LASTFM_API_SECRET", "").strip()

LASTFM_API_ROOT = "https://ws.audioscrobbler.com/2.0/"
LASTFM_AUTH_URL = "https://www.last.fm/api/auth/"
LISTENBRAINZ_API_ROOT = "https://api.listenbrainz.org"

SERVICES = ("lastfm", "listenbrainz")
SERVICE_LABELS = {"lastfm": "Last.fm", "listenbrainz": "ListenBrainz"}

USER_AGENT = f"VenTapes/{APP_VERSION} (+https://github.com/realvenerable/VenTapes)"

# Last.fm's submission rules: never scrobble a track under 30 seconds, and
# submit once half the track or 4 minutes has played, whichever is first.
MIN_TRACK_LENGTH = 30.0
SCROBBLE_CAP_SECONDS = 240.0
# Streams that never report a length (YT upload-locker URLs) fall back to a
# flat threshold instead of being dropped.
UNKNOWN_DURATION_THRESHOLD = 120.0

# Cap the offline backlog so a long outage cannot grow the file without bound.
MAX_PENDING = 500
# Give up on an entry that keeps failing instead of retrying it forever.
MAX_ATTEMPTS = 10
# Retry the backlog on this cadence while the worker is otherwise idle.
RETRY_INTERVAL = 300.0
# Both APIs take batches. 50 is Last.fm's per-request scrobble limit.
BATCH_SIZE = 50
# Refresh "now playing" no more than once per this many seconds.
NOW_PLAYING_INTERVAL = 30.0

NETWORK_TIMEOUT = 15

# Last.fm answers HTTP 200 with no error for a scrobble it then discards, so
# a submission is not proof of acceptance. These are the reasons it gives.
LASTFM_IGNORE_REASONS = {
    "1": "artist name ignored",
    "2": "track name ignored",
    "3": "timestamp too old",
    "4": "timestamp too far in the future",
    "5": "daily scrobble limit reached",
}


class TransientError(Exception):
    """A failure worth retrying: network down, rate limit, 5xx."""


class PermanentError(Exception):
    """A failure that retrying will not fix."""


class AuthError(PermanentError):
    """The stored credential was rejected. The user has to reconnect."""


def _data_dir():
    from gi.repository import GLib

    return os.path.join(GLib.get_user_data_dir(), "ventapes")


def _read_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[SCROBBLE] failed to read {os.path.basename(path)}: {e}")
    return default


def _write_json(path, data, private=False):
    temporary_path = None
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if private and os.name == "posix":
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        # Always use a unique sibling temporary file.  The old fixed
        # ``path + ".tmp"`` name let the UI thread and the scrobbler worker
        # replace/remove the same file concurrently, which could lose a
        # backlog update or leave a half-written queue behind.
        fd, temporary_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}-", suffix=".tmp", dir=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        if private and os.name == "posix":
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except Exception as e:
        print(f"[SCROBBLE] failed to write {os.path.basename(path)}: {type(e).__name__}")
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _get_prefs():
    return read_prefs(user_prefs_path(), {})


def get_scrobble_enabled():
    return get_bool(_get_prefs(), "scrobble_enabled", True)


def get_now_playing_enabled():
    return get_bool(_get_prefs(), "scrobble_now_playing", True)


def _load_creds():
    # Kept out of prefs.json on purpose. Session keys and user tokens are
    # credentials, and users paste prefs.json into bug reports.
    return _read_json(os.path.join(_data_dir(), "scrobbler.json"), {})


def _save_creds(creds):
    _write_json(os.path.join(_data_dir(), "scrobbler.json"), creds, private=True)


def _load_pending():
    data = _read_json(os.path.join(_data_dir(), "scrobble_queue.json"), {})
    return {s: list(data.get(s) or []) for s in SERVICES}


def _save_pending(pending):
    _write_json(os.path.join(_data_dir(), "scrobble_queue.json"), pending)


def _sign(params):
    """Last.fm api_sig: md5 of every key and value concatenated in key
    order, with the shared secret appended."""
    parts = [
        f"{k}{v}"
        for k, v in sorted(params.items())
        if k not in ("format", "callback")
    ]
    raw = "".join(parts) + LASTFM_API_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class ScrobblerAdapter:
    """Tracks listening time and submits plays to the connected services.

    The GTK thread only reads state and pushes work onto a queue. The worker
    thread owns the pending backlog and every network call.
    """

    def __init__(self, player):
        self.player = player
        self.last_error = ""
        self._lock = threading.RLock()
        # Lifecycle state is separate from the pending/credential lock.  It
        # serializes the check-and-start of the worker with preference
        # toggles, authentication callbacks, and shutdown.
        self._lifecycle_lock = threading.RLock()
        self._queue = queue.Queue()
        self._local = threading.local()
        self._stopping = False
        self._explicit_stop = False
        self._restart_requested = False
        self._creds = _load_creds()
        self._pending = _load_pending()
        self._enabled = get_scrobble_enabled()
        self._now_playing_enabled = get_now_playing_enabled()
        self._cur = None
        self._last_flush = time.monotonic()

        self._worker = None
        # Most installations never connect a scrobbling service.  Start the
        # worker lazily so an unused feature does not keep a thread (and its
        # HTTP/session machinery) alive for the whole session.
        if self._enabled and (self.pending_count() or any(self._creds.values())):
            self._ensure_worker()
        if self.pending_count():
            self._queue.put(("flush", None))

    def _start_worker_locked(self):
        """Start the sole worker while ``_lifecycle_lock`` is held."""

        if not self._enabled or self._explicit_stop:
            return
        self._stopping = False
        self._restart_requested = False
        worker = threading.Thread(
            target=self._run, daemon=True, name="scrobbler"
        )
        self._worker = worker
        try:
            worker.start()
        except Exception:
            self._worker = None
            raise

    def _ensure_worker(self):
        # Authentication callbacks and preference changes can arrive on
        # different threads.  Keep the liveness check and thread start under
        # one lock so two workers can never own the same persisted backlog.
        with self._lifecycle_lock:
            if not self._enabled or self._explicit_stop:
                return
            worker = self._worker
            if worker is not None and worker.is_alive():
                if self._stopping:
                    # A pause/shutdown is unwinding.  The old worker will
                    # either continue after seeing the re-enable or restart
                    # itself from its finalizer.
                    self._restart_requested = True
                return
            self._start_worker_locked()

    # -- State queried by the preferences UI -----------------------------

    def lastfm_configured(self):
        """False when the build ships without Last.fm API credentials."""
        return bool(LASTFM_API_KEY and LASTFM_API_SECRET)

    def is_connected(self, service):
        return bool(self._credential(service))

    def username(self, service):
        with self._lock:
            return str((self._creds.get(service) or {}).get("username") or "")

    def pending_count(self, service=None):
        with self._lock:
            if service:
                return len(self._pending.get(service) or [])
            return sum(len(v or []) for v in self._pending.values())

    def get_enabled(self):
        with self._lifecycle_lock:
            return self._enabled

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        worker = None
        with self._lifecycle_lock:
            if self._explicit_stop:
                return
            self._enabled = enabled
            if not enabled:
                worker = self._worker
                if worker is not None and worker.is_alive():
                    # Do not let an already queued operation flush while the
                    # user has disabled scrobbling.  The worker drains any
                    # scrobble payloads into the persistent backlog before it
                    # exits and closes its HTTP session.
                    self._stopping = True
                    self._restart_requested = False
                    self._queue.put(("pause", None))
                if worker is None or not worker.is_alive():
                    self._drain_disabled_queue()
                return

            self._restart_requested = True
            worker = self._worker
            if worker is not None and worker.is_alive():
                # A pause may be in flight.  Let that worker see the new
                # enabled state and continue, or restart from its finalizer.
                self._stopping = False
            if self.pending_count() or any(self._creds.values()):
                self._queue.put(("flush", None))
            self._ensure_worker()

    def set_now_playing_enabled(self, enabled):
        with self._lifecycle_lock:
            self._now_playing_enabled = bool(enabled)

    def stop(self):
        with self._lifecycle_lock:
            self._explicit_stop = True
            self._restart_requested = False
            self._stopping = True
            worker = self._worker
            if worker is not None and worker.is_alive():
                self._queue.put(("stop", None))
        if worker is None or not worker.is_alive():
            # There may be a queue left from a disabled period.  Persist its
            # scrobble payloads before the adapter is discarded.
            self._drain_disabled_queue()

    # -- Player events ---------------------------------------------------

    def on_track_started(self, video_id, title, artist, album="", duration=0.0):
        """A new play began. Resets the listening clock."""
        with self._lock:
            self._cur = {
                "video_id": str(video_id or ""),
                "track": str(title or "").strip(),
                "artist": str(artist or "").strip(),
                "album": str(album or "").strip(),
                "duration": float(duration or 0.0),
                # Filled when playback actually reaches PLAYING, so the
                # timestamp reflects the listen and not the load.
                "timestamp": None,
                "elapsed": 0.0,
                "playing_since": None,
                "scrobbled": False,
                "now_playing_at": None,
            }

    def refine_current_track(self, video_id, title, artist):
        """Correct the in-flight track's metadata without touching the
        clock. The OMV to ATV swap lands after playback starts and gives
        the audio version's real title."""
        title = str(title or "").strip()
        artist = str(artist or "").strip()
        with self._lock:
            cur = self._cur
            if not cur or cur["scrobbled"]:
                return
            if title:
                cur["track"] = title
            if artist:
                cur["artist"] = artist
            if video_id:
                cur["video_id"] = str(video_id)

    def on_state_changed(self, state):
        """Only ever stops the clock. Starting it is the progress tick's job,
        because a state transition is not guaranteed to arrive."""
        if state == "playing":
            return
        with self._lock:
            cur = self._cur
            if cur and cur["playing_since"] is not None:
                cur["elapsed"] += time.monotonic() - cur["playing_since"]
                cur["playing_since"] = None

    def on_progress(self, position, duration, is_playing):
        """Drives the listening clock. Called on every position tick with the
        pipeline's real state.

        The clock cannot hang off "state-changed": GStreamer flushes the bus
        when the pipeline goes to NULL between tracks, so a queue played
        straight through never reports a transition back into playing.
        """
        now_playing = None
        payload = None
        with self._lock:
            cur = self._cur
            if not cur:
                return
            now = time.monotonic()

            if is_playing:
                if cur["playing_since"] is None:
                    cur["playing_since"] = now
                if cur["timestamp"] is None:
                    cur["timestamp"] = int(time.time())
                # Last.fm expires a now-playing update on its own, so refresh
                # it periodically while the track runs.
                last_sent = cur["now_playing_at"]
                if last_sent is None or now - last_sent > NOW_PLAYING_INTERVAL:
                    cur["now_playing_at"] = now
                    now_playing = dict(cur)
            elif cur["playing_since"] is not None:
                cur["elapsed"] += now - cur["playing_since"]
                cur["playing_since"] = None

            if not cur["scrobbled"]:
                # GStreamer often learns the real length a few ticks in.
                if duration and duration > cur["duration"]:
                    cur["duration"] = float(duration)
                elapsed = cur["elapsed"]
                if cur["playing_since"] is not None:
                    elapsed += now - cur["playing_since"]
                if elapsed >= self._threshold(cur["duration"]):
                    cur["scrobbled"] = True
                    payload = dict(cur)

        if now_playing:
            self._queue_now_playing(now_playing)
        if payload:
            self._queue_scrobble(payload)

    def _threshold(self, duration):
        if duration <= 0:
            return UNKNOWN_DURATION_THRESHOLD
        return min(duration / 2.0, SCROBBLE_CAP_SECONDS)

    def _entry(self, cur):
        return {
            "artist": cur["artist"],
            "track": cur["track"],
            "album": cur["album"],
            "video_id": cur["video_id"],
            "timestamp": int(cur["timestamp"] or time.time()),
            "duration": int(cur["duration"]) if cur["duration"] > 0 else 0,
            "attempts": 0,
        }

    def _playable(self, cur):
        with self._lifecycle_lock:
            if not self._enabled or self._stopping or self._explicit_stop:
                return False
        if not cur["track"] or not cur["artist"]:
            return False
        # A missing duration is unknown, not short, so only reject a known
        # length below the floor.
        if 0 < cur["duration"] < MIN_TRACK_LENGTH:
            return False
        return any(self.is_connected(s) for s in SERVICES)

    def _queue_now_playing(self, cur):
        if not self._now_playing_enabled or not self._playable(cur):
            return
        self._queue.put(("now_playing", self._entry(cur)))

    def _queue_scrobble(self, cur):
        if not self._playable(cur):
            return
        print(f"[SCROBBLE] queueing {cur['artist']} - {cur['track']}")
        self._queue.put(("scrobble", self._entry(cur)))

    # -- Worker ----------------------------------------------------------

    def _drain_disabled_queue(self):
        """Persist queued scrobbles while no network worker is allowed to run.

        A scrobble can have been queued just before the preference switch.
        Dropping that in-memory payload would lose a listen even though the
        persisted backlog is intact, so consume the queue locally and append
        only scrobble entries.  Flush/now-playing commands are intentionally
        discarded while disabled.
        """

        saw_scrobble = False
        while True:
            try:
                op, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if op == "scrobble":
                self._append_pending(payload)
                saw_scrobble = True
            elif op == "stop":
                with self._lifecycle_lock:
                    self._explicit_stop = True
                    self._stopping = True
                break
            # pause/now_playing/flush have no useful work to do while the
            # adapter is disabled.  A later enable will enqueue a fresh flush.
        return saw_scrobble

    def _run(self):
        try:
            while True:
                try:
                    op, payload = self._queue.get(timeout=30.0)
                except queue.Empty:
                    with self._lifecycle_lock:
                        should_stop = (
                            self._explicit_stop
                            or not self._enabled
                            or self._stopping
                        )
                    if should_stop:
                        self._drain_disabled_queue()
                        break
                    idle = time.monotonic() - self._last_flush
                    if self.pending_count() and idle > RETRY_INTERVAL:
                        self._flush()
                    continue

                if op == "stop":
                    self._drain_disabled_queue()
                    break
                if op == "pause":
                    self._drain_disabled_queue()
                    with self._lifecycle_lock:
                        if self._enabled and not self._explicit_stop:
                            # The user may have toggled scrobbling back on
                            # while the pause command was in flight.
                            self._stopping = False
                        else:
                            self._stopping = True
                            break
                    self._flush()
                    continue

                with self._lifecycle_lock:
                    active = (
                        self._enabled
                        and not self._stopping
                        and not self._explicit_stop
                    )
                if not active:
                    if op == "scrobble":
                        self._append_pending(payload)
                    continue
                if op == "now_playing":
                    self._do_now_playing(payload)
                elif op == "scrobble":
                    self._append_pending(payload)
                    self._flush()
                elif op == "flush":
                    self._flush()
        finally:
            self._close_thread_session()
            with self._lifecycle_lock:
                if self._worker is threading.current_thread():
                    self._worker = None
                    if (
                        self._enabled
                        and not self._explicit_stop
                        and self._restart_requested
                    ):
                        self._start_worker_locked()

    def _do_now_playing(self, entry):
        senders = {
            "lastfm": self._lastfm_now_playing,
            "listenbrainz": self._listenbrainz_now_playing,
        }
        for service, send in senders.items():
            if not self.is_connected(service):
                continue
            try:
                send(entry)
            except AuthError as e:
                self._handle_auth_error(service, e)
            except Exception as e:
                # Now-playing is cosmetic and expires anyway, so a failure
                # is logged and dropped rather than queued.
                print(f"[SCROBBLE] {service} now-playing failed: {e}")

    def _append_pending(self, entry):
        with self._lock:
            for service in SERVICES:
                if not self.is_connected(service):
                    continue
                bucket = self._pending.setdefault(service, [])
                bucket.append(dict(entry))
                if len(bucket) > MAX_PENDING:
                    del bucket[:-MAX_PENDING]
            snapshot = json.loads(json.dumps(self._pending))
            # Keep the state snapshot and its replacement ordered with
            # disconnects and other worker writes.  A unique temp file avoids
            # corruption; this lock prevents an older snapshot from winning
            # after a newer state transition.
            _save_pending(snapshot)

    def _flush(self):
        with self._lifecycle_lock:
            if not self._enabled or self._stopping or self._explicit_stop:
                return
        self._last_flush = time.monotonic()
        senders = {
            "lastfm": self._lastfm_scrobble,
            "listenbrainz": self._listenbrainz_scrobble,
        }
        for service, send in senders.items():
            if not self.is_connected(service):
                continue
            while True:
                with self._lifecycle_lock:
                    if not self._enabled or self._stopping or self._explicit_stop:
                        break
                with self._lock:
                    batch = list((self._pending.get(service) or [])[:BATCH_SIZE])
                if not batch:
                    break
                try:
                    send(batch)
                except AuthError as e:
                    self._handle_auth_error(service, e)
                    break
                except TransientError as e:
                    self.last_error = f"{SERVICE_LABELS[service]}: {e}"
                    print(f"[SCROBBLE] {service} retry later: {e}")
                    break
                except Exception as e:
                    self.last_error = f"{SERVICE_LABELS[service]}: {e}"
                    print(f"[SCROBBLE] {service} rejected a batch: {e}")
                    self._penalize(service, len(batch))
                    break
                else:
                    self._drop_sent(service, len(batch))
                    print(f"[SCROBBLE] {service} accepted {len(batch)} listen(s)")

    def _penalize(self, service, count):
        """Count a failed attempt and drop entries that keep being refused,
        so one bad play cannot wedge the backlog forever."""
        with self._lock:
            bucket = self._pending.get(service) or []
            for item in bucket[:count]:
                item["attempts"] = int(item.get("attempts") or 0) + 1
            kept = [i for i in bucket if int(i.get("attempts") or 0) < MAX_ATTEMPTS]
            dropped = len(bucket) - len(kept)
            self._pending[service] = kept
            snapshot = json.loads(json.dumps(self._pending))
            if dropped:
                print(f"[SCROBBLE] dropped {dropped} unsendable {service} listen(s)")
            _save_pending(snapshot)

    def _drop_sent(self, service, count):
        with self._lock:
            bucket = self._pending.get(service) or []
            self._pending[service] = bucket[count:]
            snapshot = json.loads(json.dumps(self._pending))
            _save_pending(snapshot)

    def _handle_auth_error(self, service, error):
        print(f"[SCROBBLE] {service} credentials rejected: {error}")
        self.disconnect(service)
        # Set after disconnect, which clears the field, so the preferences
        # row can explain why the service dropped out.
        self.last_error = f"{SERVICE_LABELS[service]}: {error}"

    # -- HTTP ------------------------------------------------------------

    def _session(self):
        """One requests.Session per thread. The pool is deliberately tiny
        because the app runs close to the 1024 FD soft limit."""
        session = getattr(self._local, "http", None)
        if session is None:
            import requests
            from requests.adapters import HTTPAdapter

            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            adapter = HTTPAdapter(
                pool_connections=2, pool_maxsize=4, max_retries=0
            )
            session.mount("https://", adapter)
            self._local.http = session
        return session

    def close_thread_session(self):
        """Drop this thread's HTTP connection. Callers that poll in a loop
        use it once they are done instead of per attempt."""
        self._close_thread_session()

    def _close_thread_session(self):
        session = getattr(self._local, "http", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
            self._local.http = None

    def _credential(self, service):
        with self._lock:
            entry = self._creds.get(service) or {}
            if service == "lastfm":
                if not self.lastfm_configured():
                    return ""
                return str(entry.get("session_key") or "")
            return str(entry.get("token") or "")

    # -- Last.fm ---------------------------------------------------------

    def _lastfm_call(self, method, params, post=False, session_key=None):
        payload = {str(k): str(v) for k, v in params.items()}
        payload["method"] = method
        payload["api_key"] = LASTFM_API_KEY
        if session_key:
            payload["sk"] = session_key
        payload["api_sig"] = _sign(payload)
        payload["format"] = "json"

        session = self._session()
        try:
            if post:
                response = session.post(
                    LASTFM_API_ROOT, data=payload, timeout=NETWORK_TIMEOUT
                )
            else:
                response = session.get(
                    LASTFM_API_ROOT, params=payload, timeout=NETWORK_TIMEOUT
                )
        except Exception as e:
            raise TransientError(str(e))

        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise TransientError(f"unreadable response (HTTP {response.status_code})")

        if isinstance(data, dict) and data.get("error"):
            code = int(data.get("error") or 0)
            message = str(data.get("message") or "unknown error")
            # 4/9/14/15 are all "this credential is no longer good".
            if code in (4, 9, 14, 15):
                raise AuthError(message)
            # 8 operation failed, 11 service offline, 16 unavailable,
            # 29 rate limited.
            if code in (8, 11, 16, 29):
                raise TransientError(message)
            raise PermanentError(f"{message} (code {code})")
        return data

    def _report_ignored(self, data):
        """Log whatever Last.fm accepted then dropped. Without this a silently
        discarded scrobble is indistinguishable from a successful one."""
        try:
            body = (data or {}).get("scrobbles") or (data or {}).get("nowplaying")
            if not body:
                return 0
            entries = body.get("scrobble") or body
            if isinstance(entries, dict):
                entries = [entries]
            count = 0
            for item in entries:
                if not isinstance(item, dict):
                    continue
                message = item.get("ignoredMessage") or {}
                code = str(message.get("code") or "0")
                if code == "0":
                    continue
                count += 1
                reason = LASTFM_IGNORE_REASONS.get(
                    code, str(message.get("#text") or "unknown reason")
                )
                name = (item.get("track") or {}).get("#text") or "?"
                artist = (item.get("artist") or {}).get("#text") or "?"
                print(f"[SCROBBLE] lastfm discarded {artist} - {name}: {reason}")
                self.last_error = f"Last.fm: {reason}"
            return count
        except Exception as e:
            print(f"[SCROBBLE] could not read the lastfm response: {e}")
            return 0

    def _lastfm_now_playing(self, entry):
        params = {"artist": entry["artist"], "track": entry["track"]}
        if entry.get("album"):
            params["album"] = entry["album"]
        if entry.get("duration"):
            params["duration"] = int(entry["duration"])
        self._report_ignored(
            self._lastfm_call(
                "track.updateNowPlaying",
                params,
                post=True,
                session_key=self._credential("lastfm"),
            )
        )

    def _lastfm_scrobble(self, batch):
        params = {}
        for i, item in enumerate(batch):
            params[f"artist[{i}]"] = item["artist"]
            params[f"track[{i}]"] = item["track"]
            params[f"timestamp[{i}]"] = int(item["timestamp"])
            if item.get("album"):
                params[f"album[{i}]"] = item["album"]
            if item.get("duration"):
                params[f"duration[{i}]"] = int(item["duration"])
        self._report_ignored(
            self._lastfm_call(
                "track.scrobble",
                params,
                post=True,
                session_key=self._credential("lastfm"),
            )
        )

    def lastfm_request_token(self):
        """Start the desktop auth flow. Returns (token, url) for the caller
        to open in a browser."""
        if not self.lastfm_configured():
            raise PermanentError("This build has no Last.fm API credentials")
        try:
            data = self._lastfm_call("auth.getToken", {})
            token = str((data or {}).get("token") or "")
            if not token:
                raise PermanentError("Last.fm returned no token")
            query = urlencode({"api_key": LASTFM_API_KEY, "token": token})
            return token, f"{LASTFM_AUTH_URL}?{query}"
        finally:
            self._close_thread_session()

    def lastfm_finish_auth(self, token):
        """Trade an authorized token for a session key. Raises AuthError
        while the user has not pressed Allow yet. The caller polls this, so
        it leaves the HTTP session open for close_thread_session()."""
        data = self._lastfm_call("auth.getSession", {"token": token})
        session = (data or {}).get("session") or {}
        key = str(session.get("key") or "")
        if not key:
            raise PermanentError("Last.fm returned no session")
        name = str(session.get("name") or "")
        self._store_credentials("lastfm", {"session_key": key, "username": name})
        return name

    # -- ListenBrainz ----------------------------------------------------

    def _listenbrainz_metadata(self, entry):
        info = {
            "media_player": "VenTapes",
            "submission_client": "VenTapes",
            "music_service": "music.youtube.com",
        }
        if entry.get("duration"):
            info["duration_ms"] = int(entry["duration"]) * 1000
        if entry.get("video_id"):
            info["origin_url"] = (
                f"https://music.youtube.com/watch?v={entry['video_id']}"
            )
        metadata = {
            "artist_name": entry["artist"],
            "track_name": entry["track"],
            "additional_info": info,
        }
        if entry.get("album"):
            metadata["release_name"] = entry["album"]
        return metadata

    def _listenbrainz_post(self, body):
        session = self._session()
        try:
            response = session.post(
                f"{LISTENBRAINZ_API_ROOT}/1/submit-listens",
                json=body,
                headers={"Authorization": f"Token {self._credential('listenbrainz')}"},
                timeout=NETWORK_TIMEOUT,
            )
        except Exception as e:
            raise TransientError(str(e))

        if response.status_code == 200:
            return
        if response.status_code in (401, 403):
            raise AuthError("user token rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"HTTP {response.status_code}")
        raise PermanentError(
            f"HTTP {response.status_code}: {response.text[:200]}"
        )

    def _listenbrainz_now_playing(self, entry):
        self._listenbrainz_post({
            "listen_type": "playing_now",
            "payload": [{"track_metadata": self._listenbrainz_metadata(entry)}],
        })

    def _listenbrainz_scrobble(self, batch):
        self._listenbrainz_post({
            "listen_type": "single" if len(batch) == 1 else "import",
            "payload": [
                {
                    "listened_at": int(item["timestamp"]),
                    "track_metadata": self._listenbrainz_metadata(item),
                }
                for item in batch
            ],
        })

    def listenbrainz_connect(self, token):
        """Validate a user token and store it. Returns the ListenBrainz
        username."""
        token = str(token or "").strip()
        if not token:
            raise PermanentError("Enter your ListenBrainz user token")
        session = self._session()
        try:
            response = session.get(
                f"{LISTENBRAINZ_API_ROOT}/1/validate-token",
                headers={"Authorization": f"Token {token}"},
                timeout=NETWORK_TIMEOUT,
            )
            if response.status_code in (401, 403):
                raise AuthError("that token is not valid")
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientError(f"HTTP {response.status_code}")
            data = response.json()
            if not data.get("valid"):
                raise AuthError(str(data.get("message") or "that token is not valid"))
            name = str(data.get("user_name") or "")
            self._store_credentials("listenbrainz", {"token": token, "username": name})
            return name
        except (AuthError, TransientError, PermanentError):
            raise
        except Exception as e:
            raise TransientError(str(e))
        finally:
            self._close_thread_session()

    # -- Credential storage ----------------------------------------------

    def _store_credentials(self, service, entry):
        with self._lock:
            self._creds[service] = entry
            snapshot = json.loads(json.dumps(self._creds))
            _save_creds(snapshot)
        self.last_error = ""
        if self.get_enabled():
            self._ensure_worker()
            self._queue.put(("flush", None))

    def disconnect(self, service):
        self.last_error = ""
        with self._lock:
            self._creds.pop(service, None)
            self._pending[service] = []
            creds = json.loads(json.dumps(self._creds))
            pending = json.loads(json.dumps(self._pending))
            _save_creds(creds)
            _save_pending(pending)
