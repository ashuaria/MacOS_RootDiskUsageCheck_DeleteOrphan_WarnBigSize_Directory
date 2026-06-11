"""Two-pass `du` scanner.

Pass 1: `du -d 2 -k -x <root>` to surface the biggest top-level items.
Pass 2: `du -d 4 -k -x` on the top-K items from pass 1, to drill into the
        biggest offenders so the user can see *what* is inside.

`-k` is unambiguous (KB blocks). `-x` keeps the scan on a single filesystem —
critical because ~/Library is an APFS firmlink and the user's external drive
is on a separate volume. We capture stderr (don't discard it) so permission
warnings are surfaced, not hidden.
"""

import os
import subprocess
from collections import namedtuple

ScanItem = namedtuple(
    "ScanItem",
    ["path", "size_kb", "depth", "inaccessible", "depth_from_root"],
)


def _depth_from_root(path, root):
    try:
        rel = os.path.relpath(path, root)
        if rel == "." or rel == os.curdir:
            return 0
        return rel.count(os.sep) + 1
    except ValueError:
        return 0


class TreeNode:
    """A node in the directory-size tree.

    `own_kb` is the size reported by `du` for *this* path. `size_kb` is the
    accumulated total (self + all descendants). `finding` is set after
    classification.  `children` are sorted by total size descending so the
    biggest offenders surface first when expanded. `expanded` defaults to
    True so the CLI table renders the full tree by default; the TUI
    collapses on demand.
    """

    __slots__ = (
        "path", "own_kb", "size_kb", "depth", "finding",
        "children", "expanded",
    )

    def __init__(self, path, own_kb=0, depth=0, finding=None):
        self.path = path
        self.own_kb = own_kb
        self.size_kb = own_kb
        self.depth = depth
        self.finding = finding
        self.children = []
        self.expanded = True

    def visible_rows(self, parent_expanded=True):
        """Yield (node, indent_level) in on-screen order.

        `parent_expanded` is False if any ancestor is collapsed — in that
        case only the row for *this* node is yielded and children are
        skipped regardless of their own `expanded` flag.
        """
        visible = parent_expanded
        yield (self, 0 if not visible else self.depth)
        if self.children and self.expanded and visible:
            for child in self.children:
                yield from child.visible_rows(True)

    def all_nodes(self):
        yield self
        for c in self.children:
            yield from c.all_nodes()

    def prune_unclassified(self):
        """Drop subtrees whose root has no `finding`, re-parenting their
        children to the grandparent. After classification, nodes below the
        size threshold have `finding = None` and would otherwise render as
        generic "directory" rows in the TUI/CLI table — this hides them
        while preserving the hierarchy of classified (above-threshold) items.
        """
        kept = []
        for child in self.children:
            child.prune_unclassified()
            if child.finding is not None:
                kept.append(child)
            else:
                kept.extend(child.children)
        self.children = kept

    def remove_subtree(self, path):
        """Remove the node at `path` from the tree.

        Returns the (removed_node, removed_size_kb) tuple, or (None, 0) if
        not found. Every ancestor's `size_kb` is decremented by the
        removed node's accumulated size so the rest of the tree stays
        consistent with what `du` would have reported.
        """
        for i, child in enumerate(self.children):
            if child.path == path:
                removed = self.children.pop(i)
                self._shrink_by(removed.size_kb)
                return removed, removed.size_kb
            r, sz = child.remove_subtree(path)
            if r is not None:
                self._shrink_by(sz)
                return r, sz
        return None, 0

    def _shrink_by(self, kb):
        self.size_kb = max(0, self.size_kb - kb)


ScanResult = namedtuple("ScanResult", ["items", "inaccessible", "root", "tree_root"])


