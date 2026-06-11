"""Well-known macOS paths used by macscan.

Single source of truth for which directories count as "Library subdirs", "Apps
dirs", etc. Keeping this in one place makes it easy to add new heuristics later
and to test path-based classification in isolation.

All Library / data constants are derived from `HOME` and recomputed by
`set_home()` so tests can run against a synthetic home directory.
"""

import os

HOME = os.path.expanduser("~")


def set_home(new_home):
    """Re-derive every Library / data constant from a new home base.

    Primarily for tests, but also useful if the user has moved their home
    directory. Mutates the module's module-level constants.
    """
    global HOME
    global LIBRARY_APP_SUPPORT, LIBRARY_CACHES, LIBRARY_CONTAINERS
    global LIBRARY_GROUP_CONTAINERS, LIBRARY_SAVED_STATE, LIBRARY_DEVELOPER
    global LIBRARY_MOBILE_DOCS
    global TRASH, DOWNLOADS, DOCUMENTS, MOVIES, PICTURES, DESKTOP
    global DEFAULT_APP_DIRS

    HOME = os.path.expanduser(new_home)

    LIBRARY_APP_SUPPORT = os.path.join(HOME, "Library", "Application Support")
    LIBRARY_CACHES = os.path.join(HOME, "Library", "Caches")
    LIBRARY_CONTAINERS = os.path.join(HOME, "Library", "Containers")
    LIBRARY_GROUP_CONTAINERS = os.path.join(HOME, "Library", "Group Containers")
    LIBRARY_SAVED_STATE = os.path.join(HOME, "Library", "Saved Application State")
    LIBRARY_DEVELOPER = os.path.join(HOME, "Library", "Developer")
    LIBRARY_MOBILE_DOCS = os.path.join(HOME, "Library", "Mobile Documents")

    TRASH = os.path.join(HOME, ".Trash")
    DOWNLOADS = os.path.join(HOME, "Downloads")
    DOCUMENTS = os.path.join(HOME, "Documents")
    MOVIES = os.path.join(HOME, "Movies")
    PICTURES = os.path.join(HOME, "Pictures")
    DESKTOP = os.path.join(HOME, "Desktop")

    DEFAULT_APP_DIRS = [
        "/Applications",
        "/System/Applications",
        os.path.join(HOME, "Applications"),
        "/Volumes/NVME_500G/Applications",
    ]


# Initial computation.
set_home(HOME)

# Known cache/dev artifact subdir names we want to flag specifically.
DEV_ARTIFACT_NAMES = {
    "node_modules",
    ".cache",
    ".npm",
    ".cargo",
    ".pub-cache",
    ".dart-tool",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "target",  # rust build output
    "build",  # generic
    ".next",  # next.js
    ".gradle",
    "DerivedData",
}

# Chromium-family vendors — clearing these caches breaks sign-in.
BROWSER_VENDORS = {
    "Google",
    "Chromium",
    "BraveSoftware",
    "Microsoft Edge",
    "Vivaldi",
    "Arc",
}


def real(p):
    """Resolve symlinks/firmlinks for path comparisons."""
    return os.path.realpath(p)


def is_under(path, ancestor):
    """True if `path` is the same as or under `ancestor` (handles APFS firmlinks)."""
    path = real(path)
    ancestor = real(ancestor)
    if path == ancestor:
        return True
    try:
        return os.path.commonpath([path, ancestor]) == ancestor
    except ValueError:
        return False


def common_parent(path, candidates):
    """Return the candidate that is an ancestor of `path`, or None."""
    path = real(path)
    for c in candidates:
        c = real(c)
        if path == c or os.path.commonpath([path, c]) == c:
            return c
    return None
