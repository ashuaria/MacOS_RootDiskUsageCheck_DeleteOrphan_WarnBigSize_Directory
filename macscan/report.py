"""Output formatters: ANSI table, JSON, self-contained HTML, progress bar."""

import html
import json
import os
import shutil
import sys
import threading
import time

from macscan import analyze

# ANSI color helpers. Respect NO_COLOR env var and non-TTY stdout.
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


def _color(s, color):
    if not USE_COLOR:
        return s
    return f"{color}{s}{RESET}"


def _action_color(action):
    return {
        analyze.ACTION_KEEP_ACTIVE: DIM,
        analyze.ACTION_REVIEW: YELLOW,
        analyze.ACTION_CLEAR_CACHE: GREEN,
        analyze.ACTION_REVIEW_TRASH: MAGENTA,
        analyze.ACTION_UNINSTALL_APP: RED,
    }.get(action, RESET)


def finding_to_dict(f):
    return {
        "path": f.path,
        "size_kb": f.size_kb,
        "size_human": analyze.humanize_size_kb(f.size_kb),
        "category": f.category,
        "what": analyze.category_what(f.category),
        "owner": f.owner,
        "action": f.action,
        "reason": f.reason,
        "is_orphan": f.is_orphan,
    }


# Column layout shared by the table and the TUI. Each entry is
# (header, width, getter_or_None). The path column is the wide trailing
# column in the table; the TUI presents them all side-by-side with
# horizontal scroll.
COLUMNS = [
    ("SIZE",     7,  lambda f: analyze.humanize_size_kb(f.size_kb)),
    ("ACTION",   14, lambda f: f.action),
    ("CATEGORY", 22, lambda f: f.category),
    ("OWNER",    24, lambda f: f.owner or "-"),
    ("WHAT",     30, lambda f: analyze.category_what(f.category)),
    ("REASON",   40, lambda f: f.reason),
    ("PATH",     60, lambda f: f.path),
]
COLUMN_SEP = 2  # spaces between columns

# Per-level indent for the TUI tree prefix (matches the CLI table).
TREE_INDENT = 2


def _is_dir(path):
    return os.path.isdir(path)


def _tree_lines(node, prefix="", is_last=True, show_root=True, indent=TREE_INDENT):
    """Yield (indent_prefix, node) tuples for a pretty-printed ASCII tree.

    `prefix` carries the vertical bars for ancestors; `is_last` controls
    whether this node gets `└──` (last child) or `├──` (has next sibling).
    `indent` is the per-level indent in spaces (default 2) so each level
    visibly steps to the right of its parent. `show_root` is False when
    the root itself is omitted (rendered separately).
    """
    if show_root and not prefix:
        # The root is rendered at the call site; descendants get connectors.
        if node.children:
            yield ("", node)
            last_idx = len(node.children) - 1
            for i, child in enumerate(node.children):
                yield from _tree_lines(
                    child,
                    prefix=" " * indent,
                    is_last=(i == last_idx),
                    show_root=False,
                    indent=indent,
                )
        else:
            yield ("", node)
        return

    connector = "└─ " if is_last else "├─ "
    yield (prefix + connector, node)

    if node.children and node.expanded:
        # "parent closed" → blank gutter under this node; otherwise keep
        # the vertical bar. The 3-char width of the extension already
        # aligns with the connector, so we don't add another `indent`
        # here — that would push descendants two extra spaces off.
        extension = "   " if is_last else "│  "
        last_idx = len(node.children) - 1
        for i, child in enumerate(node.children):
            yield from _tree_lines(
                child,
                prefix=prefix + extension,
                is_last=(i == last_idx),
                show_root=False,
                indent=indent,
            )


