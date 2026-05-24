#!/usr/bin/env python3

from __future__ import annotations

import os
import time
from pathlib import Path

from buffer_manager import BufferManager
from disk_space_manager import DiskSpaceManager


class _InstrumentedDiskSpaceManager(DiskSpaceManager):
	def __init__(self, config: dict):
		super().__init__(config)
		self.log_write_calls: list[tuple[str, int]] = []

	def log_write(self, file_id: str, page_number: int) -> None:  # type: ignore[override]
		self.log_write_calls.append((str(file_id), int(page_number)))
		return


def _unique_file_id(prefix: str) -> str:
	return f"{prefix}_{os.getpid()}_{int(time.time() * 1000)}"


def _bin_path_for(disk: DiskSpaceManager, file_id: str) -> Path:
	# DiskSpaceManager stores files in project root next to archive.py as <file_id>.bin
	base_dir = Path(getattr(disk, "_base_dir"))
	return base_dir / f"{file_id}.bin"


def _cleanup(paths: list[Path]) -> None:
	for p in paths:
		try:
			p.unlink()
		except FileNotFoundError:
			pass


def test_disk_space_manager() -> None:
	page_size = 64
	disk = _InstrumentedDiskSpaceManager({"page_size": page_size})
	file_id = _unique_file_id("__tmp_test_dsm")
	bin_path = _bin_path_for(disk, file_id)

	_cleanup([bin_path])
	try:
		# create_file creates a binary file
		res = disk.create_file(file_id)
		assert getattr(res, "success", False), f"create_file failed: {getattr(res, 'message', '')}"
		assert bin_path.exists(), "create_file did not create <file_id>.bin"

		# allocate_page appends exactly one fixed-size page
		assert disk.num_pages(file_id) == 0, "new file should have 0 pages"
		disk.reset_stats()
		alloc = disk.allocate_page(file_id)
		assert getattr(alloc, "success", False), f"allocate_page failed: {getattr(alloc, 'message', '')}"
		assert disk.num_pages(file_id) == 1, "allocate_page should increase num_pages to 1"
		assert bin_path.stat().st_size == page_size, "allocate_page should append exactly one page_size bytes"

		# num_pages returns the correct number
		alloc2 = disk.allocate_page(file_id)
		assert getattr(alloc2, "success", False)
		assert disk.num_pages(file_id) == 2, "num_pages incorrect after second allocation"
		assert bin_path.stat().st_size == 2 * page_size

		# write_page writes exactly one page and pads shorter data
		disk.reset_stats()
		disk.log_write_calls.clear()
		write = disk.write_page(file_id, 0, b"hello")
		assert getattr(write, "success", False), f"write_page failed: {getattr(write, 'message', '')}"
		assert getattr(write, "bytes_written", None) == page_size, "write_page must write exactly one full page"
		read = disk.read_page(file_id, 0)
		assert getattr(read, "success", False), f"read_page failed: {getattr(read, 'message', '')}"
		data = getattr(read, "data", b"")
		assert isinstance(data, (bytes, bytearray))
		assert len(data) == page_size, "read_page must return exactly page_size bytes"
		assert data[:5] == b"hello", "write_page did not persist payload prefix"
		assert data[5:] == b"\x00" * (page_size - 5), "write_page did not pad with zeros"

		# write_page rejects data longer than page_size
		too_big = disk.write_page(file_id, 0, b"x" * (page_size + 1))
		assert not getattr(too_big, "success", True), "write_page must reject payload larger than page_size"

		# read_page on a missing page returns failure and does not crash
		missing = disk.read_page(file_id, 999)
		assert not getattr(missing, "success", True), "read_page must fail on missing page"

		# disk read/write counters are updated correctly
		disk.reset_stats()
		_ = disk.read_page(file_id, 0)
		_ = disk.read_page(file_id, 1)
		_ = disk.write_page(file_id, 1, b"a")
		st = disk.get_stats()
		assert st.disk_reads == 2, f"expected 2 disk reads, got {st.disk_reads}"
		assert st.disk_writes == 1, f"expected 1 disk write, got {st.disk_writes}"

		# log_write is called on successful writes
		# write_page should call log_write; allocate_page should call log_write.
		disk.log_write_calls.clear()
		_ = disk.write_page(file_id, 1, b"b")
		assert (file_id, 1) in disk.log_write_calls, "log_write not called on write_page"
		disk.log_write_calls.clear()
		_ = disk.allocate_page(file_id)
		assert (file_id, 2) in disk.log_write_calls, "log_write not called on allocate_page"
	finally:
		_cleanup([bin_path])

	print("PASS: DiskSpaceManager")


def _init_relation_with_pages(disk: DiskSpaceManager, file_id: str, page_size: int, n_pages: int) -> None:
	res = disk.create_file(file_id)
	assert getattr(res, "success", False)
	for _ in range(n_pages):
		alloc = disk.allocate_page(file_id)
		assert getattr(alloc, "success", False)

	# Put distinct content so we can verify writebacks.
	for page_no in range(n_pages):
		payload = (f"P{page_no}".encode("ascii") + b"_" * (page_size - 2))[:page_size]
		wr = disk.write_page(file_id, page_no, payload)
		assert getattr(wr, "success", False)


