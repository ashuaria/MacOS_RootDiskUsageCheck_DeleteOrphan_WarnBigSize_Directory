"""macscan CLI: argparse, orchestration, output dispatch."""

import argparse
import os
import sys

from macscan import analyze, apps, paths, report, scan


def _parse_size(s):
    """Parse a size string like '1M', '500K', '2G' into KB."""
    if not s:
        return 1024
    s = s.strip().upper()
    if s.endswith("K"):
        return int(s[:-1])
    if s.endswith("M"):
        return int(s[:-1]) * 1024
    if s.endswith("G"):
        return int(s[:-1]) * 1024 * 1024
    return int(s)


def _add_scan_flags(p):
    """Attach scan-related flags to a parser."""
    p.add_argument("--root", default=os.path.expanduser("~"),
                   help="directory to scan (default: ~)")
    p.add_argument("--top", type=int, default=10,
                   help="drill into the top N items from pass 1 (default: 10)")
    p.add_argument("--threshold", default="10M",
                   help="minimum size to report (default: 10M)")
    p.add_argument("--max-depth", type=int, default=4,
                   help="drill-down depth (default: 4)")
    p.add_argument("--json", dest="json_path", default=None,
                   help="write JSON output to this path")
    p.add_argument("--html", dest="html_path", default=None,
                   help="write self-contained HTML report to this path")
    p.add_argument("--no-color", action="store_true",
                   help="disable ANSI color in CLI output")
    p.add_argument("--tui", action="store_true",
                   help="launch interactive terminal UI (curses): "
                        "space to select, ←/→ to scroll, ESC to quit")
    p.add_argument("--apps-paths", default="",
                   help="comma-separated additional app directories to cross-reference")
    p.add_argument("--rebuild-registry", action="store_true",
                   help="ignore the registry cache and rebuild it")
    p.add_argument("--cache-dir", default=None,
                   help="directory for the app registry cache")


def _build_parser():
    p = argparse.ArgumentParser(
        prog="macscan",
        description=(
            "macOS root-disk usage checker. Scans for big items, identifies the "
            "app that owns each, flags orphans (uninstalled-app leftovers, stale "
            "caches, ~/.Trash, old Downloads, dev artifacts), and proposes an "
            "action. Read-only — never deletes anything."
        ),
    )
    # Scan flags live at the top level so `python3 -m macscan` works without
    # an explicit subcommand. The `scan` subcommand below is a no-op alias.
    _add_scan_flags(p)

    sub = p.add_subparsers(dest="cmd")

    scan_p = sub.add_parser("scan", help="scan the disk and report (default)")
    _add_scan_flags(scan_p)

    list_p = sub.add_parser("list-apps",
                            help="show the registry of installed apps (debug)")
    list_p.add_argument("--apps-paths", default="",
                        help="comma-separated additional app directories to cross-reference")
    list_p.add_argument("--rebuild-registry", action="store_true")
    list_p.add_argument("--cache-dir", default=None)

    return p


def _make_registry(args):
    extra = [p for p in (args.apps_paths or "").split(",") if p.strip()]
    app_dirs = list(paths.DEFAULT_APP_DIRS) + extra
    return apps.build(
        app_dirs=app_dirs,
        cache_dir=getattr(args, "cache_dir", None),
        rebuild=getattr(args, "rebuild_registry", False),
    )


def _cmd_scan(args):
    if args.no_color:
        report.USE_COLOR = False

    threshold_kb = _parse_size(args.threshold)

    root = os.path.realpath(args.root)
    if root == "/":
        print(
            "warning: --root / scans the boot volume; cross-volume items are "
            "excluded (du -x). Some system paths may be inaccessible.",
            file=sys.stderr,
        )

    with report.Progress("Building app registry..."):
        registry = _make_registry(args)

    with report.Progress(f"Scanning {root}..."):
        result = scan.scan(root, top=args.top, max_depth_pass2=args.max_depth)

    findings = []
    node_by_path = (
        {node.path: node for node in result.tree_root.all_nodes()}
        if result.tree_root is not None else {}
    )
    for item in result.items:
        f = analyze.classify(item, registry, threshold_kb=threshold_kb)
        if f is not None:
            findings.append(f)
            node = node_by_path.get(item.path)
            if node is not None:
                node.finding = f

    # Hide directories below the threshold from the tree view. They
    # produced no finding; without pruning they'd still render as
    # generic "directory" rows in the TUI/CLI table.
    if result.tree_root is not None:
        result.tree_root.prune_unclassified()

    findings.sort(key=lambda f: (not f.is_orphan, -f.size_kb))

    if args.json_path:
        report.render_json(findings, args.json_path, tree_root=getattr(result, "tree_root", None))
        print(f"wrote {args.json_path}", file=sys.stderr)
    if args.html_path:
        report.render_html(findings, args.html_path, tree_root=getattr(result, "tree_root", None))
        print(f"wrote {args.html_path}", file=sys.stderr)
    if args.tui:
        report.render_interactive(findings, tree_root=getattr(result, "tree_root", None))
    if not (args.json_path or args.html_path or args.tui):
        report.render_tree_table(findings, getattr(result, "tree_root", None))

    if result.inaccessible:
        print(
            f"\nwarning: {len(result.inaccessible)} inaccessible path(s):",
            file=sys.stderr,
        )
        for line in result.inaccessible[:10]:
            print(f"  {line}", file=sys.stderr)
        if len(result.inaccessible) > 10:
            print(f"  ... and {len(result.inaccessible) - 10} more", file=sys.stderr)

    return 0


def _cmd_list_apps(args):
    with report.Progress("Building app registry..."):
        registry = _make_registry(args)
    print(f"# {len(registry)} app(s) across {len(registry.sources)} source(s):")
    for s in registry.sources:
        print(f"#   {s}")
    print()
    print(f"{'NAME':<32} {'BUNDLE ID':<48} PATH")
    for app in registry.all_apps():
        print(f"{app.name[:32]:<32} {app.bundle_id[:48]:<48} {app.path}")
    return 0


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "list-apps":
        return _cmd_list_apps(args)
    return _cmd_scan(args)


if __name__ == "__main__":
    sys.exit(main())
