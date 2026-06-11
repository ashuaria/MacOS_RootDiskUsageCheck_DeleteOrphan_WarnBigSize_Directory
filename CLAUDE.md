# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`macscan` is a **read-only by default** macOS root-disk usage checker. It scans a directory (default `~`) using BSD `du`, classifies each item against an installed-app registry, and recommends an action (`KEEP_ACTIVE`, `REVIEW`, `CLEAR_CACHE`, `REVIEW_TRASH`, `UNINSTALL_APP`). All CLI/JSON/HTML outputs are read-only. The interactive TUI has an **opt-in destructive delete** triggered by `Ctrl+D` and gated by a typed "delete" confirmation modal — that is the only path in macscan that mutates the filesystem. macOS-only: relies on BSD `du`, `xattr`, the macOS `~/Library` layout, and `plistlib` on `.app/Contents/Info.plist`.

## Commands

```bash
# Run from the repo root. Stdlib only — no install step required.
python3 -m macscan                    # default scan: ~, top 10, 10MB threshold
python3 -m macscan scan --top 5 --threshold 500M
python3 -m macscan --tui              # interactive curses UI
python3 -m macscan list-apps          # dump the installed-app registry
python3 -m macscan scan --json out.json --html out.html

# Tests
python3 -m unittest discover -s tests
# Single test file:
python3 -m unittest tests.test_analyze
# Single test case / method:
python3 -m unittest tests.test_analyze.TestClassify.test_cache_orphan

# Optional editable install (exposes `macscan` console script)
pip install -e .
```

There is **no linter, formatter, or type checker** configured in `pyproject.toml` — keep style consistent with the existing modules (PEP 8, stdlib only, `from __future__` not used, type hints only on a few function signatures).

## Architecture

The pipeline is strictly one-way: `scan` → `analyze` → `report`. Each layer is a separate module with a small public surface.

```
cli.py  ── orchestrates everything; argparse + subcommands (`scan`, `list-apps`)
  │
  ├─► apps.build()                  # registry of installed .app bundles
  │     ├─ walks /Applications, /System/Applications, ~/Applications, --apps-paths
  │     ├─ reads Contents/Info.plist (plistlib) → name + bundle_id
  │     ├─ checks com.apple.quarantine xattr (soft signal only)
  │     ├─ reads ~/Library/Saved Application State/*.savedState → "definitely installed" set
  │     └─ caches to ~/.cache/macscan/registry.json + .sig (auto-rebuild when apps dirs change)
  │
  ├─► scan.scan(root, top, max_depth_pass2)
  │     ├─ pass 1: du -d 2 -k -x <root>           # find big top-level items
  │     ├─ pick top-K, pass 2: du -d 4 -k -x <each>  # drill in
  │     ├─ dedupe pass-1 against pass-2 output
  │     └─ _build_tree() → TreeNode hierarchy (own_kb + accumulated size_kb)
  │
  ├─► analyze.classify(scan_item, registry, threshold_kb)
  │     ├─ PURE FUNCTIONS only — no I/O, easy to test
  │     ├─ _apply_rules() checks paths against well-known Library subdirs
  │     ├─ resolves owner by app name (fuzzy) or bundle id (with suffix stripping)
  │     └─ emits a Finding namedtuple (path, size_kb, category, owner, action, reason, is_orphan, depth_from_root)
  │
  └─► report.*                       # output dispatch
        ├─ render_tree_table()       # default CLI: hierarchical ASCII tree, color
        ├─ render_interactive()      # --tui: curses, scroll/select/expand
        ├─ render_json() / render_html()
        └─ COLUMNS / COLUMNS_TREE    # shared column layout for CLI + TUI
```

### Module-by-module notes

