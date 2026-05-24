from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from common.result import OperationResult, PageResult, StatsResult, WriteResult


Config = Dict[str, Any]


@dataclass
class PageId:
	"""Identifiziert eine Page im Storage.

	TODO: In der echten Implementierung: (file_id, page_no) o.ä.
	"""

	file: str
	page_no: int


class DiskSpaceManager:
	"""DiskSpaceManager: lowest layer that performs real file I/O.

	- Uses fixed-size pages.
	- Stores binary relation files in the project root (next to archive.py).
	- Never loads an entire relation file into memory; uses seek/read/write.

	TODO: Free space management, file layout metadata, checksums, WAL integration.
	"""

	def __init__(self, config: Config):
		self.config = config
		self.page_size: int = int(config.get("page_size", 4096) or 4096)
		if self.page_size <= 0:
			self.page_size = 4096

		# Store files in project root
		self._base_dir = Path(__file__).resolve().parents[1]

		# Stats
		self._disk_reads: int = 0
		self._disk_writes: int = 0

	# -----------------
	# File helpers
	# -----------------
	def _file_path(self, file_id: str) -> Path:
		name = str(file_id).strip()
		if not name:
			raise ValueError("file_id must be non-empty")
		# Prevent directory traversal / path separators.
		if "/" in name or "\\" in name or ".." in name or "\x00" in name:
			raise ValueError("invalid file_id")
		return self._base_dir / f"{name}.bin"

	def create_file(self, file_id: str) -> OperationResult:
		"""Creates the binary file if it does not exist."""
		try:
			path = self._file_path(file_id)
			path.parent.mkdir(parents=True, exist_ok=True)
			# Create if missing; do not truncate if it exists.
			with path.open("ab"):
				pass
			return OperationResult(True, message="file created")
		except Exception as exc:
			return OperationResult(False, message=f"create_file failed: {exc}")

	def file_exists(self, file_id: str) -> bool:
		try:
			return self._file_path(file_id).exists()
		except Exception:
			return False

	def num_pages(self, file_id: str) -> int:
		"""Returns number of fixed-size pages in the file."""
		try:
			path = self._file_path(file_id)
			if not path.exists():
				return 0
			size = path.stat().st_size
			if size <= 0:
				return 0
			return int(size // self.page_size)
		except Exception:
			return 0

	# -----------------
	# Page operations
	# -----------------
	def log_write(self, file_id: str, page_number: int) -> None:
		"""Stub: called on every successful write.

		TODO: Integrate with WAL/log manager.
		"""
		return

	def allocate_page(self, file_id: str) -> PageResult | OperationResult:
		"""Appends one zero-filled page and returns the new page id."""
		try:
			create_res = self.create_file(file_id)
			if not create_res.success:
				return create_res
			path = self._file_path(file_id)
			page_no = self.num_pages(file_id)
			zero_page = b"\x00" * self.page_size
			with path.open("ab") as f:
				f.write(zero_page)
			self._disk_writes += 1
			self.log_write(file_id, page_no)
			return PageResult(True, message="page allocated", page_id=PageId(file=str(file_id), page_no=page_no), data=zero_page)
		except Exception as exc:
			return OperationResult(False, message=f"allocate_page failed: {exc}")

	def read_page(self, file_id: str, page_number: int) -> PageResult | OperationResult:
		"""Reads exactly one page.

		Uses seek offset: offset = page_number * page_size.
		"""
		try:
			path = self._file_path(file_id)
			if not path.exists():
				return OperationResult(False, message="missing file")
			pno = int(page_number)
			if pno < 0 or pno >= self.num_pages(file_id):
				return OperationResult(False, message="missing page")

			offset = pno * self.page_size
			with path.open("rb") as f:
				f.seek(offset, os.SEEK_SET)
				data = f.read(self.page_size)
			if len(data) != self.page_size:
				return OperationResult(False, message="short read")
			self._disk_reads += 1
			return PageResult(True, message="page read", page_id=PageId(file=str(file_id), page_no=pno), data=data)
		except Exception as exc:
			return OperationResult(False, message=f"read_page failed: {exc}")

	def write_page(self, file_id: str, page_number: int, data: bytes) -> WriteResult | OperationResult:
		"""Writes exactly one fixed-size page.

		- If data shorter than page_size, pads with null bytes.
		- If data longer than page_size, returns failure.
		"""
		try:
			path = self._file_path(file_id)
			if not path.exists():
				return OperationResult(False, message="missing file")
			pno = int(page_number)
			if pno < 0 or pno >= self.num_pages(file_id):
				return OperationResult(False, message="missing page")

			payload = bytes(data)
			if len(payload) > self.page_size:
				return OperationResult(False, message="data too large")
			if len(payload) < self.page_size:
				payload = payload + (b"\x00" * (self.page_size - len(payload)))

			offset = pno * self.page_size
			with path.open("r+b") as f:
				f.seek(offset, os.SEEK_SET)
				written = f.write(payload)
				f.flush()
			if written != self.page_size:
				return OperationResult(False, message="short write")
			self._disk_writes += 1
			self.log_write(file_id, pno)
			return WriteResult(True, message="page written", page_id=PageId(file=str(file_id), page_no=pno), bytes_written=written)
		except Exception as exc:
			return OperationResult(False, message=f"write_page failed: {exc}")

	def get_stats(self) -> StatsResult:
		return StatsResult(
			True,
			disk_reads=self._disk_reads,
			disk_writes=self._disk_writes,
		)

	def reset_stats(self) -> None:
		self._disk_reads = 0
		self._disk_writes = 0


__all__ = ["Config", "DiskSpaceManager", "PageId"]

