from __future__ import annotations

"""FileIndexManager backed by persistent fixed-size pages (stdlib-only).

Storage
- Each relation/type is stored in its own binary file: <type_name>.bin
- Pages are fixed-size (config["page_size"], default 4096) and accessed only via
  BufferManager.get_page / put_page (Disk I/O is encapsulated).

Catalog
- Catalog is kept in-memory for runtime.
- Catalog is persisted to catalog.json next to archive.py (project root).
- On startup, catalog.json is loaded (if present) and in-memory indexes are
  rebuilt by scanning relation pages through BufferManager.

Index strategies
- heap_scan: scan all occupied slots across pages
- hash_index: in-memory hash mapping primary key -> (page_no, slot_idx)
- bplus_tree: in-memory B+Tree mapping primary key -> (page_no, slot_idx)

Page format (simple slotted page)
- Header: record_count (uint16 little-endian) + 10 slot flags (bytes 0/1)
- Slots: fixed-length records, one per slot

Serialization
- int: 4 bytes signed (struct.pack('<i'))
- str: fixed 32 bytes ASCII, null-padded; reject longer than 32 bytes

TODO
- Persistent on-disk hash/b+tree structures (instead of rebuild on startup)
- WAL integration
"""

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from common.result import OperationResult
from file_index_manager.bplus_tree import BPlusTree


Config = Dict[str, Any]


@dataclass
class _Schema:
	type_name: str
	field_names: list[str]
	field_types: list[str]  # only: "int" | "str"
	primary_key_index: int  # 0-based


Pointer = Tuple[int, int]  # (page_no, slot_idx)