def render_tree_table(findings, tree_root, stream=None):
    """Render the scan as an ASCII tree table (hierarchical).

    One row per node. SIZE = own_kb of that node (so distribution is visible
    at every level). PATH is indented with `├──` / `└──` connectors.
    """
    stream = stream or sys.stdout

    if tree_root is None:
        if not findings:
            stream.write("No items matched the threshold.\n")
            return
        return render_table(findings, stream=stream)

    rows = []
    for prefix, node in _tree_lines(tree_root, show_root=True):
        finding = node.finding
        if finding is None:
            category = ""
            what = node.path
            action = analyze.ACTION_KEEP_ACTIVE if node.children else analyze.ACTION_KEEP_ACTIVE
            owner = ""
            reason = "directory"
            is_dir_node = bool(node.children)
        else:
            category = finding.category
            what = analyze.category_what(finding.category)
            action = finding.action
            owner = finding.owner or ""
            reason = finding.reason
            is_dir_node = bool(node.children)

        size_str = analyze.humanize_size_kb(node.own_kb) if node.own_kb else "dir"
        size_str = _color(size_str, GREEN if is_dir_node else DIM)
        action_str = _color(action, _action_color(action))
        indicator = "[-]" if node.children and node.expanded else "[+]" if node.children else "   "

        rows.append([
            prefix + indicator,
            size_str,
            _color(category, CYAN),
            _color(owner or "-", DIM),
            what,
            reason,
            node.path,
        ])

    # Column widths
    widths = [5, 7, 22, 24, 30, 40, 60]
    headers = ["", "SIZE", "ACTION", "CATEGORY", "OWNER", "WHAT", "REASON", "PATH"]

    # Recompute widths from content
    for i, w in enumerate(widths):
        widths[i] = max(widths[i], len(headers[i]))
        for row in rows:
            widths[i] = max(widths[i], min(len(str(row[i])), 200 if i == len(widths) - 1 else w + 40))

    # Path absorbs terminal width
    try:
        term_w = shutil.get_terminal_size((120, 20)).columns
    except Exception:
        term_w = 120
    fixed = sum(widths[:-1]) + COLUMN_SEP * (len(widths) - 1)
    path_max = max(20, term_w - fixed - 1)
    widths[-1] = min(widths[-1], path_max)

    def fmt_cell(s, w, col_name):
        s = str(s)
        if col_name == "PATH" and len(s) > w:
            s = "…" + s[-(w - 1) :]
        return s.ljust(w)

    def fmt_row(cells, action):
        out = []
        for i, c in enumerate(cells):
            cell = fmt_cell(c, widths[i], COLUMNS_TREE[i][0] if i < len(COLUMNS_TREE) else "PATH")
            out.append(cell)
        return (" " * COLUMN_SEP).join(out)

    # Header
    header_cells = [h.ljust(widths[i]) for i, h in enumerate(headers)]
    stream.write(_color((" " * COLUMN_SEP).join(header_cells), BOLD) + "\n")
    stream.write(_color((" " * COLUMN_SEP).join("-" * w for w in widths), DIM) + "\n")
    for row in rows:
        stream.write(fmt_row(row, row[1]) + "\n")


COLUMNS_TREE = [
    ("", 5, lambda f: ""),
    ("SIZE", 7, lambda f: analyze.humanize_size_kb(f.size_kb)),
    ("ACTION", 14, lambda f: f.action),
    ("CATEGORY", 22, lambda f: f.category),
    ("OWNER", 24, lambda f: f.owner or "-"),
    ("WHAT", 30, lambda f: analyze.category_what(f.category)),
    ("REASON", 40, lambda f: f.reason),
    ("PATH", 60, lambda f: f.path),
]


