"""
Windows system tray icon using pystray.
Shows when the app is hidden to background (playing with window closed).
"""

import sys
import threading

if sys.platform != "win32":
    raise ImportError("tray_win is Windows-only")

import pystray
from PIL import Image
from gi.repository import GLib


class TrayIcon:
    def __init__(self, window, player):
        self.window = window
        self.player = player
        self._icon = None
        self._running = False

    def show(self):
        if self._running:
            return

        self._running = True
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def hide(self):
        if self._icon:
            self._icon.stop()
            self._icon = None
        self._running = False

    def _run(self):
        try:
            image = Image.open(self._get_icon_path())
        except Exception:
            # Fallback: create a simple colored circle
            image = Image.new("RGB", (64, 64), color=(100, 100, 200))

        menu = pystray.Menu(
            pystray.MenuItem("Show VenTapes", self._on_show, default=True),
            pystray.MenuItem("Play/Pause", self._on_play_pause),
            pystray.MenuItem("Next", self._on_next),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

        self._icon = pystray.Icon("ventapes", image, "VenTapes", menu)
        self._icon.run()

    def _get_icon_path(self):
        import os
        # Look for icon relative to the app
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidates = [
            os.path.join(base, "windows", "ventapes.ico"),
            os.path.join(base, "assets", "ventapes.ico"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        raise FileNotFoundError("No icon found")

    def _on_show(self, icon, item):
        GLib.idle_add(self._do_show)

    def _do_show(self):
        self.window.set_visible(True)
        self.window.present()
        return False

    def _on_play_pause(self, icon, item):
        GLib.idle_add(self._do_play_pause)

    def _do_play_pause(self):
        state = self.player.get_state_string()
        if state == "playing":
            self.player.pause()
        else:
            self.player.play()
        return False

    def _on_next(self, icon, item):
        GLib.idle_add(self.player.next)

    def _on_quit(self, icon, item):
        self.hide()
        GLib.idle_add(self._do_quit)

    def _do_quit(self):
        self.player.stop()
        from gi.repository import Gtk
        app = Gtk.Application.get_default()
        if app:
            app.quit()
        return False
