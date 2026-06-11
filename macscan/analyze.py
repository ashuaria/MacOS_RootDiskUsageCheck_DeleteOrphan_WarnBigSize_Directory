"""Classifier: turn a scanned (path, size) into a Finding with owner, category,
and recommended action.

Pure functions — no I/O. The caller passes in the registry and the scanned
item; we return a Finding. This keeps the rules easy to test.
"""

import os
import time
from collections import namedtuple

from macscan import paths

# Actions (collapsed to 5; REVIEW carries a reason).
ACTION_KEEP_ACTIVE = "KEEP_ACTIVE"
ACTION_REVIEW = "REVIEW"
ACTION_CLEAR_CACHE = "CLEAR_CACHE"
ACTION_REVIEW_TRASH = "REVIEW_TRASH"
ACTION_UNINSTALL_APP = "UNINSTALL_APP"

Finding = namedtuple(
    "Finding",
    ["path", "size_kb", "category", "owner", "action", "reason", "is_orphan",
     "depth_from_root"],
)

# Short, plain-language description of each category. Surfaced in the
# CLI table, HTML report, and TUI as a "WHAT" column so the user can read
# what each line is without having to dig into the file system.
CATEGORY_WHAT = {
    "ios_backup":            "iOS device backup",
    "xcode_derived":         "Xcode build cache",
    "xcode_simulator":       "iOS simulator device",
    "cloud_mirror":          "iCloud mirror (Desktop & Documents)",
    "app_support":           "App support files",
    "app_support_orphan":    "App support files (app uninstalled)",
    "app_support_unknown":   "App support files (app not in registry)",
    "cache":                 "App cache (rebuilt on launch)",
    "cache_orphan":          "App cache (app uninstalled)",
    "container":             "Sandbox container",
    "container_orphan":      "Sandbox container (app uninstalled)",
    "group_container":       "Group container",
    "group_container_orphan":"Group container (app uninstalled)",
    "trash":                 "Item in ~/.Trash",
    "old_download":          "Old download (>90 days)",
    "download":              "Recent download",
    "dev_artifact":          "Dev/cache artifact (re-installable)",
    "app_bundle":            "Inside an app bundle",
    "user_data":             "Your data (Documents/Movies/etc.)",
    "browser_profile":       "Browser profile cache",
    "unknown":               "Unknown — no heuristic matched",
}


def category_what(category):
    return CATEGORY_WHAT.get(category, category)

# Thresholds.
STALENESS_DAYS = 180
OLD_DOWNLOAD_DAYS = 90
SECONDS_PER_DAY = 86400


def _stale(path, days=STALENESS_DAYS):
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    return (time.time() - mtime) > days * SECONDS_PER_DAY


def _mtime_days(path):
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    return int((time.time() - mtime) / SECONDS_PER_DAY)


def _is_path_under(path, ancestor):
    return paths.is_under(path, ancestor)


def _immediate_child(under, full_path):
    """Return the segment of `full_path` directly under `under`, or None."""
    try:
        rel = os.path.relpath(full_path, under)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    first = rel.split(os.sep, 1)[0]
    return first or None


def _strip_team_id(s):
    """Group container IDs are often <TEAMID>.<bundle-id> or <bundle-id>.group.

    Return candidates to try as bundle IDs.
    """
    candidates = [s]
    if "." in s:
        # Try the last 3+ segments as the bundle id (handles TEAMID.bundle.id)
        parts = s.split(".")
        for i in range(1, len(parts)):
            candidate = ".".join(parts[i:])
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        # Try stripping a known group suffix
        if s.endswith(".group"):
            candidates.append(s[: -len(".group")])
    return candidates


def _resolve_owner_by_name(name, registry):
    app = registry.by_name(name)
    if app:
        return app
    # Fuzzy: registered app whose name (CFBundleName or .app file name)
    # contains `name` (or vice versa). Handles e.g. cache dir "Google"
    # matching registered app with CFBundleName "Chrome" when the .app
    # file is "Google Chrome.app".
    name_l = name.lower()
    for candidate in registry.all_apps():
        for variant in (candidate.name, _app_file_stem(candidate.path)):
            v = variant.lower()
            if v and (name_l in v or v in name_l):
                return candidate
    return None


def _app_file_stem(app_path):
    """Return the .app bundle's display name (file name without '.app')."""
    base = os.path.basename(app_path)
    if base.endswith(".app"):
        return base[: -len(".app")]
    return base