class FileIndexManager:
	def __init__(self, config: dict, buffer: Any):
		self.config = config
		self.buffer = buffer
		self.index_strategy: str = str(config.get("index_strategy", "heap_scan") or "heap_scan").strip().lower()

		self.page_size: int = int(config.get("page_size", 4096) or 4096)
		if self.page_size <= 0:
			self.page_size = 4096

		# Spec: header contains record_count and 10 slot flags.
		self.max_records_per_page: int = int(config.get("max_records_per_page", 10) or 10)
		if self.max_records_per_page <= 0:
			self.max_records_per_page = 10
		# We always reserve 10 slot flags in the header.
		self._slots_per_page: int = min(10, self.max_records_per_page)

		self._base_dir = Path(__file__).resolve().parents[1]
		self._catalog_path = self._base_dir / "catalog.json"

		# In-memory catalog
		self._schemas: dict[str, _Schema] = {}

		# In-memory indexes (rebuilt from pages on startup)
		self._hash_index: dict[str, dict[Any, Pointer]] = {}
		self._bptree: dict[str, BPlusTree[Any, Pointer]] = {}

		# Stats (cumulative)
		self._records_scanned: int = 0
		self._records_returned: int = 0
		self._pages_accessed: int = 0
		self._index_nodes_visited: int = 0

		self._load_catalog()
		self._rebuild_indexes()

	def _accumulate_stats(self, scanned: int = 0, returned: int = 0, pages: int = 0, index_nodes: int = 0) -> None:
		self._records_scanned += max(0, int(scanned))
		self._records_returned += max(0, int(returned))
		self._pages_accessed += max(0, int(pages))
		self._index_nodes_visited += max(0, int(index_nodes))

	# -----------------
	# Public API
	# -----------------
	def create_type(self, type_name: str, fields: Sequence[Any], primary_key_order: int) -> OperationResult:
		name = str(type_name).strip()
		if not name:
			return OperationResult(False, message="malformed schema")
		if name in self._schemas:
			return OperationResult(False, message="duplicate type")
		if not fields:
			return OperationResult(False, message="malformed schema")

		try:
			pk_order = int(primary_key_order)
		except Exception:
			return OperationResult(False, message="malformed schema")

		field_names: list[str] = []
		field_types: list[str] = []
		for f in fields:
			if isinstance(f, (list, tuple)) and len(f) == 2:
				fname, ftype = f
			elif isinstance(f, dict) and "name" in f and "type" in f:
				fname, ftype = f["name"], f["type"]
			else:
				return OperationResult(False, message="malformed schema")

			fname_s = str(fname).strip()
			ftype_s = str(ftype).strip().lower()
			if not fname_s:
				return OperationResult(False, message="malformed schema")
			if ftype_s not in {"int", "str"}:
				return OperationResult(False, message="invalid field type")
			if fname_s in field_names:
				return OperationResult(False, message="malformed schema")
			field_names.append(fname_s)
			field_types.append(ftype_s)

		if not (1 <= pk_order <= len(field_names)):
			return OperationResult(False, message="malformed schema")

		schema = _Schema(
			type_name=name,
			field_names=field_names,
			field_types=field_types,
			primary_key_index=pk_order - 1,
		)

		# Ensure record fits into a page with our slot count.
		record_size = self._record_size(schema)
		if self._header_size() + (self._slots_per_page * record_size) > self.page_size:
			return OperationResult(False, message="record too large for page")

		# Create the relation file via DiskSpaceManager, reachable through buffer.
		disk = getattr(self.buffer, "disk", None)
		if disk is None:
			return OperationResult(False, message="missing disk")
		create_res = disk.create_file(name)
		if not getattr(create_res, "success", False):
			return OperationResult(False, message=getattr(create_res, "message", "create_file failed"))

		self._schemas[name] = schema
		self._hash_index[name] = {}
		self._bptree[name] = BPlusTree(order=4)

		self._persist_catalog()
		return OperationResult(True, message="type created")

	def create_record(self, type_name: str, values: list[str]) -> OperationResult:
		schema = self._schemas.get(str(type_name))
		if schema is None:
			return OperationResult(False, message="missing type")
		if len(values) != len(schema.field_types):
			return OperationResult(False, message="wrong number of values")

		converted: list[Any] = []
		for idx, raw in enumerate(values):
			try:
				converted.append(self._convert_value(schema.field_types[idx], raw))
			except ValueError as exc:
				return OperationResult(False, message=str(exc) or "invalid value")

		pk_value = converted[schema.primary_key_index]

		# Duplicate PK check
		dup_check = self._check_duplicate_pk(schema, pk_value)
		if not dup_check.success:
			self._accumulate_stats(
				scanned=getattr(dup_check, "records_scanned", 0) or 0,
				pages=getattr(dup_check, "pages_accessed", 0) or 0,
				index_nodes=getattr(dup_check, "index_nodes_visited", 0) or 0,
			)
			return dup_check

		local_scanned = int(getattr(dup_check, "records_scanned", 0) or 0)
		local_dup_pages = int(getattr(dup_check, "pages_accessed", 0) or 0)
		local_index_nodes = int(getattr(dup_check, "index_nodes_visited", 0) or 0)

		# Find a free slot (or allocate a new page)
		location = self._find_free_slot(schema)
		if location is None:
			return OperationResult(False, message="no free slot")
		page_no, slot_idx, page_data, pages_from_find = location

		# Write record into slot
		buf = bytearray(page_data)
		rec_bytes = self._serialize_record(schema, converted)
		count, flags = self._read_page_header(buf)
		if slot_idx < 0 or slot_idx >= self._slots_per_page:
			return OperationResult(False, message="invalid slot")
		if flags[slot_idx] == 1:
			# Shouldn't happen, but keep safe.
			return OperationResult(False, message="slot already occupied")

		flags[slot_idx] = 1
		count = min(65535, count + 1)
		self._write_page_header(buf, count, flags)
		self._write_slot_bytes(schema, buf, slot_idx, rec_bytes)

		# Persist through BufferManager
		self.buffer.put_page(schema.type_name, page_no, bytes(buf), dirty=True)

		# Update indexes
		ptr: Pointer = (page_no, slot_idx)
		self._hash_index.setdefault(schema.type_name, {})[pk_value] = ptr
		self._bptree.setdefault(schema.type_name, BPlusTree(order=4)).insert(pk_value, ptr)

		local_pages = local_dup_pages + int(pages_from_find) + 1  # include the written page
		self._accumulate_stats(scanned=local_scanned, pages=local_pages, index_nodes=local_index_nodes)
		return OperationResult(
			True,
			message="record created",
			records_scanned=local_scanned,
			pages_accessed=local_pages,
			index_nodes_visited=local_index_nodes,
		)

	def delete_record(self, type_name: str, pk_value: str) -> OperationResult:
		schema = self._schemas.get(str(type_name))
		if schema is None:
			return OperationResult(False, message="missing type")

		try:
			pk = self._convert_value(schema.field_types[schema.primary_key_index], pk_value)
		except ValueError as exc:
			return OperationResult(False, message=str(exc) or "invalid value")

		# Try indexed path
		ptr = self._lookup_pointer(schema, pk)
		if ptr is not None:
			page_no, slot_idx = ptr
			page_res = self.buffer.get_page(schema.type_name, page_no)
			local_pages = 1
			local_index_nodes = 1 if self.index_strategy in {"hash_index", "bplus_tree"} else 0
			if getattr(page_res, "success", False):
				buf = bytearray(getattr(page_res, "data", b""))
				count, flags = self._read_page_header(buf)
				if 0 <= slot_idx < self._slots_per_page and flags[slot_idx] == 1:
					# Verify PK matches (stale index safety)
					values = self._read_slot_values(schema, buf, slot_idx)
					if values[schema.primary_key_index] == pk:
						flags[slot_idx] = 0
						count = max(0, count - 1)
						self._write_page_header(buf, count, flags)
						self.buffer.put_page(schema.type_name, page_no, bytes(buf), dirty=True)

						# Update indexes
						self._hash_index.get(schema.type_name, {}).pop(pk, None)
						self._bptree.setdefault(schema.type_name, BPlusTree(order=4)).delete(pk)

						self._accumulate_stats(scanned=1, pages=local_pages, index_nodes=local_index_nodes)
						return OperationResult(
							True,
							message="record deleted",
							records_scanned=1,
							pages_accessed=local_pages,
							index_nodes_visited=local_index_nodes,
						)

		# Fallback heap scan delete
		res = self._heap_scan_find(schema, pk)
		if not res.success:
			self._accumulate_stats(scanned=res.records_scanned, pages=res.pages_accessed)
			return res
		page_no, slot_idx, page_data = res.records[0]  # type: ignore[assignment]
		buf = bytearray(page_data)
		count, flags = self._read_page_header(buf)
		flags[slot_idx] = 0
		count = max(0, count - 1)
		self._write_page_header(buf, count, flags)
		self.buffer.put_page(schema.type_name, page_no, bytes(buf), dirty=True)
		local_pages = int(res.pages_accessed) + 1  # include written page

		# Update indexes
		self._hash_index.get(schema.type_name, {}).pop(pk, None)
		self._bptree.setdefault(schema.type_name, BPlusTree(order=4)).delete(pk)

		self._accumulate_stats(scanned=res.records_scanned, pages=local_pages)
		return OperationResult(True, message="record deleted", records_scanned=res.records_scanned, pages_accessed=local_pages)

	def search_record(self, type_name: str, pk_value: str) -> OperationResult:
		schema = self._schemas.get(str(type_name))
		if schema is None:
			return OperationResult(False, message="missing type")

		try:
			pk = self._convert_value(schema.field_types[schema.primary_key_index], pk_value)
		except ValueError as exc:
			return OperationResult(False, message=str(exc) or "invalid value")

		if self.index_strategy in {"hash_index", "bplus_tree"}:
			ptr = self._lookup_pointer(schema, pk)
			local_index_nodes = 1
			if ptr is None:
				self._accumulate_stats(scanned=1, pages=0, index_nodes=local_index_nodes)
				return OperationResult(False, message="missing record", records_scanned=1, pages_accessed=0, index_nodes_visited=local_index_nodes)

			page_no, slot_idx = ptr
			page_res = self.buffer.get_page(schema.type_name, page_no)
			local_pages = 1
			if not getattr(page_res, "success", False):
				self._accumulate_stats(scanned=1, pages=local_pages, index_nodes=local_index_nodes)
				return OperationResult(False, message="missing record", records_scanned=1, pages_accessed=local_pages, index_nodes_visited=local_index_nodes)

			buf = bytearray(getattr(page_res, "data", b""))
			count, flags = self._read_page_header(buf)
			if not (0 <= slot_idx < self._slots_per_page) or flags[slot_idx] != 1:
				self._accumulate_stats(scanned=1, pages=local_pages, index_nodes=local_index_nodes)
				return OperationResult(False, message="missing record", records_scanned=1, pages_accessed=local_pages, index_nodes_visited=local_index_nodes)

			values = self._read_slot_values(schema, buf, slot_idx)
			if values[schema.primary_key_index] != pk:
				# Stale index: fallback scan
				self._accumulate_stats(pages=local_pages, index_nodes=local_index_nodes)
				return self._heap_scan_search(schema, pk)

			line = self._format_record(values)
			self._accumulate_stats(scanned=1, returned=1, pages=local_pages, index_nodes=local_index_nodes)
			return OperationResult(True, message="found", records=[line], records_scanned=1, records_returned=1, pages_accessed=local_pages, index_nodes_visited=local_index_nodes)

		# heap_scan
		return self._heap_scan_search(schema, pk)

	def range_search(self, type_name: str, field_name: str, low: str, high: str) -> OperationResult:
		schema = self._schemas.get(str(type_name))
		if schema is None:
			return OperationResult(False, message="missing type")
		if field_name not in schema.field_names:
			return OperationResult(False, message="missing field")

		field_index = schema.field_names.index(field_name)
		# Project rule: range_search only for int fields.
		if schema.field_types[field_index] != "int":
			return OperationResult(False, message="range_search only supported for int fields")

		try:
			low_i = int(low)
			high_i = int(high)
		except Exception:
			return OperationResult(False, message="invalid range bounds")

		# bplus_tree can accelerate only on int primary key
		if self.index_strategy == "bplus_tree" and field_index == schema.primary_key_index:
			tree = self._bptree.setdefault(schema.type_name, BPlusTree(order=4))
			ptrs = tree.range_search(low_i, high_i)
			local_index_nodes = 1 + len(ptrs)

			# Group by page for fewer reads
			by_page: dict[int, list[int]] = {}
			for page_no, slot_idx in ptrs:
				by_page.setdefault(int(page_no), []).append(int(slot_idx))

			out: list[str] = []
			pages_accessed = 0
			for page_no, slots in by_page.items():
				page_res = self.buffer.get_page(schema.type_name, page_no)
				pages_accessed += 1
				if not getattr(page_res, "success", False):
					continue
				buf = bytearray(getattr(page_res, "data", b""))
				_, flags = self._read_page_header(buf)
				for slot_idx in slots:
					if 0 <= slot_idx < self._slots_per_page and flags[slot_idx] == 1:
						values = self._read_slot_values(schema, buf, slot_idx)
						out.append(self._format_record(values))

			returned = len(out)
			self._accumulate_stats(scanned=1, returned=returned, pages=pages_accessed, index_nodes=local_index_nodes)
			return OperationResult(
				True,
				message="range_search",
				records=out,
				records_scanned=1,
				records_returned=returned,
				pages_accessed=pages_accessed,
				index_nodes_visited=local_index_nodes,
			)

		# Otherwise: heap scan across pages
		res = self._heap_scan_range(schema, field_index, low_i, high_i)
		self._accumulate_stats(scanned=res.records_scanned, returned=res.records_returned, pages=res.pages_accessed)
		return res

	def get_stats(self) -> OperationResult:
		return OperationResult(
			True,
			message="stats",
			records_scanned=self._records_scanned,
			records_returned=self._records_returned,
			pages_accessed=self._pages_accessed,
			index_nodes_visited=self._index_nodes_visited,
		)

	def reset_stats(self) -> OperationResult:
		self._records_scanned = 0
		self._records_returned = 0
		self._pages_accessed = 0
		self._index_nodes_visited = 0
		return OperationResult(True, message="stats reset")

	# -----------------
	# Catalog
	# -----------------
	def _load_catalog(self) -> None:
		if not self._catalog_path.exists():
			return
		try:
			data = json.loads(self._catalog_path.read_text(encoding="utf-8"))
			if not isinstance(data, dict):
				return
			types = data.get("types")
			if not isinstance(types, dict):
				return
			for type_name, meta in types.items():
				if not isinstance(meta, dict):
					continue
				fields = meta.get("fields")
				pk_order = meta.get("primary_key_order")
				if not isinstance(fields, list):
					continue
				try:
					pk_order_i = int(pk_order)
				except Exception:
					continue

				field_names: list[str] = []
				field_types: list[str] = []
				ok = True
				for f in fields:
					if not isinstance(f, dict):
						ok = False
						break
					fname = str(f.get("name", "")).strip()
					ftype = str(f.get("type", "")).strip().lower()
					if not fname or ftype not in {"int", "str"} or fname in field_names:
						ok = False
						break
					field_names.append(fname)
					field_types.append(ftype)
				if not ok:
					continue
				if not (1 <= pk_order_i <= len(field_names)):
					continue

				schema = _Schema(
					type_name=str(type_name),
					field_names=field_names,
					field_types=field_types,
					primary_key_index=pk_order_i - 1,
				)
				# Ensure record fits
				record_size = self._record_size(schema)
				if self._header_size() + (self._slots_per_page * record_size) > self.page_size:
					continue
				self._schemas[schema.type_name] = schema
				self._hash_index[schema.type_name] = {}
				self._bptree[schema.type_name] = BPlusTree(order=4)
		except Exception:
			return

	def _persist_catalog(self) -> None:
		try:
			types: dict[str, Any] = {}
			for name, schema in self._schemas.items():
				types[name] = {
					"fields": [{"name": n, "type": t} for n, t in zip(schema.field_names, schema.field_types)],
					"primary_key_order": schema.primary_key_index + 1,
				}
			payload = {"types": types}
			self._catalog_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
		except Exception:
			return

	# -----------------
	# Index rebuild
	# -----------------
	def _rebuild_indexes(self) -> None:
		"""Rebuild hash_index and bplus_tree mappings by scanning all pages."""
		disk = getattr(self.buffer, "disk", None)
		if disk is None:
			return

		for type_name, schema in self._schemas.items():
			# Ensure file exists
			try:
				disk.create_file(type_name)
			except Exception:
				pass

			self._hash_index[type_name] = {}
			self._bptree[type_name] = BPlusTree(order=4)

			n_pages = 0
			try:
				n_pages = int(disk.num_pages(type_name) or 0)
			except Exception:
				n_pages = 0

			for page_no in range(n_pages):
				page_res = self.buffer.get_page(type_name, page_no)
				# Startup rebuild should not crash; ignore failures.
				if not getattr(page_res, "success", False):
					continue
				buf = bytearray(getattr(page_res, "data", b""))
				_, flags = self._read_page_header(buf)
				for slot_idx in range(self._slots_per_page):
					if flags[slot_idx] != 1:
						continue
					try:
						values = self._read_slot_values(schema, buf, slot_idx)
						pk = values[schema.primary_key_index]
						ptr: Pointer = (page_no, slot_idx)
						self._hash_index[type_name][pk] = ptr
						self._bptree[type_name].insert(pk, ptr)
					except Exception:
						continue

	# -----------------
	# Slotted page helpers
	# -----------------
	@staticmethod
	def _header_size() -> int:
		# record_count (2 bytes) + 10 slot flags
		return 2 + 10

	def _record_size(self, schema: _Schema) -> int:
		sz = 0
		for t in schema.field_types:
			if t == "int":
				sz += 4
			else:
				sz += 32
		return sz

	def _slot_offset(self, schema: _Schema, slot_idx: int) -> int:
		return self._header_size() + (slot_idx * self._record_size(schema))

	def _read_page_header(self, buf: bytearray) -> Tuple[int, List[int]]:
		if len(buf) < self.page_size:
			# Defensive: pad to full page
			buf.extend(b"\x00" * (self.page_size - len(buf)))
		count = struct.unpack_from("<H", buf, 0)[0]
		flags_raw = bytes(buf[2 : 2 + 10])
		flags = [1 if b != 0 else 0 for b in flags_raw]
		return int(count), flags

	def _write_page_header(self, buf: bytearray, count: int, flags: Sequence[int]) -> None:
		struct.pack_into("<H", buf, 0, int(count) & 0xFFFF)
		for i in range(10):
			b = 1 if (i < len(flags) and int(flags[i]) != 0) else 0
			buf[2 + i] = b

	def _find_free_slot_in_flags(self, flags: Sequence[int]) -> Optional[int]:
		for i in range(self._slots_per_page):
			if i < len(flags) and int(flags[i]) == 0:
				return i
		return None

	def _write_slot_bytes(self, schema: _Schema, buf: bytearray, slot_idx: int, rec_bytes: bytes) -> None:
		off = self._slot_offset(schema, slot_idx)
		end = off + self._record_size(schema)
		buf[off:end] = rec_bytes

	def _read_slot_values(self, schema: _Schema, buf: bytearray, slot_idx: int) -> list[Any]:
		off = self._slot_offset(schema, slot_idx)
		end = off + self._record_size(schema)
		chunk = bytes(buf[off:end])
		return self._deserialize_record(schema, chunk)

	def _serialize_record(self, schema: _Schema, values: Sequence[Any]) -> bytes:
		parts: list[bytes] = []
		for t, v in zip(schema.field_types, values):
			if t == "int":
				parts.append(struct.pack("<i", int(v)))
			else:
				s = str(v)
				try:
					b = s.encode("ascii")
				except UnicodeEncodeError:
					raise ValueError("invalid value")
				if len(b) > 32:
					raise ValueError("string too long")
				parts.append(b + (b"\x00" * (32 - len(b))))
		out = b"".join(parts)
		if len(out) != self._record_size(schema):
			raise ValueError("serialization error")
		return out

	def _deserialize_record(self, schema: _Schema, chunk: bytes) -> list[Any]:
		values: list[Any] = []
		off = 0
		for t in schema.field_types:
			if t == "int":
				(values_i,) = struct.unpack_from("<i", chunk, off)
				values.append(int(values_i))
				off += 4
			else:
				raw = chunk[off : off + 32]
				# Trim at first null
				raw = raw.split(b"\x00", 1)[0]
				values.append(raw.decode("ascii", errors="ignore"))
				off += 32
		return values

	# -----------------
	# Record operations helpers
	# -----------------
	@staticmethod
	def _convert_value(field_type: str, raw: str) -> Any:
		t = str(field_type).lower()
		if t == "int":
			try:
				return int(raw)
			except Exception:
				raise ValueError("invalid value")
		if t == "str":
			# Validate ASCII length <= 32
			s = str(raw)
			try:
				b = s.encode("ascii")
			except UnicodeEncodeError:
				raise ValueError("invalid value")
			if len(b) > 32:
				raise ValueError("string too long")
			return s
		raise ValueError("invalid field type")

	@staticmethod
	def _format_record(values: Sequence[Any]) -> str:
		return " ".join(str(v) for v in values)

	def _check_duplicate_pk(self, schema: _Schema, pk_value: Any) -> OperationResult:
		# Indexed fast path
		if self.index_strategy == "hash_index":
			if pk_value in self._hash_index.setdefault(schema.type_name, {}):
				return OperationResult(False, message="duplicate primary key", records_scanned=1, index_nodes_visited=1)
			return OperationResult(True, message="ok", records_scanned=1, index_nodes_visited=1)
		if self.index_strategy == "bplus_tree":
			tree = self._bptree.setdefault(schema.type_name, BPlusTree(order=4))
			if tree.search(pk_value) is not None:
				return OperationResult(False, message="duplicate primary key", records_scanned=1, index_nodes_visited=1)
			return OperationResult(True, message="ok", records_scanned=1, index_nodes_visited=1)

		# heap_scan duplicate check
		return self._heap_scan_duplicate(schema, pk_value)

	def _lookup_pointer(self, schema: _Schema, pk_value: Any) -> Optional[Pointer]:
		if self.index_strategy == "hash_index":
			return self._hash_index.setdefault(schema.type_name, {}).get(pk_value)
		if self.index_strategy == "bplus_tree":
			tree = self._bptree.setdefault(schema.type_name, BPlusTree(order=4))
			return tree.search(pk_value)
		return None

	def _find_free_slot(self, schema: _Schema) -> Optional[Tuple[int, int, bytes, int]]:
		"""Return (page_no, slot_idx, page_bytes, pages_accessed)."""
		disk = getattr(self.buffer, "disk", None)
		if disk is None:
			return None

		try:
			n_pages = int(disk.num_pages(schema.type_name) or 0)
		except Exception:
			n_pages = 0

		pages_accessed = 0
		# Scan existing pages
		for page_no in range(n_pages):
			page_res = self.buffer.get_page(schema.type_name, page_no)
			pages_accessed += 1
			if not getattr(page_res, "success", False):
				continue
			data = bytes(getattr(page_res, "data", b""))
			buf = bytearray(data)
			_, flags = self._read_page_header(buf)
			free = self._find_free_slot_in_flags(flags)
			if free is not None:
				return (page_no, free, bytes(buf), pages_accessed)

		# Allocate new page
		alloc_res = disk.allocate_page(schema.type_name)
		if not getattr(alloc_res, "success", False):
			return None
		pid = getattr(alloc_res, "page_id", None)
		page_no = int(getattr(pid, "page_no", n_pages))
		page_bytes = bytes(getattr(alloc_res, "data", b""))
		if len(page_bytes) != self.page_size:
			page_bytes = page_bytes.ljust(self.page_size, b"\x00")

		# Cache as clean page
		self.buffer.put_page(schema.type_name, page_no, page_bytes, dirty=False)

		buf = bytearray(page_bytes)
		count, flags = self._read_page_header(buf)
		# Ensure header initialized
		self._write_page_header(buf, 0, flags)
		free = self._find_free_slot_in_flags(flags)
		if free is None:
			free = 0
		return (page_no, free, bytes(buf), pages_accessed)

	# -----------------
	# Heap scan operations
	# -----------------
	def _heap_scan_duplicate(self, schema: _Schema, pk_value: Any) -> OperationResult:
		disk = getattr(self.buffer, "disk", None)
		if disk is None:
			return OperationResult(False, message="missing disk")

		scanned = 0
		pages = 0
		try:
			n_pages = int(disk.num_pages(schema.type_name) or 0)
		except Exception:
			n_pages = 0

		for page_no in range(n_pages):
			page_res = self.buffer.get_page(schema.type_name, page_no)
			pages += 1
			if not getattr(page_res, "success", False):
				continue
			buf = bytearray(getattr(page_res, "data", b""))
			_, flags = self._read_page_header(buf)
			for slot_idx in range(self._slots_per_page):
				if flags[slot_idx] != 1:
					continue
				scanned += 1
				values = self._read_slot_values(schema, buf, slot_idx)
				if values[schema.primary_key_index] == pk_value:
					return OperationResult(False, message="duplicate primary key", records_scanned=scanned, pages_accessed=pages)

		return OperationResult(True, message="ok", records_scanned=scanned, pages_accessed=pages)

	def _heap_scan_search(self, schema: _Schema, pk_value: Any) -> OperationResult:
		res = self._heap_scan_find(schema, pk_value)
		if not res.success:
			self._accumulate_stats(scanned=res.records_scanned, pages=res.pages_accessed)
			return OperationResult(False, message="missing record", records_scanned=res.records_scanned, pages_accessed=res.pages_accessed)
		page_no, slot_idx, page_data = res.records[0]  # type: ignore[assignment]
		buf = bytearray(page_data)
		values = self._read_slot_values(schema, buf, slot_idx)
		line = self._format_record(values)
		self._accumulate_stats(scanned=res.records_scanned, returned=1, pages=res.pages_accessed)
		return OperationResult(True, message="found", records=[line], records_scanned=res.records_scanned, records_returned=1, pages_accessed=res.pages_accessed)

	def _heap_scan_find(self, schema: _Schema, pk_value: Any) -> OperationResult:
		"""Find record location via heap scan.

		Returns success with records=[(page_no, slot_idx, page_bytes)] as payload.
		"""
		disk = getattr(self.buffer, "disk", None)
		if disk is None:
			return OperationResult(False, message="missing disk")

		scanned = 0
		pages = 0
		try:
			n_pages = int(disk.num_pages(schema.type_name) or 0)
		except Exception:
			n_pages = 0

		for page_no in range(n_pages):
			page_res = self.buffer.get_page(schema.type_name, page_no)
			pages += 1
			if not getattr(page_res, "success", False):
				continue
			buf = bytearray(getattr(page_res, "data", b""))
			_, flags = self._read_page_header(buf)
			for slot_idx in range(self._slots_per_page):
				if flags[slot_idx] != 1:
					continue
				scanned += 1
				values = self._read_slot_values(schema, buf, slot_idx)
				if values[schema.primary_key_index] == pk_value:
					return OperationResult(True, message="found", records=[(page_no, slot_idx, bytes(buf))], records_scanned=scanned, pages_accessed=pages)

		return OperationResult(False, message="missing record", records_scanned=scanned, pages_accessed=pages)

	def _heap_scan_range(self, schema: _Schema, field_index: int, low_i: int, high_i: int) -> OperationResult:
		disk = getattr(self.buffer, "disk", None)
		if disk is None:
			return OperationResult(False, message="missing disk")

		scanned = 0
		returned = 0
		pages = 0
		out: list[str] = []
		try:
			n_pages = int(disk.num_pages(schema.type_name) or 0)
		except Exception:
			n_pages = 0

		for page_no in range(n_pages):
			page_res = self.buffer.get_page(schema.type_name, page_no)
			pages += 1
			if not getattr(page_res, "success", False):
				continue
			buf = bytearray(getattr(page_res, "data", b""))
			_, flags = self._read_page_header(buf)
			for slot_idx in range(self._slots_per_page):
				if flags[slot_idx] != 1:
					continue
				scanned += 1
				values = self._read_slot_values(schema, buf, slot_idx)
				v = values[field_index]
				if isinstance(v, int) and low_i <= v <= high_i:
					returned += 1
					out.append(self._format_record(values))

		return OperationResult(
			True,
			message="range_search",
			records=out,
			records_scanned=scanned,
			records_returned=returned,
			pages_accessed=pages,
			index_nodes_visited=0,
		)


__all__ = ["FileIndexManager"]
