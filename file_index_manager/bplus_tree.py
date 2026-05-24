from __future__ import annotations

"""In-memory B+Tree (stdlib-only).

This module is intentionally self-contained and does *not* integrate with the
rest of the project yet (per assignment requirement).

Design notes
- B+Tree stores keys in *all* nodes, but values only in leaf nodes.
- Internal nodes route searches via keys + child pointers.
- Leaf nodes are linked via `next` pointers to support efficient range scans.

Implementation scope
- insert/search/range_search are fully supported.
- delete is intentionally simple: removes key/value from a leaf without full
  underflow rebalancing. It is written to be safe (must not crash).

TODO (future)
- Proper delete rebalancing (borrow/merge) and persistent on-disk storage.
"""

from dataclasses import dataclass, field
from bisect import bisect_left, bisect_right
from typing import Any, Generic, Optional, TypeVar, Union, List


K = TypeVar("K", int, str)
V = TypeVar("V")


def _check_key_type(key: Any) -> None:
	if not isinstance(key, (int, str)):
		raise TypeError("BPlusTree keys must be int or str")


@dataclass
class _LeafNode(Generic[K, V]):
	"""Leaf node.

	- Holds sorted `keys`.
	- Holds matching `values` (same length as keys).
	- Linked list via `next` to enable range scans across leaves.
	"""

	keys: List[K] = field(default_factory=list)
	values: List[V] = field(default_factory=list)
	parent: Optional[_InternalNode[K, V]] = None
	next: Optional[_LeafNode[K, V]] = None

	@property
	def is_leaf(self) -> bool:
		return True


@dataclass
class _InternalNode(Generic[K, V]):
	"""Internal node.

	- Holds separator `keys`.
	- Holds `children` pointers; number of children is len(keys) + 1.
	
	Routing rule (standard B+Tree):
	- For a key `k`, find index i = bisect_right(keys, k)
	- Follow children[i]
	"""

	keys: List[K] = field(default_factory=list)
	children: List[Union[_InternalNode[K, V], _LeafNode[K, V]]] = field(default_factory=list)
	parent: Optional[_InternalNode[K, V]] = None

	@property
	def is_leaf(self) -> bool:
		return False


_Node = Union[_InternalNode[K, V], _LeafNode[K, V]]