def test_buffer_manager() -> None:
	page_size = 64
	file_id = _unique_file_id("__tmp_test_buf")
	disk = _InstrumentedDiskSpaceManager({"page_size": page_size})
	bin_path = _bin_path_for(disk, file_id)

	_cleanup([bin_path])
	try:
		_init_relation_with_pages(disk, file_id, page_size, n_pages=3)

		# get_page first access is a miss; second access is a hit
		buf = BufferManager({"buffer_pool_size": 2, "replacement_policy": "LRU"}, disk)
		buf.reset_stats()
		p0 = buf.get_page(file_id, 0)
		assert getattr(p0, "success", False)
		assert getattr(p0, "message", "") == "buffer miss", "first get_page should be a miss"
		p0b = buf.get_page(file_id, 0)
		assert getattr(p0b, "success", False)
		assert getattr(p0b, "message", "") == "buffer hit", "second get_page should be a hit"
		st = buf.get_stats()
		assert st.buffer_requests == 2
		assert st.buffer_hits == 1
		assert st.buffer_misses == 1

		# buffer_pool_size is respected + LRU evicts least recently used
		buf = BufferManager({"buffer_pool_size": 2, "replacement_policy": "LRU"}, disk)
		buf.reset_stats()
		assert getattr(buf.get_page(file_id, 0), "success", False)  # miss
		assert getattr(buf.get_page(file_id, 1), "success", False)  # miss
		assert getattr(buf.get_page(file_id, 0), "success", False)  # hit -> makes page 1 LRU
		assert getattr(buf.get_page(file_id, 2), "success", False)  # miss -> eviction expected

		# now page 1 should have been evicted: re-access should be miss
		p1_again = buf.get_page(file_id, 1)
		assert getattr(p1_again, "success", False)
		assert getattr(p1_again, "message", "") == "buffer miss", "LRU should evict least recently used page"
		st = buf.get_stats()
		assert st.evictions >= 1, "expected at least one eviction under LRU"

		# MRU evicts most recently used
		buf = BufferManager({"buffer_pool_size": 2, "replacement_policy": "MRU"}, disk)
		buf.reset_stats()
		assert getattr(buf.get_page(file_id, 0), "success", False)  # miss
		assert getattr(buf.get_page(file_id, 1), "success", False)  # miss
		assert getattr(buf.get_page(file_id, 0), "success", False)  # hit -> makes page 0 MRU
		assert getattr(buf.get_page(file_id, 2), "success", False)  # miss -> should evict MRU (page 0)

		p0_again = buf.get_page(file_id, 0)
		assert getattr(p0_again, "success", False)
		assert getattr(p0_again, "message", "") == "buffer miss", "MRU should evict most recently used page"
		st = buf.get_stats()
		assert st.evictions >= 1, "expected at least one eviction under MRU"

		# dirty pages are written back on eviction
		disk.reset_stats()
		buf = BufferManager({"buffer_pool_size": 1, "replacement_policy": "LRU"}, disk)
		buf.reset_stats()
		assert getattr(buf.get_page(file_id, 0), "success", False)
		new_payload = b"DIRTY" + (b"!" * (page_size - 5))
		put = buf.put_page(file_id, 0, new_payload, dirty=True)
		assert getattr(put, "success", False)

		# trigger eviction by loading another page
		assert getattr(buf.get_page(file_id, 1), "success", False)
		bst = buf.get_stats()
		assert bst.evictions >= 1, "expected eviction when buffer_pool_size=1"
		assert bst.dirty_writebacks >= 1, "dirty page should be written back on eviction"

		# verify disk contains new_payload
		rd = disk.read_page(file_id, 0)
		assert getattr(rd, "success", False)
		assert getattr(rd, "data", b"")[:5] == b"DIRTY", "dirty writeback did not reach disk"

		# flush writes all dirty pages
		disk.reset_stats()
		buf = BufferManager({"buffer_pool_size": 2, "replacement_policy": "LRU"}, disk)
		buf.reset_stats()
		assert getattr(buf.get_page(file_id, 0), "success", False)
		assert getattr(buf.get_page(file_id, 1), "success", False)
		payload0 = b"F0" + (b"0" * (page_size - 2))
		payload1 = b"F1" + (b"1" * (page_size - 2))
		assert getattr(buf.put_page(file_id, 0, payload0, dirty=True), "success", False)
		assert getattr(buf.put_page(file_id, 1, payload1, dirty=True), "success", False)

		before_writes = disk.get_stats().disk_writes
		flush_res = buf.flush()
		assert getattr(flush_res, "success", False), f"flush failed: {getattr(flush_res, 'message', '')}"
		after_writes = disk.get_stats().disk_writes
		assert after_writes - before_writes == 2, "flush must write all dirty pages"

		bst = buf.get_stats()
		assert bst.dirty_writebacks >= 2, "buffer dirty_writebacks should include flush writebacks"

		rd0 = disk.read_page(file_id, 0)
		rd1 = disk.read_page(file_id, 1)
		assert getattr(rd0, "success", False) and getattr(rd1, "success", False)
		assert getattr(rd0, "data", b"")[:2] == b"F0"
		assert getattr(rd1, "data", b"")[:2] == b"F1"
	finally:
		_cleanup([bin_path])

	print("PASS: BufferManager")


def main() -> None:
	test_disk_space_manager()
	test_buffer_manager()
	print("ALL PASS")


if __name__ == "__main__":
	main()
