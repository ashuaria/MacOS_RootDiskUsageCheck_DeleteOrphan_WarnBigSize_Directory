"""Unit tests for the classifier. Uses a stub registry (no real I/O)."""

import os
import unittest

from macscan import analyze, apps, paths, scan


def _set_test_home():
    paths.set_home("/Users/u")


def _reset_test_home():
    paths.set_home(os.path.expanduser("~"))


class StubRegistry:
    """Minimal stand-in for apps.Registry with just the methods the analyzer uses."""

    def __init__(self, by_name=None, by_bundle_id=None, signals=None):
        self._by_name = {n: apps.AppInfo(n, b, "/Applications/X.app", False)
                         for n, b in (by_name or {}).items()}
        self._by_bundle_id = {b: apps.AppInfo(n, b, "/Applications/X.app", False)
                              for n, b in (by_name or {}).items()}
        if by_bundle_id:
            for bid, name in by_bundle_id.items():
                self._by_bundle_id[bid] = apps.AppInfo(name, bid, "/Applications/X.app", False)
        self._signals = set(signals or [])

    def by_name(self, name):
        return self._by_name.get(name)

    def by_bundle_id(self, bid):
        return self._by_bundle_id.get(bid)

    def is_installed_signal(self, bid):
        return bid in self._signals

    def all_apps(self):
        return list(self._by_name.values())


def _item(path, kb):
    return scan.ScanItem(path=path, size_kb=kb, depth=2, inaccessible=False)


class TestClassify(unittest.TestCase):
    def setUp(self):
        _set_test_home()

    def tearDown(self):
        _reset_test_home()

    def test_ios_backup(self):
        f = analyze.classify(
            _item("/Users/u/Library/Application Support/MobileSync/Backup/abc", 5_000_000),
            StubRegistry(),
        )
        self.assertEqual(f.category, "ios_backup")
        self.assertEqual(f.action, analyze.ACTION_REVIEW)

    def test_xcode_derived(self):
        f = analyze.classify(
            _item("/Users/u/Library/Developer/Xcode/DerivedData/SomeApp", 2_000_000),
            StubRegistry(),
        )
        self.assertEqual(f.category, "xcode_derived")

    def test_app_support_installed(self):
        f = analyze.classify(
            _item("/Users/u/Library/Application Support/Slack", 500_000),
            StubRegistry(by_name={"Slack": "com.tinyspeck.chatlyio"}),
        )
        self.assertEqual(f.category, "app_support")
        self.assertEqual(f.owner, "Slack")
        self.assertEqual(f.action, analyze.ACTION_KEEP_ACTIVE)
        self.assertFalse(f.is_orphan)

    def test_app_support_orphan(self):
        f = analyze.classify(
            _item("/Users/u/Library/Application Support/FakeApp1234", 500_000),
            StubRegistry(),
        )
        # Not in registry, no installed signal. With a fresh mtime the analyzer
        # will mark it as app_support_unknown + REVIEW; with a stale mtime it
        # would be UNINSTALL_APP. Either way it is an orphan.
        self.assertTrue(f.is_orphan)
        self.assertIn(f.action, (analyze.ACTION_REVIEW, analyze.ACTION_UNINSTALL_APP))

    def test_cache_installed(self):
        f = analyze.classify(
            _item("/Users/u/Library/Caches/Slack", 200_000),
            StubRegistry(by_name={"Slack": "com.tinyspeck.chatlyio"}),
        )
        self.assertEqual(f.action, analyze.ACTION_CLEAR_CACHE)

    def test_cache_orphan(self):
        f = analyze.classify(
            _item("/Users/u/Library/Caches/OldApp", 200_000),
            StubRegistry(),
        )
        self.assertEqual(f.action, analyze.ACTION_UNINSTALL_APP)
        self.assertTrue(f.is_orphan)

    def test_container_installed(self):
        f = analyze.classify(
            _item("/Users/u/Library/Containers/com.example.App", 100_000),
            StubRegistry(by_bundle_id={"com.example.App": "Example"}),
        )
        self.assertEqual(f.action, analyze.ACTION_KEEP_ACTIVE)

    def test_container_orphan(self):
        f = analyze.classify(
            _item("/Users/u/Library/Containers/com.gone.app", 100_000),
            StubRegistry(),
        )
        self.assertEqual(f.action, analyze.ACTION_UNINSTALL_APP)
        self.assertTrue(f.is_orphan)

    def test_group_container_with_team_id(self):
        f = analyze.classify(
            _item("/Users/u/Library/Group Containers/ABCDE.com.example.App", 100_000),
            StubRegistry(by_bundle_id={"com.example.App": "Example"}),
        )
        self.assertEqual(f.owner, "Example")
        self.assertEqual(f.action, analyze.ACTION_KEEP_ACTIVE)

    def test_trash(self):
        f = analyze.classify(
            _item("/Users/u/.Trash/old-file.dmg", 50_000),
            StubRegistry(),
        )
        self.assertEqual(f.action, analyze.ACTION_REVIEW_TRASH)

    def test_node_modules(self):
        f = analyze.classify(
            _item("/Users/u/Projects/myapp/node_modules", 1_000_000),
            StubRegistry(),
        )
        self.assertEqual(f.category, "dev_artifact")

    def test_below_threshold(self):
        f = analyze.classify(
            _item("/Users/u/Documents/tiny.txt", 10),
            StubRegistry(),
        )
        self.assertIsNone(f)

    def test_humanize(self):
        self.assertEqual(analyze.humanize_size_kb(500), "500K")
        self.assertEqual(analyze.humanize_size_kb(2048), "2.0M")
        self.assertEqual(analyze.humanize_size_kb(5 * 1024 * 1024), "5.00G")

    def test_category_what_covers_every_category_in_findings(self):
        # Every category the analyzer emits should have a short description.
        import re

        # Categories referenced anywhere in analyze.py's _apply_rules:
        known = {
            "ios_backup", "xcode_derived", "xcode_simulator", "cloud_mirror",
            "app_support", "app_support_orphan", "app_support_unknown",
            "cache", "cache_orphan",
            "container", "container_orphan",
            "group_container", "group_container_orphan",
            "trash", "old_download", "download", "dev_artifact",
            "app_bundle", "user_data", "browser_profile", "unknown",
        }
        missing = [c for c in known if not analyze.category_what(c)]
        self.assertEqual(missing, [], f"missing descriptions: {missing}")

    def test_finding_to_dict_includes_what(self):
        from macscan.report import finding_to_dict

        f = analyze.Finding(
            path="/x", size_kb=100, category="app_support_orphan",
            owner="Slack", action=analyze.ACTION_UNINSTALL_APP,
            reason="x", is_orphan=True,
        )
        d = finding_to_dict(f)
        self.assertIn("what", d)
        self.assertEqual(d["what"], "App support files (app uninstalled)")


if __name__ == "__main__":
    unittest.main()
