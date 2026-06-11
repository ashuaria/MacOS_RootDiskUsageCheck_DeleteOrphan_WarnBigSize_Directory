"""Registry of installed macOS apps.

Scans well-known app directories, extracts bundle ID and bundle name from each
.app's Info.plist, and exposes lookup maps for the analyzer. Caches the result
to ~/.cache/macscan/registry.json, invalidating when the apps dirs change.

Also collects "definitely installed" signals from:
  - ~/Library/Saved Application State/<bundle-id>.savedState
  - the quarantine xattr on .app bundles (soft signal)
"""

import json
import os
import plistlib
import subprocess
import sys
from collections import namedtuple

from macscan import paths

AppInfo = namedtuple("AppInfo", ["name", "bundle_id", "path", "quarantined"])

CACHE_FILENAME = "registry.json"
SIGNATURE_FILENAME = "registry.sig"

STALENESS_DAYS = 180  # used downstream by analyzer; exposed for documentation


class Registry:
    """Maps for app lookups. Construct via build()."""

    def __init__(self, by_name, by_bundle_id, installed_signals, sources, cache_path):
        self._by_name = by_name  # name -> AppInfo
        self._by_bundle_id = by_bundle_id  # bundle_id -> AppInfo
        self._installed_signals = installed_signals  # set of bundle_ids
        self.sources = sources  # list of dirs that contributed
        self.cache_path = cache_path

    def by_name(self, name):
        return self._by_name.get(name)

    def by_bundle_id(self, bundle_id):
        return self._by_bundle_id.get(bundle_id)

    def is_installed_signal(self, bundle_id):
        """True if any 'definitely installed' signal exists for this bundle id."""
        return bundle_id in self._installed_signals

    def __len__(self):
        return len(self._by_name)

    def all_apps(self):
        return sorted(self._by_name.values(), key=lambda a: a.name.lower())

    def to_dict(self):
        return {
            "by_name": {n: _app_to_dict(a) for n, a in self._by_name.items()},
            "by_bundle_id": {b: _app_to_dict(a) for b, a in self._by_bundle_id.items()},
            "installed_signals": sorted(self._installed_signals),
            "sources": self.sources,
        }

    @classmethod
    def from_dict(cls, d, cache_path):
        by_name = {n: AppInfo(**a) for n, a in d.get("by_name", {}).items()}
        by_bundle_id = {b: AppInfo(**a) for b, a in d.get("by_bundle_id", {}).items()}
        return cls(
            by_name=by_name,
            by_bundle_id=by_bundle_id,
            installed_signals=set(d.get("installed_signals", [])),
            sources=d.get("sources", []),
            cache_path=cache_path,
        )


def _app_to_dict(a):
    return {
        "name": a.name,
        "bundle_id": a.bundle_id,
        "path": a.path,
        "quarantined": a.quarantined,
    }


def _has_quarantine_xattr(app_path):
    """Check if a .app bundle has the com.apple.quarantine xattr set."""
    try:
        proc = subprocess.run(
            ["/usr/bin/xattr", "-p", "com.apple.quarantine", app_path],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _read_info_plist(app_path):
    plist_path = os.path.join(app_path, "Contents", "Info.plist")
    try:
        with open(plist_path, "rb") as f:
            return plistlib.load(f)
    except (FileNotFoundError, PermissionError, plistlib.InvalidFileException, ValueError):
        return None
    except Exception:
        # Some third-party apps have non-standard Info.plist (binary plists
        # with custom types, or XML with non-UTF-8 bytes). Don't crash the
        # whole scan over one bad app.
        return None


def _dirs_signature(app_dirs):
    """Stable signature: which app dirs exist and what *.app names are inside."""
    parts = []
    for d in app_dirs:
        if not os.path.isdir(d):
            parts.append(f"{d}:missing")
            continue
        try:
            entries = sorted(os.listdir(d))
        except PermissionError:
            parts.append(f"{d}:denied")
            continue
        apps = [e for e in entries if e.endswith(".app")]
        parts.append(f"{d}:{len(apps)}:" + ",".join(apps))
    return "|".join(parts)


def _scan_apps(app_dirs):
    """Walk each dir for *.app bundles, read Info.plist, return list of AppInfo."""
    found = []
    for d in app_dirs:
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except PermissionError:
            continue
        for entry in entries:
            if not entry.endswith(".app"):
                continue
            app_path = os.path.join(d, entry)
            info = _read_info_plist(app_path)
            if info is None:
                continue
            bundle_id = info.get("CFBundleIdentifier", "")
            name = info.get("CFBundleName") or entry[:-4]
            if not bundle_id:
                continue
            quarantined = _has_quarantine_xattr(app_path)
            found.append(
                AppInfo(
                    name=name,
                    bundle_id=bundle_id,
                    path=app_path,
                    quarantined=quarantined,
                )
            )
    return found


def _scan_saved_state_signals(saved_state_dir):
    """Return a set of bundle_ids with a Saved Application State entry."""
    if not os.path.isdir(saved_state_dir):
        return set()
    out = set()
    try:
        for entry in os.listdir(saved_state_dir):
            if entry.endswith(".savedState"):
                bid = entry[: -len(".savedState")]
                if bid:
                    out.add(bid)
    except PermissionError:
        pass
    return out


def _signature_matches_cache(app_dirs, cache_dir):
    sig = _dirs_signature(app_dirs)
    sig_file = os.path.join(cache_dir, SIGNATURE_FILENAME)
    try:
        with open(sig_file, "r", encoding="utf-8") as f:
            return f.read().strip() == sig, sig, sig_file
    except FileNotFoundError:
        return False, sig, sig_file


def build(app_dirs=None, cache_dir=None, rebuild=False):
    """Build the registry, using cache if valid. Returns a Registry instance."""
    if app_dirs is None:
        app_dirs = list(paths.DEFAULT_APP_DIRS)
    if cache_dir is None:
        cache_dir = os.path.join(paths.HOME, ".cache", "macscan")
    os.makedirs(cache_dir, exist_ok=True)

    cache_path = os.path.join(cache_dir, CACHE_FILENAME)
    matches, sig, sig_file = _signature_matches_cache(app_dirs, cache_dir)

    if not rebuild and matches and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Registry.from_dict(data, cache_path=cache_path)
        except (json.JSONDecodeError, OSError, TypeError):
            pass  # rebuild on any cache error

    apps = _scan_apps(app_dirs)
    by_name = {}
    by_bundle_id = {}
    for a in apps:
        # If two apps share a name (e.g., same name in /Applications and the
        # external drive), prefer the first we saw; the other is reachable
        # by bundle id.
        if a.name not in by_name:
            by_name[a.name] = a
        if a.bundle_id not in by_bundle_id:
            by_bundle_id[a.bundle_id] = a

    installed_signals = _scan_saved_state_signals(paths.LIBRARY_SAVED_STATE)
    installed_signals.update(by_bundle_id.keys())

    reg = Registry(
        by_name=by_name,
        by_bundle_id=by_bundle_id,
        installed_signals=installed_signals,
        sources=[d for d in app_dirs if os.path.isdir(d)],
        cache_path=cache_path,
    )

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(reg.to_dict(), f, indent=2)
        with open(sig_file, "w", encoding="utf-8") as f:
            f.write(sig)
    except OSError as e:
        print(f"warning: could not write registry cache: {e}", file=sys.stderr)

    return reg