- **`paths.py`** — single source of truth for `~/Library/{Application Support,Caches,Containers,Group Containers,Saved Application State,Developer,Mobile Documents}`, `~/.Trash`, `~/Downloads`, `~/Documents`, etc. `set_home(new_home)` re-derives every constant and **mutates module-level globals**; tests use this to point everything at a synthetic home. Also defines `DEV_ARTIFACT_NAMES` (node_modules, .cache, .cargo, target, DerivedData, …), `BROWSER_VENDORS` (Chromium family), and the `is_under` / `common_parent` / `real` helpers (APFS firmlink-aware).
- **`scan.py`** — runs `du` via `subprocess.run([...])`, captures stderr so permission warnings are surfaced (not hidden). `parse_du_output()` is split out for testing. `TreeNode` is the in-memory tree used by the TUI for expand/collapse; nodes carry `own_kb`, accumulated `size_kb`, and an attached `finding` (set after classification). `TreeNode.remove_subtree(path)` removes a node and decrements every ancestor's `size_kb` — used by the TUI's `Ctrl+D` delete path. `_build_tree` accumulates sizes deepest-first so the post-build `size_kb` reflects the actual `du` total.
- **`apps.py`** — `Registry` exposes `by_name()`, `by_bundle_id()`, `is_installed_signal(bid)`, `all_apps()`, `to_dict()/from_dict()`. Cache invalidation uses a **signature file** listing each apps dir + the `.app` filenames inside — rebuilt automatically when the dir contents change.
- **`analyze.py`** — `Finding` is a namedtuple; `ACTION_*` constants and `CATEGORY_WHAT` (the human "WHAT" description for each category) are exported. The rule order in `_apply_rules` matters: iOS backups → Xcode → iCloud → Application Support → Caches → Containers → Group Containers → Trash → Downloads (mtime check) → dev artifacts → inside `.app` bundle → user data dirs → Chromium browser profile. **The classifier is pure — no subprocess, no `os.stat` except the mtime helpers `_stale`/`_mtime_days`.** When adding a new rule, add a category, a `CATEGORY_WHAT` entry, and a test.
- **`report.py`** — `USE_COLOR` defaults to `sys.stdout.isatty() and NO_COLOR not set`; `--no-color` flips it. The TUI is read-only by default — selecting items just sums their sizes. `Ctrl+D` opens a confirm modal that requires the user to type the literal word `delete` and press Enter, then calls `shutil.rmtree` (dirs) / `os.remove` (files) and `TreeNode.remove_subtree` to keep the in-memory tree consistent; results are shown in a results modal. The HTML report is a single self-contained file (no network), sortable via inline JS. The `curses` import is **lazy** (inside `render_interactive`) so the rest of the module loads in non-tty environments.

### Testing

Tests live in `tests/` and use only `unittest`. Notable patterns:

- `tests/test_analyze.py` re-points `paths` at `/Users/u` via `paths.set_home()` in `setUp` and resets it in `tearDown` — so the classifier can be exercised with synthetic paths against a `StubRegistry` (no real filesystem I/O).
- `StubRegistry` (in `test_analyze.py`) is the only fixture; `tests/test_scan.py` only exercises `parse_du_output()` (no `du` invocation in tests).
- Adding a new category? Update `CATEGORY_WHAT` in `analyze.py` and the `test_category_what_covers_every_category_in_findings` test will catch any missing description.
- There is **no test coverage** for `report.py` (TUI/HTML/JSON output) or `apps.py` (registry build/cache). Total: 24 tests (9 scan, 15 analyze); 11 pass, 13 pre-existing failures from the `ScanItem` field addition — fix those before counting on the suite for regression protection.

## Gotchas

- **macOS only.** `scan.py` calls `/usr/bin/du` and raises `RuntimeError` if absent. `apps.py` uses `/usr/bin/xattr`. `analyze.py`'s `~/Library` heuristics assume Apple's default layout.
- **`du -x` excludes cross-volume items.** If `~/Library` lives on a different APFS volume from `/Applications`, the home scan won't show app leftovers in the system app dirs — use `--root /` to scan the boot volume.
- **Adding fields to `ScanItem` or `Finding`?** They are namedtuples. Update the definition, every test that constructs one, and any `dataclass`-style usage in `analyze.py` / `report.py`.
- **Path comparisons must go through `paths.is_under` / `paths.real`** to handle APFS firmlinks. Plain `os.path.commonpath` will miscompare paths that cross the firmlink boundary.
- The current test suite has **pre-existing failures**: 13 tests in `test_analyze.py` construct `ScanItem` with only 4 args, but `ScanItem` now has 5 fields (`depth_from_root` was added). The tests still need to be updated to pass `depth_from_root=0` in `_item()`. This is unrelated to the core logic — fix it before adding new tests in that file.
- **Destructive delete (`Ctrl+D` in TUI).** The only mutating path in macscan. It is gated by a modal that requires the user to type the literal word `delete` and press Enter, and the same path is opt-in (must press `Ctrl+D` first). Selection is deduped so deleting a parent doesn't try to also delete its descendants. If the modal or delete path throws, the in-memory tree may end up out of sync with disk — quit and re-run `macscan` to rebuild it.
