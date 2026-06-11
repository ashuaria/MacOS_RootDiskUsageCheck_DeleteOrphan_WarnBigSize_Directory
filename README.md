# macscan

A read-only macOS root-disk usage checker. Surfaces big items in your home
directory, identifies the app that owns each one, flags orphans (leftover
support files for apps you've uninstalled, stale caches, `~/.Trash` contents,
old `~/Downloads`, dev artifacts), and proposes a non-destructive action.

**macscan never deletes anything by default.** It only reports. The TUI has an opt-in `Ctrl+D` delete (described below) gated by a typed confirmation — that is the only way the tool touches the filesystem, and the CLI/JSON/HTML outputs are still read-only.

## Why

The manual workflow is slow: `cd ~ && du -d1 -h` → sort → drill into biggest →
identify owning app → decide. macscan automates that, with cross-referenced
app lookups so you can tell at a glance whether a 5 GB `~/Library/Caches/X`
is from an app you still have or an app you uninstalled months ago.

## Install

Stdlib only — no `pip install` needed.

```bash
git clone <this repo>
cd MacOS_RootDiskUsageCheck_DeleteOrphan_WarnBigSize_Directory
python3 -m macscan --help
```

Optionally install as a console script:

```bash
pip install -e .
macscan scan
```

## Usage

```bash
# Default: scan ~, top 10 drill-down, 10 MB threshold, colored CLI table
python3 -m macscan

# Tighter scan
python3 -m macscan scan --top 5 --threshold 500M

# Interactive TUI: spacebar to select items, ←/→ to scroll, ESC to quit
python3 -m macscan --tui

# Scan the whole boot volume (cross-volume items are excluded)
python3 -m macscan scan --root / --top 20

# Add more app directories to cross-reference (comma-separated)
python3 -m macscan scan --apps-paths /opt/homebrew/Caskroom

# Machine-readable output
python3 -m macscan scan --json report.json
python3 -m macscan scan --html report.html

# Inspect the app registry (debug)
python3 -m macscan list-apps
```

## Output

Four formats:

- **CLI** (default) — colored ASCII table. One finding per line; columns are
  `SIZE | ACTION | CATEGORY | OWNER | WHAT | REASON | PATH`. The `WHAT` column
  gives a one-line description of the category (e.g. "App cache (app
  uninstalled)" or "iOS device backup") so you can read each line at a glance
  without having to dig into the file system.
- **TUI** (`--tui`) — interactive curses UI. One finding per row, with
  selectable checkboxes. Up/Down navigates rows; Left/Right scrolls the wide
  table horizontally (use this to read the full path or `REASON` column);
  Space toggles selection; the bottom bar shows the running total size of
  selected items; `a` selects all, `n` clears the selection, Page Up/Down and
  Home/End jump; `q` or ESC quits. By default the TUI is read-only — selecting
  items does not delete anything, it just lets you sum up what you'd recover.
  `Ctrl+D` opens a confirm dialog (you must type the literal word `delete` and
  press Enter) that removes the selected items from disk. ESC at any point
  cancels; this is the only path in macscan that mutates the filesystem.
- **JSON** (`--json PATH`) — one object per finding, fields: `path, size_kb,
  size_human, category, what, owner, action, reason, is_orphan`.
- **HTML** (`--html PATH`) — single self-contained file, sortable columns
  (Size, Category, Owner, What, Action, Reason, Path), embedded CSS+JS, no
  network requests.

## Actions

| Action             | Meaning                                                              |
| ------------------ | -------------------------------------------------------------------- |
| `KEEP_ACTIVE`      | Leave it. App is installed and the item is normal.                  |
| `REVIEW`           | Look at it. Could be useful data, could be junk. Reason explains.    |
| `CLEAR_CACHE`      | Safe to delete — apps rebuild caches on next launch.                 |
| `REVIEW_TRASH`     | It's in `~/.Trash`. Empty the trash when you're ready.              |
| `UNINSTALL_APP`    | App is no longer installed; this directory is its leftover data.    |

