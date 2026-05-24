from __future__ import annotations

"""BufferManager (stdlib-only).

The BufferManager caches fixed-size pages in memory and performs physical I/O
*only* through DiskSpaceManager.

Replacement policies
- LRU: evict least recently used page (oldest access).
- MRU: evict most recently used page (newest access).

Dirty pages
- `put_page(..., dirty=True)` marks a cached page as dirty.
- Dirty pages are written back on `flush_page`, `flush`, or eviction.
"""

from dataclasses import dataclass
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from common.result import OperationResult, PageResult, StatsResult, WriteResult


Config = Dict[str, Any]
Key = Tuple[str, int]  # (file_id, page_number)


@dataclass
class _Frame:
	data: bytes
	dirty: bool


class BufferManager:
	"""In-memory buffer pool sitting between FileIndexManager and DiskSpaceManager."""

	def __init__(self, config: Config, disk: Any):
		self.config = config
		self.disk = disk

		self.pool_size: int = int(config.get("buffer_pool_size", 16) or 16)
		if self.pool_size <= 0:
			self.pool_size = 16

		policy = str(config.get("replacement_policy", "LRU") or "LRU").upper()
		self.replacement_policy: str = policy if policy in {"LRU", "MRU"} else "LRU"

		# OrderedDict maintains access order when we move keys on each access.
		# Oldest at the beginning, newest at the end.
		self._frames: "OrderedDict[Key, _Frame]" = OrderedDict()

		# Stats
		self._requests: int = 0
		self._hits: int = 0
		self._misses: int = 0
		self._evictions: int = 0
		self._dirty_writebacks: int = 0

	# -----------------
	# Public API
	# -----------------
	def get_page(self, file_id: str, page_number: int) -> PageResult | OperationResult:
		"""Return a page, served from buffer if present; otherwise loads from disk."""
		self._requests += 1
		key = (str(file_id), int(page_number))

		frame = self._frames.get(key)
		if frame is not None:
			self._hits += 1
			self._touch(key)
			return PageResult(True, message="buffer hit", page_id=key, data=frame.data)

		self._misses += 1
		ensure = self._ensure_capacity_for_new_key(key)
		if not ensure.success:
			return ensure

		res = self.disk.read_page(key[0], key[1])
		if not getattr(res, "success", False):
			return OperationResult(False, message=getattr(res, "message", "read failed"))

		data = getattr(res, "data", b"")
		if not isinstance(data, (bytes, bytearray)):
			return OperationResult(False, message="read_page returned non-bytes data")

		# Cache as clean frame.
		self._frames[key] = _Frame(data=bytes(data), dirty=False)
		self._touch(key)
		return PageResult(True, message="buffer miss", page_id=key, data=bytes(data))

	def put_page(self, file_id: str, page_number: int, data: bytes, dirty: bool = True) -> OperationResult:
		"""Insert/update a page in buffer pool and mark dirty if requested."""
		key = (str(file_id), int(page_number))
		payload = bytes(data)

		if key not in self._frames:
			ensure = self._ensure_capacity_for_new_key(key)
			if not ensure.success:
				return ensure
			self._frames[key] = _Frame(data=payload, dirty=bool(dirty))
		else:
			frame = self._frames[key]
			frame.data = payload
			frame.dirty = frame.dirty or bool(dirty)

		self._touch(key)
		return OperationResult(True, message="page cached")

	def mark_dirty(self, file_id: str, page_number: int) -> OperationResult:
		"""Mark a cached page as dirty."""
		key = (str(file_id), int(page_number))
		frame = self._frames.get(key)
		if frame is None:
			return OperationResult(False, message="page not cached")
		frame.dirty = True
		self._touch(key)
		return OperationResult(True, message="marked dirty")

	def flush_page(self, file_id: str, page_number: int) -> WriteResult | OperationResult:
		"""Write a single dirty page back to disk."""
		key = (str(file_id), int(page_number))
		frame = self._frames.get(key)
		if frame is None:
			return OperationResult(False, message="page not cached")
		if not frame.dirty:
			return WriteResult(True, message="clean", page_id=key, bytes_written=0)

		res = self.disk.write_page(key[0], key[1], frame.data)
		if not getattr(res, "success", False):
			return OperationResult(False, message=getattr(res, "message", "write failed"))

		frame.dirty = False
		self._dirty_writebacks += 1
		return WriteResult(True, message="flushed", page_id=key, bytes_written=int(getattr(res, "bytes_written", 0) or 0))

	def flush(self) -> OperationResult:
		"""Write all dirty pages back to disk."""
		for key, frame in list(self._frames.items()):
			if not frame.dirty:
				continue
			res = self.disk.write_page(key[0], key[1], frame.data)
			if getattr(res, "success", False):
				frame.dirty = False
				self._dirty_writebacks += 1
			else:
				# Never crash; keep dirty so it can be retried.
				return OperationResult(False, message=getattr(res, "message", "flush failed"))
		return OperationResult(True, message="flushed")

	def get_stats(self) -> StatsResult:
		return StatsResult(
			True,
			buffer_requests=self._requests,
			buffer_hits=self._hits,
			buffer_misses=self._misses,
			evictions=self._evictions,
			dirty_writebacks=self._dirty_writebacks,
		)

	def reset_stats(self) -> None:
		self._requests = 0
		self._hits = 0
		self._misses = 0
		self._evictions = 0
		self._dirty_writebacks = 0

	# -----------------
	# Internals
	# -----------------
	def _touch(self, key: Key) -> None:
		"""Update access order for LRU/MRU.

		We keep frames in access order: oldest -> newest.
		"""
		try:
			self._frames.move_to_end(key, last=True)
		except KeyError:
			return

	def _ensure_capacity_for_new_key(self, new_key: Key) -> OperationResult:
		"""Evict one page if pool is full and we're inserting a new key."""
		if self.pool_size <= 0:
			return OperationResult(False, message="invalid pool size")

		if new_key in self._frames:
			return OperationResult(True, message="ok")

		if len(self._frames) < self.pool_size:
			return OperationResult(True, message="ok")

		return self._evict_one()

	def _evict_one(self) -> OperationResult:
		"""Evict one frame according to LRU/MRU.

		- LRU: evict oldest (beginning)
		- MRU: evict newest (end)
		"""
		if not self._frames:
			return OperationResult(False, message="buffer empty")

		last = True if self.replacement_policy == "MRU" else False
		key, frame = self._frames.popitem(last=last)

		# If dirty, write back before final eviction.
		if frame.dirty:
			res = self.disk.write_page(key[0], key[1], frame.data)
			if getattr(res, "success", False):
				self._dirty_writebacks += 1
			else:
				# Put it back to avoid silent data loss.
				self._frames[key] = frame
				self._touch(key)
				return OperationResult(False, message=getattr(res, "message", "eviction writeback failed"))

		self._evictions += 1
		return OperationResult(True, message="evicted")


__all__ = ["BufferManager"]
