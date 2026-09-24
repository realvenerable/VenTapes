import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from player import lyrics_prefs
from ui.preferences import update_prefs


class LyricsPreferenceSchemaTests(unittest.TestCase):
    def test_malformed_provider_list_is_ignored_safely(self):
        old_path = lyrics_prefs._path
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "prefs.json")
                update_prefs(path, {"lyrics_provider_order": [[], None, 3]})
                lyrics_prefs._path = lambda: path
                lyrics_prefs.invalidate()
                self.assertEqual(
                    lyrics_prefs.full_provider_order(),
                    lyrics_prefs.DEFAULT_PROVIDER_ORDER,
                )
        finally:
            lyrics_prefs._path = old_path
            lyrics_prefs.invalidate()


if __name__ == "__main__":
    unittest.main()
