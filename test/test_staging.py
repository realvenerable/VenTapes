import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from player.staging import (
    STAGING_DIR_PREFIX,
    acquire_staging_lease,
    acquire_staging_lock,
    directory_size,
    find_completed_audio,
    is_staging_dir,
    limit_bytes_from_mb,
    remove_staging_dir,
    reported_size_exceeds,
    staging_lease_state,
    sweep_staging_dirs,
)


class StagingHelpersTests(unittest.TestCase):
    def test_limit_parser_clamps_and_handles_invalid_values(self):
        self.assertEqual(limit_bytes_from_mb("2"), 2 * 1024 * 1024)
        self.assertEqual(limit_bytes_from_mb("99999"), 2048 * 1024 * 1024)
        self.assertEqual(limit_bytes_from_mb("not-a-number", 8), 8 * 1024 * 1024)
        self.assertEqual(limit_bytes_from_mb(float("nan"), 8), 8 * 1024 * 1024)
        self.assertEqual(limit_bytes_from_mb(float("inf"), 8), 8 * 1024 * 1024)

    def test_sweep_only_removes_owned_directories(self):
        with tempfile.TemporaryDirectory() as root:
            owned = os.path.join(root, STAGING_DIR_PREFIX + "owned")
            unrelated = os.path.join(root, "other")
            os.makedirs(owned)
            os.makedirs(unrelated)
            with open(os.path.join(owned, "audio.part"), "wb") as handle:
                handle.write(b"1234")
            with open(os.path.join(unrelated, "keep"), "wb") as handle:
                handle.write(b"keep")

            self.assertEqual(sweep_staging_dirs(root, min_age_seconds=0), 1)
            self.assertFalse(os.path.exists(owned))
            self.assertTrue(os.path.exists(unrelated))
            self.assertEqual(directory_size(unrelated), 4)

    def test_sweep_keeps_active_directory(self):
        with tempfile.TemporaryDirectory() as root:
            active = os.path.join(root, STAGING_DIR_PREFIX + "active")
            os.makedirs(active)
            self.assertEqual(
                sweep_staging_dirs(root, keep=(active,), min_age_seconds=0),
                0,
            )
            self.assertTrue(is_staging_dir(active, (root,)))

    def test_recent_directory_is_not_swept_by_default(self):
        with tempfile.TemporaryDirectory() as root:
            recent = os.path.join(root, STAGING_DIR_PREFIX + "recent")
            os.makedirs(recent)
            self.assertEqual(sweep_staging_dirs(root), 0)
            self.assertTrue(os.path.exists(recent))

    def test_activity_inside_old_directory_prevents_legacy_sweep(self):
        with tempfile.TemporaryDirectory() as root:
            active = os.path.join(root, STAGING_DIR_PREFIX + "active-file")
            os.makedirs(active)
            partial = os.path.join(active, "audio.m4a.part")
            with open(partial, "wb") as handle:
                handle.write(b"still writing")
            old = 0.0
            os.utime(active, (old, old))
            self.assertEqual(sweep_staging_dirs(root), 0)
            self.assertTrue(os.path.exists(active))

    def test_reported_download_size_is_checked(self):
        self.assertTrue(reported_size_exceeds({"downloaded_bytes": 11}, 10))
        self.assertTrue(reported_size_exceeds({"total_bytes_estimate": 11}, 10))
        self.assertFalse(reported_size_exceeds({"downloaded_bytes": 10}, 10))
        self.assertFalse(reported_size_exceeds({"downloaded_bytes": "bad"}, 10))

    def test_partial_audio_is_not_returned(self):
        with tempfile.TemporaryDirectory() as root:
            partial = os.path.join(root, "audio.m4a.part")
            with open(partial, "wb") as handle:
                handle.write(b"partial")
            self.assertIsNone(find_completed_audio(root))
            fragment = os.path.join(root, "audio.m4a.part-Frag0")
            with open(fragment, "wb") as handle:
                handle.write(b"fragment")
            self.assertIsNone(find_completed_audio(root))
            final = os.path.join(root, "audio.m4a")
            with open(final, "wb") as handle:
                handle.write(b"complete")
            self.assertEqual(find_completed_audio(root), final)

    def test_ambiguous_audio_files_are_rejected_unless_named(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("audio.m4a", "audio.webm"):
                with open(os.path.join(root, name), "wb") as handle:
                    handle.write(name.encode())
            self.assertIsNone(find_completed_audio(root))
            self.assertEqual(
                find_completed_audio(root, expected_name="audio.webm"),
                os.path.join(root, "audio.webm"),
            )

    def test_live_lease_blocks_sweep_and_release_allows_reclaim(self):
        with tempfile.TemporaryDirectory() as root:
            owned = os.path.join(root, STAGING_DIR_PREFIX + "leased")
            os.makedirs(owned)
            with open(os.path.join(owned, "audio.m4a"), "wb") as handle:
                handle.write(b"audio")
            lease = acquire_staging_lease(owned, blocking=False)
            self.assertIsNotNone(lease)
            self.assertEqual(staging_lease_state(owned), "held")
            self.assertEqual(
                sweep_staging_dirs(root, min_age_seconds=0),
                0,
            )
            lease.close()
            self.assertEqual(staging_lease_state(owned), "released")
            self.assertEqual(
                sweep_staging_dirs(root, min_age_seconds=0),
                1,
            )

    def test_budget_lock_serializes_reservation_observation(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "budget.lock")
            first = acquire_staging_lock(lock_path, blocking=False)
            self.assertIsNotNone(first)
            self.assertIsNone(acquire_staging_lock(lock_path, blocking=False))
            first.close()
            second = acquire_staging_lock(lock_path, blocking=False)
            self.assertIsNotNone(second)
            second.close()

    def test_remove_requires_a_direct_owned_root(self):
        with tempfile.TemporaryDirectory() as root:
            outside = os.path.join(root, "..", "outside-" + STAGING_DIR_PREFIX)
            outside = os.path.abspath(outside)
            os.makedirs(outside)
            self.assertFalse(remove_staging_dir(outside, (root,)))
            self.assertTrue(os.path.isdir(outside))
            os.rmdir(outside)

    def test_nested_and_symlinked_paths_are_not_owned(self):
        with tempfile.TemporaryDirectory() as root:
            nested = os.path.join(root, "nested", STAGING_DIR_PREFIX + "nested")
            os.makedirs(nested)
            self.assertFalse(is_staging_dir(nested, (root,)))
            link = os.path.join(root, STAGING_DIR_PREFIX + "link")
            try:
                os.symlink(nested, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available on this platform")
            self.assertFalse(is_staging_dir(link, (root,)))


if __name__ == "__main__":
    unittest.main()
