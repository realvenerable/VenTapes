import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ui.preferences import get_bool, get_float, get_int, read_prefs, update_prefs


class PreferencesStoreTests(unittest.TestCase):
    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "prefs.json")
            self.assertEqual(read_prefs(path, {"fallback": True}), {"fallback": True})

    def test_updates_merge_without_losing_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "prefs.json")
            update_prefs(path, {"color_scheme": "dark", "unknown": [1, 2]})
            update_prefs(path, {"accent_source": "custom"})

            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["color_scheme"], "dark")
            self.assertEqual(raw["unknown"], [1, 2])
            self.assertEqual(raw["accent_source"], "custom")

    def test_concurrent_updates_merge_distinct_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "prefs.json")
            update_prefs(path, {"base": 1})
            barrier = threading.Barrier(3)

            def write(key):
                barrier.wait()
                update_prefs(path, {key: key})

            threads = [
                threading.Thread(target=write, args=("left",)),
                threading.Thread(target=write, args=("right",)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            result = read_prefs(path)
            self.assertEqual(result["base"], 1)
            self.assertEqual(result["left"], "left")
            self.assertEqual(result["right"], "right")

    def test_boolean_strings_are_explicit(self):
        self.assertTrue(get_bool({"value": "true"}, "value"))
        self.assertFalse(get_bool({"value": "false"}, "value", True))
        self.assertTrue(get_bool({}, "value", True))
        self.assertFalse(get_bool({"value": []}, "value", False))
        self.assertTrue(get_bool({"value": []}, "value", True))
        self.assertFalse(get_bool({"value": 0}, "value", True))
        self.assertTrue(get_bool({"value": 1}, "value"))

    def test_numeric_preferences_are_clamped_and_safe(self):
        self.assertEqual(get_float({"value": "bad"}, "value", 1.5), 1.5)
        self.assertEqual(get_float({"value": 9}, "value", 1.0, 0.0, 2.0), 2.0)
        self.assertEqual(get_int({"value": None}, "value", 8, 1, 100), 8)

    def test_numeric_overflow_falls_back_instead_of_raising(self):
        huge = 10 ** 1000
        self.assertFalse(get_bool({"value": huge}, "value", False))
        self.assertEqual(get_float({"value": huge}, "value", 1.5), 1.5)


if __name__ == "__main__":
    unittest.main()
