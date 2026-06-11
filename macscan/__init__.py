"""macscan — macOS root disk usage checker.

Scans your home directory (and other roots) for big items, identifies the app
that owns each one, flags orphans (app support files left behind by uninstalled
apps, stale caches, old downloads, .Trash contents, dev artifacts), and proposes
a non-destructive action for each.

Read-only: never deletes anything.
"""

__version__ = "0.1.0"
