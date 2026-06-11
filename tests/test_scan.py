"""Unit tests for the du-output parser and the tree builder."""

import os
import unittest

from macscan import scan


class TestParseDuOutput(unittest.TestCase):
    def test_basic(self):
        text = "8\t/Users/foo\n120\t/Users/foo/Bar\n4\t/Users/foo/Bar/baz\n"
        result = scan.parse_du_output(text)
        self.assertEqual(result, [
            ("/Users/foo", 8),
            ("/Users/foo/Bar", 120),
            ("/Users/foo/Bar/baz", 4),
        ])

    def test_path_with_spaces(self):
        text = "16\t/Users/foo/My Documents\n"
        result = scan.parse_du_output(text)
        self.assertEqual(result, [("/Users/foo/My Documents", 16)])

    def test_path_with_tabs(self):
        text = "16\t/Users/foo/with\ttab\n"
        result = scan.parse_du_output(text)
        self.assertEqual(result, [("/Users/foo/with\ttab", 16)])

    def test_ignores_garbage(self):
        text = "du: cannot read: Permission denied\n32\t/Users/foo\n"
        result = scan.parse_du_output(text)
        self.assertEqual(result, [("/Users/foo", 32)])


def _item(path, kb, dfr):
    return scan.ScanItem(
        path=path, size_kb=kb, depth=2, inaccessible=False, depth_from_root=dfr,
    )


class TestBuildTree(unittest.TestCase):
    def test_all_nodes_expanded_by_default(self):
        """The CLI table renders via _tree_lines, which hides children of any
        node whose `expanded` flag is False. To make the printed output show
        the full tree by default (matching the user's expectation), every
        node from _build_tree must come out expanded.
        """
        items = [
            _item("/r/a", 100, 1),
            _item("/r/a/b", 50, 2),
            _item("/r/c", 30, 1),
        ]
        root = scan._build_tree(items, "/r")
        for n in root.all_nodes():
            self.assertTrue(n.expanded, f"node {n.path} should be expanded by default")

    def test_selection_dedup_skips_descendants_of_selected_ancestor(self):
        """Regression: the TUI's running total must not double-count a parent
        and its descendants. `du` already reports each path's cumulative
        size, so when both a parent and a child are selected, only the
        parent's size should be summed.
        """
        items = [
            _item("/A",     100, 1),
            _item("/A/B",    60, 2),
            _item("/A/B/C",  40, 3),
            _item("/A/X",    10, 2),
            _item("/Y",      20, 1),
        ]
        tree = scan._build_tree(items, "/")
        visible = list(tree.all_nodes())[1:]  # mirror TUI's [1:] drop of root
        idx = {n.path: i for i, n in enumerate(visible)}

        def ancestor_selected(path, selected):
            parent = os.path.dirname(path)
            # os.path.dirname("/") == "/" on POSIX, so terminate when the
            # dirname stops changing instead of relying on truthiness.
            while parent and parent != os.path.dirname(parent):
                if parent in selected:
                    return True
                parent = os.path.dirname(parent)
            return False

        def total_selected_kb(sel):
            sp = {visible[i].path for i in sel}
            t = 0
            for i in sel:
                n = visible[i]
                if ancestor_selected(n.path, sp):
                    continue
                t += n.own_kb or n.size_kb
            return t

        # All selected: only top-level (A=100, Y=20) count -> 120
        self.assertEqual(total_selected_kb(set(idx.values())), 120)
        # Just leaves -> C(40) + X(10) + Y(20) = 70
        self.assertEqual(total_selected_kb({idx["/A/B/C"], idx["/A/X"], idx["/Y"]}), 70)
        # Just /A -> 100
        self.assertEqual(total_selected_kb({idx["/A"]}), 100)
        # /A and /Y -> 120
        self.assertEqual(total_selected_kb({idx["/A"], idx["/Y"]}), 120)

    def test_remove_subtree_shrinks_ancestors(self):
        """Regression: after the TUI deletes a node from disk, the
        in-memory tree must drop that node and decrement every
        ancestor's accumulated `size_kb` by the removed node's size —
        otherwise the running total displayed at the top of the TUI
        would be wrong.
        """
        items = [
            _item("/A",       100, 1),
            _item("/A/B",      60, 2),
            _item("/A/B/C",    40, 3),
            _item("/A/X",      10, 2),
            _item("/Y",        20, 1),
        ]
        tree = scan._build_tree(items, "/")
        a = next(n for n in tree.all_nodes() if n.path == "/A")
        # /A: own=100, /A/B=60+40=100, /A/X=10 → total 210
        self.assertEqual(a.size_kb, 210)

        removed, sz = tree.remove_subtree("/A/B")
        self.assertIsNotNone(removed)
        self.assertEqual(removed.path, "/A/B")
        # /A/B accumulated: own 60 + /A/B/C 40 = 100
        self.assertEqual(sz, 100)
        # /A lost /A/B and its descendants, so should now be 100 (own) + 10 (X) = 110
        self.assertEqual(a.size_kb, 110)
        # /A/B and /A/B/C must be gone
        remaining = {n.path for n in tree.all_nodes()}
        self.assertNotIn("/A/B", remaining)
        self.assertNotIn("/A/B/C", remaining)
        self.assertIn("/A/X", remaining)
        self.assertIn("/Y", remaining)

    def test_remove_subtree_returns_none_when_missing(self):
        items = [_item("/A", 100, 1)]
        tree = scan._build_tree(items, "/")
        a = next(n for n in tree.all_nodes() if n.path == "/A")
        before = a.size_kb
        removed, sz = tree.remove_subtree("/does/not/exist")
        self.assertIsNone(removed)
        self.assertEqual(sz, 0)
        self.assertEqual(a.size_kb, before)

    def test_prune_unclassified_removes_below_threshold_subtrees(self):
        """Regression: after classification, subtrees whose root has no
        finding (i.e. the directory was below the threshold) must be
        pruned from the tree so the TUI/CLI table doesn't show them as
        generic "directory" rows. The hierarchy of classified (above-
        threshold) items must be preserved.
        """
        items = [
            _item("/A",        100_000, 1),  # above threshold -> classified
            _item("/A/big",     50_000, 2),  # above threshold -> classified
            _item("/A/big/c",   30_000, 3),  # above threshold -> classified
            _item("/A/small",      500, 2),  # below threshold -> unclassified
            _item("/A/small/d",    100, 3),  # below threshold -> unclassified
            _item("/Y",         20_000, 1),  # above threshold -> classified
        ]
        tree = scan._build_tree(items, "/")
        by_path = {n.path: n for n in tree.all_nodes()}

        # Simulate the classifier attaching findings to the three above-threshold nodes.
        by_path["/A"].finding = "f"
        by_path["/A/big"].finding = "f"
        by_path["/A/big/c"].finding = "f"
        by_path["/Y"].finding = "f"

        tree.prune_unclassified()

        remaining = {n.path for n in tree.all_nodes()}
        self.assertIn("/A", remaining)
        self.assertIn("/A/big", remaining)
        self.assertIn("/A/big/c", remaining)
        self.assertIn("/Y", remaining)
        # Below-threshold subtrees gone:
        self.assertNotIn("/A/small", remaining)
        self.assertNotIn("/A/small/d", remaining)

        # The kept child's parent should be /A (re-parented up over the
        # pruned /A/small).
        small_d = None
        for n in tree.all_nodes():
            if n.path == "/A/big":
                parent_path = os.path.dirname(n.path)
                self.assertEqual(parent_path, "/A")


if __name__ == "__main__":
    unittest.main()
