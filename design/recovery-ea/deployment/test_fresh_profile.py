import json
from pathlib import Path
import tempfile
import unittest
import uuid

from fresh_profile import initialize_fresh_profile


class TestFreshProfile(unittest.TestCase):
    def test_creates_two_independent_profiles_without_account_detection(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = [json.loads(initialize_fresh_profile(Path(d) / n).read_text())
                    for n in ("one", "two")]
        for profile in (a, b):
            self.assertEqual(str(uuid.UUID(profile["install_id"])), profile["install_id"])
            self.assertGreaterEqual(len(profile["panel_key"]), 40)
            self.assertEqual(profile["mt5"], {"login": None, "server": None, "symbol": "XAUUSD.s"})
        self.assertNotEqual(a["panel_key"], b["panel_key"])
        self.assertNotEqual(a["install_id"], b["install_id"])

    def test_cannot_overwrite_existing_profile(self):
        with tempfile.TemporaryDirectory() as d:
            target = initialize_fresh_profile(Path(d))
            initial = target.read_bytes()
            with self.assertRaises(FileExistsError):
                initialize_fresh_profile(Path(d))
            self.assertEqual(target.read_bytes(), initial)

    def test_does_not_import_or_mutate_legacy_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = root / "old"
            old.mkdir()
            sentinel = old / "settings.json"
            sentinel.write_text("dirt")
            with self.assertRaises(FileExistsError):
                initialize_fresh_profile(old)
            profile = initialize_fresh_profile(root / "fresh")
            self.assertTrue(profile.is_file())
            self.assertEqual(sentinel.read_text(), "dirt")

    def test_rejects_link_as_target(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            destination = root / "real"
            destination.mkdir()
            linked = root / "symlink"
            linked.symlink_to(destination, target_is_directory=True)
            with self.assertRaises(ValueError):
                initialize_fresh_profile(linked)
            self.assertFalse(any(destination.iterdir()))