def _resolve_cache_owner(subdir, registry):
    """Resolve the app that owns a Library/Caches/<subdir> entry.

    Caches are typically named either after the app display name ("Google")
    or after the bundle ID with a possible suffix ("com.microsoft.VSCode.ShipIt").
    Try name first, then bundle-id-style with common suffixes stripped.
    """
    app = _resolve_owner_by_name(subdir, registry)
    if app:
        return app
    # If it looks like a bundle id, try direct lookup + suffix stripping.
    if "." in subdir:
        for suffix in _CACHE_BUNDLE_ID_SUFFIXES:
            if subdir.endswith(suffix):
                bare = subdir[: -len(suffix)]
                app = _resolve_owner_by_bundle_id(bare, registry)
                if app:
                    return app
        app = _resolve_owner_by_bundle_id(subdir, registry)
        if app:
            return app
    return None


# Common suffixes appended to a bundle id when the OS or an app creates a
# separate cache/ container directory.
_CACHE_BUNDLE_ID_SUFFIXES = (".ShipIt", ".Updater", ".Update", ".Helper")


def _resolve_owner_by_bundle_id(bundle_id, registry):
    if not bundle_id:
        return None
    app = registry.by_bundle_id(bundle_id)
    if app:
        return app
    # Try a few normalizations: strip ".local" or numeric suffix.
    for variant in (bundle_id.replace(".local", ""), bundle_id.split("@")[0]):
        if variant and variant != bundle_id:
            app = registry.by_bundle_id(variant)
            if app:
                return app
    return None


def classify(scan_item, registry, threshold_kb=1024):
    """Classify a single scanned item.

    `scan_item` is a ScanItem namedtuple from scan.scan().
    `registry` is a Registry from apps.build().
    `threshold_kb` filters out trivial items (function default 1MB; the
    CLI default is 10MB).

    Returns a Finding, or None if the item is below the threshold.
    """
    if scan_item.size_kb < threshold_kb:
        return None

    path = scan_item.path
    dfr = getattr(scan_item, "depth_from_root", 0)
    finding = _apply_rules(path, scan_item.size_kb, dfr, registry)
    if finding is None:
        # Fallback: unknown, no owner.
        finding = Finding(
            path=path,
            size_kb=scan_item.size_kb,
            category="unknown",
            owner="",
            action=ACTION_KEEP_ACTIVE,
            reason="no heuristic matched; inspect manually",
            is_orphan=False,
            depth_from_root=dfr,
        )
    return finding


