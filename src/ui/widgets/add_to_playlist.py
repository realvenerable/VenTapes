"""Popover that replaces the old Gio.Menu submenu for picking a playlist to
add tracks to. Adds: cover thumbnails, type-to-search, recently-used sorting,
and a fixed max height so the user with 200 playlists isn't scrolling the
whole window."""

import json
import os
import threading
import time

from gi.repository import Gtk, GLib, Gdk
from ui.util_classes import ScrolledWindow

# CSS provider for this widget — installed once into the default display so
# every popover instance picks up the styling. Keeps the rounding subtle and
# trims the listbox row padding that the theme adds by default.
_CSS_INSTALLED = False


def _install_css():
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    css = b"""
    .add-to-playlist-cover {
        border-radius: 4px;
    }
    .add-to-playlist-list > row {
        padding: 4px 6px;
    }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _CSS_INSTALLED = True


# Persisted at ~/.cache/ventapes/playlist_recents.json so the ordering survives
# restarts. {playlist_id: last_used_unix_ts}. Best-effort — any I/O failure
# just falls back to alphabetical sort.
_RECENTS_LOCK = threading.Lock()


def _recents_path():
    return os.path.join(
        GLib.get_user_cache_dir(), "ventapes", "playlist_recents.json"
    )


def _load_recents():
    path = _recents_path()
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_recents(data):
    path = _recents_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def mark_playlist_used(playlist_id):
    """Bump a playlist's last-used timestamp. Call after a successful
    add-to-playlist operation."""
    if not playlist_id:
        return
    with _RECENTS_LOCK:
        data = _load_recents()
        data[playlist_id] = int(time.time())
        # Cap stored entries so the file doesn't grow forever for long-time
        # users — 200 is plenty to keep the surfaces useful.
        if len(data) > 200:
            sorted_items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
            data = dict(sorted_items[:200])
        _save_recents(data)


class AddToPlaylistPopover(Gtk.Popover):
    """Anchored popover with search, recently-used sorting, covers, capped
    height. `on_select(playlist_id)` fires when the user picks one.

    Pass `parent` (the widget the popover should anchor to) and call
    `popup()` after creation."""

    def __init__(self, player, on_select, parent=None):
        super().__init__()
        _install_css()
        self.player = player
        self.client = player.client
        self.on_select = on_select
        self._filter_text = ""
        self._rows = []  # parallel list to listbox children, kept for filtering
        self.set_has_arrow(True)
        self.set_autohide(True)
        if parent:
            self.set_parent(parent)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_size_request(320, -1)
        # No margins on the container — the popover already provides its
        # own chrome; doubling the padding made the popover look bloated.

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search playlists…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        outer.append(self.search_entry)

        # ScrolledWindow with a capped height — natural height grows with the
        # list up to max_content_height, then scrolls. Without the cap, a
        # user with 200 playlists got a popover taller than their screen.
        scrolled = ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_min_content_height(120)
        scrolled.set_max_content_height(360)

        self.listbox = Gtk.ListBox()
        # Don't add "boxed-list" — its borders/separators look heavy inside
        # a popover that already has its own background and rounding. Our
        # own `.add-to-playlist-list` class tightens the row padding the
        # theme adds (default ~12px each side).
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.add_css_class("add-to-playlist-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.set_filter_func(self._row_filter)
        self.listbox.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self.listbox)
        outer.append(scrolled)

        self.empty_label = Gtk.Label(label="No playlists")
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_margin_top(12)
        self.empty_label.set_margin_bottom(12)
        self.empty_label.set_visible(False)
        outer.append(self.empty_label)

        self.set_child(outer)

        self._populate()

    def _populate(self):
        playlists = list(self.client.get_editable_playlists() or [])
        if not playlists:
            self.empty_label.set_visible(True)
            return

        recents = _load_recents()
        # ytmusicapi returns the user's library in YT's own most-recently-
        # modified order. We use that as the fallback ordering for playlists
        # the user hasn't interacted with from inside the app yet — sorting
        # alphabetically here would override the user's real activity from
        # everywhere else (YouTube web, mobile app, etc).
        api_order = {
            (p.get("playlistId") or ""): idx for idx, p in enumerate(playlists)
        }

        def sort_key(p):
            pid = p.get("playlistId") or ""
            ts = recents.get(pid, 0)
            # Negative ts so larger (more recent) ts comes first; for the
            # never-touched-in-app cohort, ts is 0 and we fall back to the
            # API order index.
            return (-ts, api_order.get(pid, 0))

        playlists.sort(key=sort_key)

        from ui.utils import AsyncPicture

        for p in playlists:
            pid = p.get("playlistId")
            title = p.get("title", "Untitled")
            if not pid:
                continue

            row = Gtk.ListBoxRow()
            row._pid = pid
            row._title_lower = title.lower()

            # No inner margins on the row hbox — the listbox's own CSS rule
            # (`.add-to-playlist-list > row`) provides the only padding.
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

            cover = AsyncPicture(
                crop_to_square=True, target_size=36, player=self.player
            )
            # "rounded" alone is a circle (50% radius); we want a gentle
            # 4px album-art rounding via our own class.
            cover.add_css_class("add-to-playlist-cover")
            cover.set_overflow(Gtk.Overflow.HIDDEN)
            thumb_url = None
            thumbs = p.get("thumbnails") or []
            if thumbs:
                thumb_url = thumbs[-1].get("url")
            if not thumb_url:
                thumb_url = p.get("thumb")
            if thumb_url:
                cover.load_url(thumb_url)
            hbox.append(cover)

            label = Gtk.Label(label=title)
            label.set_halign(Gtk.Align.START)
            label.set_hexpand(True)
            label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            label.set_xalign(0.0)
            hbox.append(label)

            row.set_child(hbox)
            self.listbox.append(row)
            self._rows.append(row)

    def _on_search_changed(self, entry):
        self._filter_text = entry.get_text().strip().lower()
        self.listbox.invalidate_filter()

    def _row_filter(self, row):
        if not self._filter_text:
            return True
        return self._filter_text in row._title_lower

    def _on_row_activated(self, listbox, row):
        pid = getattr(row, "_pid", None)
        if pid and self.on_select:
            self.on_select(pid)
        self.popdown()