def render_json(findings, path):
    """Write findings as JSON to `path`."""
    data = {
        "findings": [finding_to_dict(f) for f in findings],
        "count": len(findings),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def render_html(findings, path):
    """Write findings as a self-contained sortable HTML report."""
    rows_html = []
    for f in findings:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(analyze.humanize_size_kb(f.size_kb))}</td>"
            f"<td>{html.escape(f.category)}</td>"
            f"<td>{html.escape(f.owner or '-')}</td>"
            f"<td>{html.escape(analyze.category_what(f.category))}</td>"
            f"<td class='action action-{html.escape(f.action.lower())}'>{html.escape(f.action)}</td>"
            f"<td class='reason'>{html.escape(f.reason)}</td>"
            f"<td class='path'>{html.escape(f.path)}</td>"
            "</tr>"
        )

    html_doc = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>macscan report</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #eee; vertical-align: top; }}
  th {{ background: #f5f5f7; cursor: pointer; user-select: none; position: sticky; top: 0; }}
  th:hover {{ background: #ececef; }}
  td.path {{ font-family: ui-monospace, Menlo, monospace; font-size: 12px; word-break: break-all; }}
  td.reason {{ color: #555; }}
  .action-keep_active {{ color: #888; }}
  .action-review {{ color: #b58900; }}
  .action-clear_cache {{ color: #2e7d32; }}
  .action-review_trash {{ color: #8e44ad; }}
  .action-uninstall_app {{ color: #c62828; font-weight: 600; }}
  tr:hover {{ background: #fafafa; }}
  .summary {{ margin: 0 0 12px; color: #555; font-size: 13px; }}
</style>
</head><body>
<h1>macscan report — {len(findings)} item(s)</h1>
<p class="summary">Click a column header to sort. Read-only: macscan never deletes anything. (The interactive TUI has an opt-in Ctrl+D delete gated by typed confirmation; HTML/JSON outputs are always read-only.)</p>
<table id="t">
<thead><tr>
  <th data-key="0">Size</th>
  <th data-key="1">Category</th>
  <th data-key="2">Owner</th>
  <th data-key="3">What</th>
  <th data-key="4">Action</th>
  <th data-key="5">Reason</th>
  <th data-key="6">Path</th>
</tr></thead>
<tbody>
{''.join(rows_html)}
</tbody>
</table>
<script>
  const table = document.getElementById('t');
  let sortDir = {{}};
  for (const th of table.querySelectorAll('th')) {{
    th.addEventListener('click', () => {{
      const key = parseInt(th.dataset.key, 10);
      const tbody = table.querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      sortDir[key] = !sortDir[key];
      rows.sort((a, b) => {{
        const av = a.children[key].innerText;
        const bv = b.children[key].innerText;
        const cmp = av.localeCompare(bv, undefined, {{numeric: true}});
        return sortDir[key] ? cmp : -cmp;
      }});
      for (const r of rows) tbody.appendChild(r);
    }});
  }}
</script>
</body></html>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_doc)


def render_interactive(findings, stream=None, tree_root=None):
    """Curses-based interactive TUI.

    Shows findings either as a flat list (no tree_root) or as an
    expandable directory tree (tree_root provided).
    """
    if not findings:
        print("No items matched the threshold.")
        return

    import curses

    has_tree = tree_root is not None
    if has_tree:
        tree_root.expanded = True
        for n in tree_root.all_nodes():
            n.expanded = True

    def all_visible_nodes():
        if not has_tree:
            return list(findings)
        return [node for node, _ in tree_root.visible_rows(True)][1:]

    def total_selected_kb(sel):
        if not has_tree:
            return sum(findings[i].size_kb for i in sel)
        selected_paths = {visible_nodes[i].path for i in sel}
        total = 0
        for i in sel:
            node = visible_nodes[i]
            if _ancestor_selected(node.path, selected_paths):
                continue
            total += node.own_kb or node.size_kb
        return total

    def _ancestor_selected(path, selected_paths):
        parent = os.path.dirname(path)
        # os.path.dirname("/") == "/" on POSIX, so `while parent` would
        # never terminate at the root; also stop when dirname stops changing.
        while parent and parent != os.path.dirname(parent):
            if parent in selected_paths:
                return True
            parent = os.path.dirname(parent)
        return False

    def _compute_row_prefixes():
        """Return a list of prefix strings (one per visible row) that
        include both the per-level indent and the connector (`├─` /
        `└─`). The TUI prepends the prefix + a `[ ]` checkbox to each
        row, mirroring `tree(1)` and giving the user a clear visual
        sense of where each item sits in the directory tree.
        """
        if not has_tree:
            return [""] * len(visible_nodes)
        # Reuse _tree_lines: it walks the same tree with the same
        # expand/collapse state and yields (prefix, node). The first
        # entry is the root, which visible_nodes also drops, so zip
        # the rest 1:1.
        prefixes = [prefix for prefix, _ in _tree_lines(tree_root, show_root=True)][1:]
        if len(prefixes) != len(visible_nodes):
            # Shouldn't happen — both come from the same tree walk —
            # but pad/truncate defensively so a mismatch can't crash
            # the TUI.
            prefixes = (prefixes + [" " * TREE_INDENT] * len(visible_nodes))[:len(visible_nodes)]
        return prefixes

    def _topmost_selected(sel):
        """Return the subset of `sel` whose paths are not descendants of
        another selected path. When the user selects both a parent and a
        child, only the parent's deletion matters; deleting the child
        would either fail (it's already gone) or be misleading.
        """
        paths_sel = {visible_nodes[i].path for i in sel}
        out = []
        for i in sel:
            if not _ancestor_selected(visible_nodes[i].path, paths_sel):
                out.append(i)
        return out

    visible_nodes = []

    def rebuild_visible():
        nonlocal visible_nodes
        if has_tree:
            visible_nodes = [node for node, _ in tree_root.visible_rows(True)][1:]
        else:
            visible_nodes = list(findings)

    rebuild_visible()

    def main(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.keypad(True)

        try:
            curses.start_color()
            curses.use_default_colors()
            has_color = True
        except curses.error:
            has_color = False

        if has_color:
            curses.init_pair(1, -1, -1)
            curses.init_pair(2, curses.COLOR_WHITE, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_GREEN, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)
            curses.init_pair(6, curses.COLOR_RED, -1)
            curses.init_pair(7, curses.COLOR_CYAN, curses.COLOR_BLUE)
            curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)

        def action_attr(action):
            if not has_color:
                return 0
            base = {
                analyze.ACTION_KEEP_ACTIVE:   curses.color_pair(2),
                analyze.ACTION_REVIEW:        curses.color_pair(3),
                analyze.ACTION_CLEAR_CACHE:   curses.color_pair(4),
                analyze.ACTION_REVIEW_TRASH:  curses.color_pair(5),
                analyze.ACTION_UNINSTALL_APP: curses.color_pair(6) | curses.A_BOLD,
            }.get(action, curses.color_pair(1))
            if action == analyze.ACTION_KEEP_ACTIVE:
                base |= curses.A_DIM
            return base

        scroll_columns = [
            ("SIZE",     7,  lambda n: analyze.humanize_size_kb(n.own_kb if has_tree else n.size_kb)),
            ("ACTION",   14, lambda n: n.finding.action if n.finding else "DIR"),
            ("CATEGORY", 22, lambda n: n.finding.category if n.finding else "directory"),
            ("OWNER",    24, lambda n: n.finding.owner if n.finding and n.finding.owner else "-"),
            ("WHAT",     30, lambda n: analyze.category_what(n.finding.category) if n.finding else ""),
            ("REASON",   40, lambda n: n.finding.reason if n.finding else ""),
            ("PATH",     60, lambda n: n.path),
        ]
        col_sep = 2
        # The TUI prefix column: `[ ] ` (4 chars including the trailing
        # space) plus a worst-case tree prefix (deepest tree we ever
        # render is bounded by --max-depth pass 2 = 4 levels deep;
        # 4 * 4 chars/level = 16). Budget 24 chars to leave headroom.
        prefix_col_width = (4 if has_tree else 3) + (16 if has_tree else 0)
        total_width = prefix_col_width + sum(c[1] for c in scroll_columns) + col_sep * len(scroll_columns)

        selected = set()
        cursor = 0
        x_offset = 0

        def make_row(idx, prefix, width):
            node = visible_nodes[idx]
            finding = node.finding if hasattr(node, "finding") else None
            if finding:
                action = finding.action
            else:
                action = analyze.ACTION_KEEP_ACTIVE
            mark = "[o]" if idx in selected else "[ ]"
            # The tree-line prefix puts the checkbox in the leftmost
            # column and the connector next to it, e.g. `[ ]  ├─ foo`.
            tree_prefix = f"{mark} {prefix}"
            parts = [tree_prefix]
            for name, w, getter in scroll_columns:
                val = str(getter(node))
                if len(val) > w:
                    val = val[: w - 1] + "…"
                parts.append(val.ljust(w))
            return (" " * col_sep).join(parts)

        def prune_selection():
            for idx in list(selected):
                if idx >= len(visible_nodes):
                    selected.discard(idx)

        def show_confirm_modal(stdscr, prompt, expected):
            """Show a centered modal box; return True iff the user typed
            `expected` and pressed Enter. ESC at any point cancels.
            """
            try:
                max_y, max_x = stdscr.getmaxyx()
            except curses.error:
                return False
            width = max(40, min(max_x - 4, len(prompt) + 8))
            height = 5
            y0 = max(0, (max_y - height) // 2)
            x0 = max(0, (max_x - width) // 2)
            buf = []
            try:
                win = curses.newwin(height, width, y0, x0)
                win.keypad(True)
                win.bkgd(" ", curses.color_pair(6) | curses.A_BOLD)
                win.attron(curses.A_BOLD)
                win.addstr(1, 2, prompt[: width - 4])
                win.attroff(curses.A_BOLD)
                win.addstr(3, 2, "> _")
                win.refresh()
                while True:
                    k = win.getch()
                    if k in (27,):
                        return False
                    if k in (curses.KEY_ENTER, 10, 13):
                        return "".join(buf).strip() == expected
                    if k in (curses.KEY_BACKSPACE, 127, 8):
                        if buf:
                            buf.pop()
                    elif 0 <= k < 256 and chr(k).isprintable():
                        buf.append(chr(k))
                    rendered = "> " + "".join(buf) + "_"
                    win.addstr(3, 2, " " * (width - 4))
                    win.addstr(3, 2, rendered[: width - 4])
                    win.refresh()
            except curses.error:
                return False

        def perform_delete(paths_to_delete):
            """Delete a list of paths from disk. Returns a list of
            (path, error_str_or_None) tuples. Skips paths that no
            longer exist (already gone)."""
            results = []
            for p in paths_to_delete:
                if not os.path.lexists(p):
                    results.append((p, "already gone"))
                    continue
                try:
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                    results.append((p, None))
                except OSError as e:
                    results.append((p, str(e)))
            return results

        def show_results_modal(stdscr, title, lines):
            """Show a simple centered modal with `lines` of text; wait for
            any keypress to dismiss."""
            try:
                max_y, max_x = stdscr.getmaxyx()
            except curses.error:
                return
            width = max(40, min(max_x - 4, max(len(title), max((len(l) for l in lines), default=0)) + 6))
            height = min(max_y - 2, 4 + len(lines))
            y0 = max(0, (max_y - height) // 2)
            x0 = max(0, (max_x - width) // 2)
            try:
                win = curses.newwin(height, width, y0, x0)
                win.keypad(True)
                win.bkgd(" ", curses.color_pair(8) | curses.A_BOLD)
                win.addstr(1, 2, title[: width - 4], curses.A_BOLD)
                for i, line in enumerate(lines):
                    win.addstr(2 + i, 2, line[: width - 4])
                win.addstr(height - 1, 2, "press any key…", curses.A_DIM)
                win.refresh()
                win.getch()
            except curses.error:
                pass

        def draw(stdscr):
            stdscr.erase()
            try:
                max_y, max_x = stdscr.getmaxyx()
            except curses.error:
                return

            if max_y < 3 or max_x < 30:
                try:
                    stdscr.addstr(0, 0, "terminal too small (need 30x3)", curses.A_BOLD)
                    stdscr.refresh()
                except curses.error:
                    pass
                return

            prune_selection()
            body_top = 2
            body_bot = max_y - 2
            visible = max(1, body_bot - body_top)

            page = cursor // max(1, visible)
            first = page * visible
            last = min(first + visible, len(visible_nodes))

            # Compute the tree-line prefix for each visible row in the
            # current page. _compute_row_prefixes() returns a list the
            # same length as visible_nodes; we slice to the page.
            all_prefixes = _compute_row_prefixes()
            page_prefixes = all_prefixes[first:last]

            n_sel = len(selected)
            sel_kb = total_selected_kb(selected)
            sel_text = (
                f"{n_sel} selected · {analyze.humanize_size_kb(sel_kb)} total"
                if n_sel else "nothing selected"
            )
            header = f" macscan · {len(visible_nodes)} item(s) · {sel_text} "
            hints = " ↑/↓ move · ^F/^B page · space select · + expand · - collapse · E expand-all · C collapse-all · ^D delete · ←/→ scroll · a all · n none · q/ESC quit "
            try:
                if has_color:
                    stdscr.addstr(0, 0, header.ljust(max_x)[:max_x],
                                  curses.color_pair(8) | curses.A_BOLD)
                    stdscr.addstr(1, 0, hints.ljust(max_x)[:max_x], curses.A_DIM)
                else:
                    stdscr.addstr(0, 0, header.ljust(max_x)[:max_x], curses.A_BOLD)
                    stdscr.addstr(1, 0, hints.ljust(max_x)[:max_x], curses.A_DIM)
            except curses.error:
                pass

            for i, idx in enumerate(range(first, last)):
                y = body_top + i
                if y >= body_bot:
                    break
                node = visible_nodes[idx]
                row = make_row(idx, page_prefixes[i], max_x)
                view = row[x_offset: x_offset + max_x]
                view = view.ljust(max_x)[:max_x]
                finding = getattr(node, "finding", None)
                action = finding.action if finding else analyze.ACTION_KEEP_ACTIVE
                attr = action_attr(action)
                if idx == cursor:
                    attr = (attr & ~0xFF00) | curses.A_REVERSE
                if idx in selected:
                    attr |= curses.A_BOLD
                try:
                    stdscr.addstr(y, 0, view, attr)
                except curses.error:
                    pass

            max_offset = max(0, total_width + 3 - max_x)
            footer = f" row {cursor + 1}/{len(visible_nodes)}  ·  scroll {x_offset}/{max_offset}  ·  {total_width} cols  "
            try:
                stdscr.addstr(max_y - 1, 0, footer.ljust(max_x)[:max_x], curses.A_DIM)
            except curses.error:
                pass

            try:
                stdscr.refresh()
            except curses.error:
                pass

        while True:
            draw(stdscr)
            key = stdscr.getch()
            max_y, max_x = stdscr.getmaxyx()
            visible = max(1, max_y - 4)
            max_offset = max(0, total_width + 3 - max_x)

            if key in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                cursor = min(len(visible_nodes) - 1, cursor + 1)
            elif key in (curses.KEY_LEFT, ord("h")):
                x_offset = max(0, x_offset - 8)
            elif key in (curses.KEY_RIGHT, ord("l")):
                x_offset = min(max_offset, x_offset + 8)
            elif key in (curses.KEY_NPAGE, 6):  # 6 = Ctrl+F
                cursor = min(len(visible_nodes) - 1, cursor + visible)
            elif key in (curses.KEY_PPAGE, 2):  # 2 = Ctrl+B
                cursor = max(0, cursor - visible)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(visible_nodes) - 1
            elif key == ord(" "):
                if cursor in selected:
                    selected.discard(cursor)
                else:
                    selected.add(cursor)
            elif key in (ord("a"), ord("A")):
                selected = set(range(len(visible_nodes)))
            elif key in (ord("n"), ord("N")):
                selected.clear()
            elif key in (ord("q"), ord("Q"), 27):
                return
            elif key in (ord("+"), ord("=")):
                if 0 <= cursor < len(visible_nodes):
                    node = visible_nodes[cursor]
                    node.expanded = True
                rebuild_visible()
                if cursor >= len(visible_nodes):
                    cursor = max(0, len(visible_nodes) - 1)
            elif key == ord("-"):
                if 0 <= cursor < len(visible_nodes):
                    node = visible_nodes[cursor]
                    node.expanded = False
                rebuild_visible()
                if cursor >= len(visible_nodes):
                    cursor = max(0, len(visible_nodes) - 1)
            elif key in (ord("E"), ord("e")):
                if has_tree:
                    for n in tree_root.all_nodes():
                        n.expanded = True
                rebuild_visible()
                if cursor >= len(visible_nodes):
                    cursor = max(0, len(visible_nodes) - 1)
            elif key in (ord("C"), ord("c")):
                if has_tree:
                    # Collapse all descendants; keep the root's children
                    # visible by leaving the root expanded.
                    for n in tree_root.all_nodes():
                        n.expanded = False
                    tree_root.expanded = True
                rebuild_visible()
                if cursor >= len(visible_nodes):
                    cursor = max(0, len(visible_nodes) - 1)
            elif key == 4:  # Ctrl+D — destructive delete
                top = _topmost_selected(selected)
                if not top:
                    show_results_modal(stdscr, "Nothing selected", ["Select items with Space first."])
                    stdscr.touchwin()
                    stdscr.refresh()
                    continue
                paths_to_delete = [visible_nodes[i].path for i in top]
                size_kb = sum(
                    (visible_nodes[i].own_kb or visible_nodes[i].size_kb) for i in top
                )
                prompt = (
                    f"Delete {len(paths_to_delete)} item(s) "
                    f"({analyze.humanize_size_kb(size_kb)})? "
                    f"Type 'delete' + Enter:"
                )
                if show_confirm_modal(stdscr, prompt, "delete"):
                    results = perform_delete(paths_to_delete)
                    if has_tree:
                        for p, err in results:
                            if err is None:
                                tree_root.remove_subtree(p)
                    selected.clear()
                    rebuild_visible()
                    if cursor >= len(visible_nodes):
                        cursor = max(0, len(visible_nodes) - 1)
                    ok = [p for p, e in results if e is None]
                    bad = [(p, e) for p, e in results if e is not None]
                    summary = [f"{len(ok)} deleted, {len(bad)} failed."]
                    for p, e in bad[:3]:
                        summary.append(f"  {p}: {e}")
                    if not bad and ok:
                        summary.append("Recovered: " + analyze.humanize_size_kb(size_kb))
                    show_results_modal(stdscr, "Delete complete", summary)
                    stdscr.touchwin()
                    stdscr.refresh()

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        print(f"interactive mode error: {e}", file=stream or sys.stderr)


class Progress:
    """Indeterminate progress bar shown on stderr while a block runs.

    Renders as "<message> [========>         ]", fills and resets
    continuously while the work runs, and on exit replaces the line
    with "<message> [========================] done". The bar only
    animates when stderr is a TTY; otherwise the context manager is a
    silent no-op (so redirects to a log file aren't polluted).

    Usage:
        with report.Progress("Building app registry..."):
            do_slow_work()
    """

    WIDTH = 24
    INTERVAL = 0.08

    def __init__(self, message, stream=None):
        self.message = message
        self.stream = stream if stream is not None else sys.stderr
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        i = 0
        cycle = self.WIDTH + 1
        while not self._stop.is_set():
            pos = i % cycle
            bar = "=" * pos + " " * (self.WIDTH - pos)
            self.stream.write(f"\r{self.message} [{bar}]")
            self.stream.flush()
            time.sleep(self.INTERVAL)
            i += 1
        bar = "=" * self.WIDTH
        self.stream.write(f"\r{self.message} [{bar}] done\n")
        self.stream.flush()

    def __enter__(self):
        if self.stream.isatty():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *args):
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._thread = None