def _apply_rules(path, size_kb, depth_from_root, registry):
    # 1. iOS device backups
    if _is_path_under(path, os.path.join(paths.LIBRARY_APP_SUPPORT, "MobileSync", "Backup")):
        return Finding(
            path=path, size_kb=size_kb, category="ios_backup", owner="MobileSync",
            action=ACTION_REVIEW,
            reason="iOS device backup; usually safe to delete old devices' backups",
            is_orphan=False,
            depth_from_root=depth_from_root,
        )

    # 2. Xcode DerivedData / Simulators
    if _is_path_under(path, os.path.join(paths.LIBRARY_DEVELOPER, "Xcode", "DerivedData")):
        return Finding(
            path=path, size_kb=size_kb, category="xcode_derived", owner="Xcode",
            action=ACTION_REVIEW,
            reason="Xcode DerivedData; safe to delete (regenerated on next build)",
            is_orphan=False,
            depth_from_root=depth_from_root,
        )
    if _is_path_under(path, os.path.join(paths.LIBRARY_DEVELOPER, "CoreSimulator", "Devices")):
        return Finding(
            path=path, size_kb=size_kb, category="xcode_simulator", owner="Xcode",
            action=ACTION_REVIEW,
            reason="iOS Simulator runtime; safe to delete unused devices",
            is_orphan=False,
            depth_from_root=depth_from_root,
        )

    # 3. iCloud mirror of Desktop/Documents
    if _is_path_under(path, paths.LIBRARY_MOBILE_DOCS):
        return Finding(
            path=path, size_kb=size_kb, category="cloud_mirror", owner="iCloud",
            action=ACTION_KEEP_ACTIVE,
            reason="iCloud Desktop & Documents mirror; not local-only space",
            is_orphan=False,
            depth_from_root=depth_from_root,
        )

    # 4. ~/Library/Application Support/<X>/
    if _is_path_under(path, paths.LIBRARY_APP_SUPPORT):
        x = _immediate_child(paths.LIBRARY_APP_SUPPORT, path)
        if x:
            owner_app = _resolve_owner_by_name(x, registry)
            if owner_app:
                return Finding(
                    path=path, size_kb=size_kb, category="app_support",
                    owner=owner_app.name, action=ACTION_KEEP_ACTIVE,
                    reason="support files for installed app",
                    is_orphan=False,
            depth_from_root=depth_from_root,
                )
            if registry.is_installed_signal(x):
                return Finding(
                    path=path, size_kb=size_kb, category="app_support",
                    owner=x, action=ACTION_KEEP_ACTIVE,
                    reason="support files for installed app (Saved State signal)",
                    is_orphan=False,
            depth_from_root=depth_from_root,
                )
            days = _mtime_days(path)
            if days is not None and days > STALENESS_DAYS:
                return Finding(
                    path=path, size_kb=size_kb, category="app_support_orphan",
                    owner=x, action=ACTION_UNINSTALL_APP,
                    reason=f"app '{x}' not installed; untouched for {days} days",
                    is_orphan=True,
            depth_from_root=depth_from_root,
                )
            return Finding(
                path=path, size_kb=size_kb, category="app_support_unknown",
                owner=x, action=ACTION_REVIEW,
                reason=f"app '{x}' not in installed registry; review before removing",
                is_orphan=True,
            depth_from_root=depth_from_root,
            )

    # 5. ~/Library/Caches/<X>/
    if _is_path_under(path, paths.LIBRARY_CACHES):
        x = _immediate_child(paths.LIBRARY_CACHES, path)
        if x:
            owner_app = _resolve_cache_owner(x, registry)
            if owner_app:
                return Finding(
                    path=path, size_kb=size_kb, category="cache", owner=owner_app.name,
                    action=ACTION_CLEAR_CACHE,
                    reason="cache for installed app; safe to clear (rebuilds on launch)",
                    is_orphan=False,
            depth_from_root=depth_from_root,
                )
            if registry.is_installed_signal(x):
                return Finding(
                    path=path, size_kb=size_kb, category="cache", owner=x,
                    action=ACTION_CLEAR_CACHE,
                    reason="cache for installed app (Saved State signal); safe to clear",
                    is_orphan=False,
            depth_from_root=depth_from_root,
                )
            return Finding(
                path=path, size_kb=size_kb, category="cache_orphan", owner=x,
                action=ACTION_UNINSTALL_APP,
                reason=f"cache for uninstalled app '{x}'; safe to remove",
                is_orphan=True,
            depth_from_root=depth_from_root,
            )

    # 6. ~/Library/Containers/<bundle-id>/
    if _is_path_under(path, paths.LIBRARY_CONTAINERS):
        x = _immediate_child(paths.LIBRARY_CONTAINERS, path)
        if x:
            owner_app = _resolve_owner_by_bundle_id(x, registry)
            if owner_app:
                return Finding(
                    path=path, size_kb=size_kb, category="container",
                    owner=owner_app.name, action=ACTION_KEEP_ACTIVE,
                    reason="sandbox container for installed app",
                    is_orphan=False,
            depth_from_root=depth_from_root,
                )
            return Finding(
                path=path, size_kb=size_kb, category="container_orphan",
                owner=x, action=ACTION_UNINSTALL_APP,
                reason=f"sandboxed data for uninstalled app (bundle id '{x}')",
                is_orphan=True,
            depth_from_root=depth_from_root,
            )

    # 7. ~/Library/Group Containers/<group-id>/
    if _is_path_under(path, paths.LIBRARY_GROUP_CONTAINERS):
        x = _immediate_child(paths.LIBRARY_GROUP_CONTAINERS, path)
        if x:
            for candidate in _strip_team_id(x):
                owner_app = _resolve_owner_by_bundle_id(candidate, registry)
                if owner_app:
                    return Finding(
                        path=path, size_kb=size_kb, category="group_container",
                        owner=owner_app.name, action=ACTION_KEEP_ACTIVE,
                        reason="group container for installed app",
                        is_orphan=False,
            depth_from_root=depth_from_root,
                    )
            return Finding(
                path=path, size_kb=size_kb, category="group_container_orphan",
                owner=x, action=ACTION_UNINSTALL_APP,
                reason=f"group container for uninstalled app ('{x}')",
                is_orphan=True,
            depth_from_root=depth_from_root,
            )

    # 8. ~/.Trash/<X>
    if _is_path_under(path, paths.TRASH):
        return Finding(
            path=path, size_kb=size_kb, category="trash", owner="",
            action=ACTION_REVIEW_TRASH,
            reason="in Trash; can empty ~/.Trash when ready",
            is_orphan=False,
            depth_from_root=depth_from_root,
        )

    # 9. ~/Downloads/<X> with mtime > 90 days
    if _is_path_under(path, paths.DOWNLOADS):
        days = _mtime_days(path)
        if days is not None and days > OLD_DOWNLOAD_DAYS:
            return Finding(
                path=path, size_kb=size_kb, category="old_download", owner="",
                action=ACTION_REVIEW,
                reason=f"in Downloads for {days} days; review and delete if no longer needed",
                is_orphan=False,
            depth_from_root=depth_from_root,
            )
        return Finding(
            path=path, size_kb=size_kb, category="download", owner="",
            action=ACTION_KEEP_ACTIVE,
            reason="recently downloaded; leave it",
            is_orphan=False,
            depth_from_root=depth_from_root,
        )

    # 10. Dev artifacts (node_modules, .cache, etc.)
    basename = os.path.basename(path)
    if basename in paths.DEV_ARTIFACT_NAMES:
        return Finding(
            path=path, size_kb=size_kb, category="dev_artifact", owner="",
            action=ACTION_REVIEW,
            reason=f"dev artifact '{basename}'; re-installable via package manager",
            is_orphan=False,
            depth_from_root=depth_from_root,
        )
    # A child of a known dev cache root (e.g. ~/.cache/uv, ~/.cache/pip,
    # ~/.cargo/registry). Walk up the path; if any component is a known
    # dev cache dir, flag.
    parts = path.split(os.sep)
    for part in parts[:-1]:
        if part in paths.DEV_ARTIFACT_NAMES:
            return Finding(
                path=path, size_kb=size_kb, category="dev_artifact", owner="",
                action=ACTION_REVIEW,
                reason=f"inside dev cache '{part}'; re-installable via package manager",
                is_orphan=False,
            depth_from_root=depth_from_root,
            )

    # 11. Inside an .app bundle (e.g., Contents/Resources or Contents/Frameworks)
    if ".app/" in path:
        bundle = None
        for seg in path.split(os.sep):
            if seg.endswith(".app"):
                bundle = seg[: -len(".app")]
                break
        if bundle:
            return Finding(
                path=path, size_kb=size_kb, category="app_bundle", owner=bundle,
                action=ACTION_KEEP_ACTIVE,
                reason="inside an installed app bundle",
                is_orphan=False,
            depth_from_root=depth_from_root,
            )

    # 12. User data dirs (Documents, Movies, Pictures, Desktop)
    for d, label in (
        (paths.DOCUMENTS, "Documents"),
        (paths.MOVIES, "Movies"),
        (paths.PICTURES, "Pictures"),
        (paths.DESKTOP, "Desktop"),
    ):
        if _is_path_under(path, d):
            return Finding(
                path=path, size_kb=size_kb, category="user_data", owner=label,
                action=ACTION_REVIEW,
                reason="your data; consider archiving to an external drive",
                is_orphan=False,
            depth_from_root=depth_from_root,
            )

    # 13. Browser profile caches (Chromium family) — clear-with-care
    if _is_path_under(path, paths.LIBRARY_APP_SUPPORT):
        # Already handled above if it matched Application Support. Otherwise
        # this branch catches deeper paths like Application Support/Google/...
        for vendor in paths.BROWSER_VENDORS:
            if f"/{vendor}/" in path or path.endswith(f"/{vendor}"):
                return Finding(
                    path=path, size_kb=size_kb, category="browser_profile",
                    owner=vendor, action=ACTION_REVIEW,
                    reason="browser profile cache; clearing may sign you out",
                    is_orphan=False,
            depth_from_root=depth_from_root,
                )

    return None


def humanize_size_kb(kb):
    if kb < 1024:
        return f"{kb}K"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f}M"
    gb = mb / 1024
    return f"{gb:.2f}G"