`is_orphan=true` is set on findings that look like uninstalled-app leftovers.

## How orphan detection works

1. **App registry** — on first run, macscan walks `/Applications`,
   `/System/Applications`, `~/Applications`, and any path you give with
   `--apps-paths`. For each `.app` it reads `Contents/Info.plist`
   (via stdlib `plistlib`) to extract the bundle ID and bundle name. The
   registry is cached at `~/.cache/macscan/registry.json` and rebuilt
   automatically when the apps dirs change.
2. **Two-pass scan** — pass 1: `du -d 2 -k -x ~` to find big top-level
   items. Pass 2: `du -d 4 -k -x` on the top-K to drill in. `-x` keeps the
   scan on a single filesystem (critical because `~/Library` is an APFS
   firmlink and external drives are on separate volumes).
3. **Classification** — for each scanned item, the analyzer applies rules
   in priority order. Each rule assigns a `category`; the `WHAT` column
   in the output maps the category to a one-line human description:
   - `ios_backup` — "iOS device backup"
   - `xcode_derived` — "Xcode build cache"
   - `xcode_simulator` — "iOS simulator device"
   - `cloud_mirror` — "iCloud mirror (Desktop & Documents)"
   - `app_support` — "App support files"
   - `app_support_orphan` — "App support files (app uninstalled)"
   - `app_support_unknown` — "App support files (app not in registry)"
   - `cache` / `cache_orphan` — "App cache" / "App cache (app uninstalled)"
   - `container` / `container_orphan` — sandbox containers
   - `group_container` / `group_container_orphan` — group containers
   - `trash` — "Item in ~/.Trash"
   - `old_download` — "Old download (>90 days)"
   - `dev_artifact` — "Dev/cache artifact (re-installable)"
   - `app_bundle` — "Inside an app bundle"
   - `user_data` — "Your data (Documents/Movies/etc.)"
   - `browser_profile` — "Browser profile cache"
   - `unknown` — no heuristic matched
4. **Saved Application State** is used as a "definitely installed" signal —
   if a bundle id has a `~/Library/Saved Application State/<id>.savedState`
   entry, the app is treated as installed even if the `.app` itself is gone.

## Limitations

- macOS only. Uses BSD `du` and the macOS `~/Library` layout.
- `du -x` excludes cross-volume items. If your home is on one APFS volume
  and your apps on another, the apps won't appear in the home scan. Use
  `--root /` to scan the boot volume instead.
- Some apps (rare) have non-standard `Info.plist` files that `plistlib`
  can't parse. macscan logs these and skips them — you'll see fewer apps
  in `list-apps` if any are broken.
- Quarantine xattr is checked but only used as a soft signal in v1; it does
  not yet bias the recommendation.
- iOS simulator devices, `DerivedData`, etc. are flagged `REVIEW` — read the
  reason before deleting. Deleting `DerivedData` is safe; deleting the wrong
  simulator runtime is annoying.
- No live process detection. An app's `.app` may be present but not running;
  the tool can't tell.

## Project layout

```
macscan/
  __init__.py
  __main__.py        # entry — `python -m macscan`
  paths.py           # well-known macOS path constants
  scan.py            # two-pass du scan + parser
  apps.py            # app registry: Info.plist extraction + cache
  analyze.py         # classifier: pure functions, no I/O
  report.py          # CLI table, JSON, HTML
  cli.py             # argparse + orchestration
tests/
  test_scan.py
  test_analyze.py
pyproject.toml
```

## Tests

```bash
python3 -m unittest discover -s tests
```

17 unit tests cover the `du` parser and every classifier branch (caches,
containers, group containers with team-id prefix, iOS backups, trash, dev
artifacts, etc.) using a synthetic home and a stub registry — no real
filesystem I/O. (Some `test_analyze.py` cases still construct `ScanItem`
without `depth_from_root`; those fail until updated — see CLAUDE.md.)

## License

MIT.