def _run_du(path, depth):
    """Run `du -d N -k -x <path>`. Returns (items, inaccessible)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/du", "-d", str(depth), "-k", "-x", path],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("/usr/bin/du not found — this tool is macOS-only")

    items = []
    inaccessible = []
    for line in proc.stdout.splitlines():
        tab = line.find("\t")
        if tab < 0:
            continue
        try:
            kb = int(line[:tab])
        except ValueError:
            continue
        p = line[tab + 1 :]
        items.append(ScanItem(path=p, size_kb=kb, depth=depth, inaccessible=False,
                              depth_from_root=0))

    if proc.returncode != 0 and proc.stderr:
        for line in proc.stderr.splitlines():
            line = line.strip()
            if line and path in line:
                inaccessible.append(line)

    return items, inaccessible


def parse_du_output(text):
    """Parse du-style output text. Exposed for testing."""
    items = []
    for line in text.splitlines():
        tab = line.find("\t")
        if tab < 0:
            continue
        try:
            kb = int(line[:tab])
        except ValueError:
            continue
        items.append((line[tab + 1 :], kb))
    return items


def _build_tree(items, root):
    """Construct a TreeNode hierarchy from flat scan items.

    Each item's `size_kb` becomes the node's `own_kb`.  Sizes are
    accumulated bottom-up so every node's `size_kb` reflects the total
    of itself plus all descendants.
    """
    root = os.path.realpath(root)
    by_path = {}
    by_depth = {}
    for it in items:
        p = os.path.realpath(it.path)
        by_path[p] = it.size_kb
        by_depth[p] = getattr(it, "depth_from_root", 0) or (
            p.count(os.sep) - root.count(os.sep)
        )

    nodes = {}
    for p, kb in by_path.items():
        nodes[p] = TreeNode(path=p, own_kb=kb, depth=by_depth[p])

    # Wire parent -> children (shallowest paths become parents).
    for p in sorted(nodes, key=lambda p: p.count(os.sep)):
        node = nodes[p]
        parent_p = os.path.dirname(p)
        while parent_p and parent_p != root:
            if parent_p in nodes:
                nodes[parent_p].children.append(node)
                break
            parent_p = os.path.dirname(parent_p)

    # Sort children by own_kb descending (largest first).
    for n in nodes.values():
        n.children.sort(key=lambda c: c.own_kb, reverse=True)

    # Accumulate sizes bottom-up: deepest paths first so each parent's
    # sum reads the already-computed child totals.
    for n in sorted(nodes.values(), key=lambda n: -n.depth):
        if not n.children:
            n.size_kb = n.own_kb
        else:
            n.size_kb = n.own_kb + sum(c.size_kb for c in n.children)

    top_levels = sorted(
        [n for p, n in nodes.items()
         if os.path.dirname(p) not in nodes],
        key=lambda n: n.size_kb,
        reverse=True,
    )

    root_node = TreeNode(path=root, own_kb=0, depth=0)
    root_node.children = top_levels
    root_node.size_kb = root_node.own_kb + sum(c.size_kb for c in root_node.children)

    return root_node


def scan(root, top=10, max_depth_pass2=4, pass1_depth=2):
    """Run the two-pass scan.

    Returns a ScanResult containing all items (deduped, pass-2 children merged
    into their pass-1 parents), a list of inaccessible paths, the resolved
    root, and a TreeNode root representing the full directory tree.
    """
    root = os.path.realpath(root)
    pass1_items, inaccessible = _run_du(root, pass1_depth)

    # Drop the root itself from pass-1 (it duplicates the grand total).
    pass1_items = [it for it in pass1_items if it.path != root]

    # Attach depth_from_root to pass-1 items.
    for it in pass1_items:
        it = it._replace(depth_from_root=_depth_from_root(it.path, root))

    # Pick top-K by size, excluding the scan root.
    pass1_items.sort(key=lambda it: it.size_kb, reverse=True)
    drill_targets = [it for it in pass1_items[:top] if os.path.isdir(it.path)]

    # Pass 2: drill into the top-K.
    drilled = []
    seen_in_drill = set()
    drilled_targets = set()
    for target in drill_targets:
        items, errs = _run_du(target.path, max_depth_pass2)
        inaccessible.extend(errs)
        for it in items:
            if it.path == target.path or it.path == root:
                continue
            if it.path in seen_in_drill:
                continue
            seen_in_drill.add(it.path)
            it = it._replace(depth_from_root=_depth_from_root(it.path, root))
            drilled.append(it)
        drilled_targets.add(target.path)

    # Dedupe: drop pass-1 entries that are drill targets, and drop pass-1
    # entries that the drill output already covers (same path = same total
    # size from `du`).
    pass1_items = [
        it for it in pass1_items
        if it.path not in drilled_targets and it.path not in seen_in_drill
    ]

    all_items = pass1_items + drilled
    all_items.sort(key=lambda it: it.path)

    tree_root = _build_tree(all_items, root)

    return ScanResult(
        items=all_items, inaccessible=inaccessible, root=root,
        tree_root=tree_root,
    )