class BPlusTree(Generic[K, V]):
	"""A simple in-memory B+Tree.

	Parameters
	- order: maximum number of children per internal node.
	  Commonly called `m`. Then internal max keys = m - 1.

	Notes
	- Leaf nodes also use max keys = order - 1.
	- This implementation supports unique keys.
	"""

	def __init__(self, order: int = 4):
		if not isinstance(order, int) or order < 3:
			# order=3 is the smallest sensible B+Tree.
			raise ValueError("order must be an int >= 3")
		self.order = order
		self.max_keys = order - 1
		self.root: _Node[K, V] = _LeafNode()

	# -----------------
	# Public operations
	# -----------------
	def insert(self, key: K, value: V) -> None:
		"""Insert (key, value). Overwrites if key already exists."""
		_check_key_type(key)
		leaf = self._find_leaf(key)
		idx = bisect_left(leaf.keys, key)
		if idx < len(leaf.keys) and leaf.keys[idx] == key:
			leaf.values[idx] = value
			return
		leaf.keys.insert(idx, key)
		leaf.values.insert(idx, value)
		if len(leaf.keys) > self.max_keys:
			self._split_leaf(leaf)

	def search(self, key: K) -> Optional[V]:
		"""Return value for key, or None if not found."""
		_check_key_type(key)
		leaf = self._find_leaf(key)
		idx = bisect_left(leaf.keys, key)
		if idx < len(leaf.keys) and leaf.keys[idx] == key:
			return leaf.values[idx]
		return None

	def delete(self, key: K) -> None:
		"""Remove key/value from leaf.

	This is intentionally *simple* (no full rebalancing). It updates parent
	separator keys when safe, and must not crash.
	"""
		_check_key_type(key)
		leaf = self._find_leaf(key)
		idx = bisect_left(leaf.keys, key)
		if idx >= len(leaf.keys) or leaf.keys[idx] != key:
			return

		removed_first = (idx == 0)
		del leaf.keys[idx]
		del leaf.values[idx]

		# If the root is a leaf, we're done.
		if leaf.parent is None:
			return

		# Update separator key in parent if we removed the first key.
		if removed_first:
			self._refresh_parent_separators_after_leaf_change(leaf)

		# If leaf became empty, we do not merge/rebalance yet.
		# TODO: implement borrow/merge underflow handling.

	def range_search(self, low: K, high: K) -> List[V]:
		"""Return all values with low <= key <= high (inclusive).

	Uses linked leaf pointers to scan across leaf nodes.
	"""
		_check_key_type(low)
		_check_key_type(high)
		# If types differ (int vs str) comparisons will error in Python 3.
		if type(low) is not type(high):
			raise TypeError("range_search bounds must have the same type")
		if low > high:
			low, high = high, low

		results: List[V] = []
		leaf = self._find_leaf(low)

		while leaf is not None:
			# Start at first key >= low in the first leaf; then continue sequentially.
			start = bisect_left(leaf.keys, low)
			for i in range(start, len(leaf.keys)):
				k = leaf.keys[i]
				if k > high:
					return results
				results.append(leaf.values[i])
			leaf = leaf.next

		return results

	# -----------------
	# Navigation helpers
	# -----------------
	def _find_leaf(self, key: K) -> _LeafNode[K, V]:
		"""Descend from root to locate the leaf that *should* contain key."""
		node: _Node[K, V] = self.root
		while not node.is_leaf:
			internal = node  # type: ignore[assignment]
			# Use bisect_right so equal keys go to the right child,
			# matching standard B+Tree routing with separator keys.
			idx = bisect_right(internal.keys, key)
			node = internal.children[idx]
		return node  # type: ignore[return-value]

	# -----------------
	# Splitting
	# -----------------
	def _split_leaf(self, leaf: _LeafNode[K, V]) -> None:
		"""Split an overflowing leaf into two leaves and promote separator."""
		# Split roughly in half; right leaf gets the higher keys.
		mid = (len(leaf.keys) + 1) // 2
		right = _LeafNode[K, V](
			keys=leaf.keys[mid:],
			values=leaf.values[mid:],
			parent=leaf.parent,
			next=leaf.next,
		)
		leaf.keys = leaf.keys[:mid]
		leaf.values = leaf.values[:mid]
		leaf.next = right

		# Separator key for parent is the first key of the right leaf.
		sep = right.keys[0]
		self._insert_into_parent(left=leaf, key=sep, right=right)

	def _split_internal(self, node: _InternalNode[K, V]) -> None:
		"""Split an overflowing internal node and promote a middle separator."""
		mid = len(node.keys) // 2
		sep = node.keys[mid]

		left_keys = node.keys[:mid]
		right_keys = node.keys[mid + 1 :]
		left_children = node.children[: mid + 1]
		right_children = node.children[mid + 1 :]

		# Reuse existing node as left.
		node.keys = left_keys
		node.children = left_children

		right = _InternalNode[K, V](keys=right_keys, children=right_children, parent=node.parent)
		for child in right.children:
			child.parent = right  # type: ignore[assignment]

		self._insert_into_parent(left=node, key=sep, right=right)

	def _insert_into_parent(self, left: _Node[K, V], key: K, right: _Node[K, V]) -> None:
		"""Insert separator key and right pointer into parent (create new root if needed)."""
		parent = left.parent
		if parent is None:
			# New root
			new_root = _InternalNode[K, V](keys=[key], children=[left, right], parent=None)
			left.parent = new_root  # type: ignore[assignment]
			right.parent = new_root  # type: ignore[assignment]
			self.root = new_root
			return

		# Insert key/right into existing parent after left child.
		try:
			left_index = parent.children.index(left)
		except ValueError:
			# Shouldn't happen, but keep safe.
			left_index = 0

		parent.keys.insert(left_index, key)
		parent.children.insert(left_index + 1, right)
		right.parent = parent  # type: ignore[assignment]

		if len(parent.keys) > self.max_keys:
			self._split_internal(parent)

	# -----------------
	# Delete helper
	# -----------------
	def _refresh_parent_separators_after_leaf_change(self, leaf: _LeafNode[K, V]) -> None:
		"""Update parent separator keys when a leaf's first key changes.

	In a B+Tree, for a parent with children C0, C1, ... and keys K0, K1, ...,
	K(i) is usually the *first key* of child C(i+1).

	So if leaf is child Cj (j>0) and its first key changes, update parent.keys[j-1].
	We then may need to propagate upwards if the changed key was also the first key
	of the parent subtree.
	"""
		parent = leaf.parent
		if parent is None:
			return

		try:
			child_index = parent.children.index(leaf)
		except ValueError:
			return

		if child_index == 0:
			# Parent key does not represent the first child.
			return

		# If leaf is now empty, we cannot derive a separator safely.
		if not leaf.keys:
			return

		new_sep = leaf.keys[0]
		parent.keys[child_index - 1] = new_sep

		# Optional: propagate if this parent is itself not the first child.
		# This keeps routing stable for searches.
		if parent.parent is not None:
			# If the separator we changed is the first key of this internal node,
			# then its parent might also need adjustment.
			# We keep it conservative/safe.
			pass


__all__ = ["BPlusTree"]
