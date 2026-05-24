from __future__ import annotations

import csv
import shlex
import time
from pathlib import Path
from typing import Any, Dict, Iterable

from buffer_manager import BufferManager
from common.result import OperationResult, StatsResult
from disk_space_manager import DiskSpaceManager
from file_index_manager import FileIndexManager


Config = Dict[str, Any]


class QueryProcessor:
	"""Minimaler Query-Prozessor.

	Erwartet textuelle Commands pro Zeile. Implementiert NICHT das volle DBMS.
	TODO: Parser/Tokenizer, Operatoren, Transaktionen, Logging.
	"""

	def __init__(
		self,
		config: Config,
		file_index: FileIndexManager,
		buffer: BufferManager,
		disk: DiskSpaceManager,
	):
		self.config = config
		self.file_index = file_index
		self.buffer = buffer
		self.disk = disk

		# Output-Targets (konservativ im Projekt-Root; TODO: aus config lesbar machen)
		self._base_dir = Path(__file__).resolve().parents[1]
		self._output_path = self._base_dir / "output.txt"
		self._stats_output_path = self._base_dir / "stats_output.txt"
		self._log_path = self._base_dir / "log.csv"
		self._suppress_query_output: bool = False

		# Pro Run einmalig leeren (niemals in process()), damit ein Lauf frisch startet.
		# stats_output.txt wird nur bei `stats` überschrieben; log.csv ist append-only.
		try:
			self._output_path.parent.mkdir(parents=True, exist_ok=True)
			self._output_path.write_text("", encoding="utf-8")
		except Exception:
			# Initialisierung darf das System nicht crashen.
			pass

	def process(self, line: str) -> OperationResult | StatsResult:
		"""Parst und verarbeitet eine einzelne Operation.

		Requirements:
		- darf bei malformed input nie crashen
		- schreibt je nach Command in output.txt / stats_output.txt
		- appends immer eine Zeile in log.csv: timestamp, operation, success/failure
		"""

		original = line.rstrip("\n")
		if not original.strip():
			# Leere Zeilen sind keine Operation.
			return OperationResult(True, message="empty")

		try:
			result = self._dispatch(original)
			self._append_log(original, success=self._is_success(result))
			return result
		except Exception as exc:  # noqa: BLE001 - Absichtlich: niemals crashen.
			self._append_log(original, success=False)
			return OperationResult(False, message=f"internal error: {exc}")

	# -----------------
	# Dispatch / Parsing
	# -----------------
	def _dispatch(self, original: str) -> OperationResult | StatsResult:
		try:
			tokens = shlex.split(original)
		except ValueError as exc:
			return OperationResult(False, message=f"parse error: {exc}")

		if not tokens:
			return OperationResult(True, message="empty")

		head = tokens[0].lower()

		if head == "create":
			return self._handle_create(tokens)

		if head == "delete":
			return self._handle_delete(tokens)

		if head == "search":
			return self._handle_search(tokens)

		if head == "range_search":
			return self._handle_range_search(tokens)

		if head == "explain":
			return self._handle_explain(tokens, original)

		if head == "stats":
			return self._handle_stats(tokens)

		return OperationResult(False, message=f"unknown command: {tokens[0]}")

	def _handle_create(self, tokens: list[str]) -> OperationResult:
		if len(tokens) < 2:
			return OperationResult(False, message="create: missing subcommand")

		sub = tokens[1].lower()
		if sub == "type":
			return self._handle_create_type(tokens)
		if sub == "record":
			return self._handle_create_record(tokens)

		return OperationResult(False, message=f"create: unknown subcommand {tokens[1]}")

	def _handle_create_type(self, tokens: list[str]) -> OperationResult:
		# create type <type-name> <num-fields> <primary-key-order> <field1-name> <field1-type> ...
		if len(tokens) < 5:
			return OperationResult(False, message="create type: too few arguments")

		type_name = tokens[2]
		try:
			num_fields = int(tokens[3])
			pk_order = int(tokens[4])
		except ValueError:
			return OperationResult(False, message="create type: num-fields/pk-order must be integers")

		expected = 5 + (2 * num_fields)
		if len(tokens) != expected:
			return OperationResult(
				False,
				message=f"create type: expected {2 * num_fields} field tokens, got {len(tokens) - 5}",
			)

		fields: list[tuple[str, str]] = []
		for i in range(num_fields):
			name = tokens[5 + 2 * i]
			ftype = tokens[5 + 2 * i + 1]
			fields.append((name, ftype))

		# Delegate: QueryProcessor darf Records nicht direkt anfassen.
		return self.file_index.create_type(type_name, fields, pk_order)

	def _handle_create_record(self, tokens: list[str]) -> OperationResult:
		# create record <type> <v1> <v2> ...
		if len(tokens) < 4:
			return OperationResult(False, message="create record: too few arguments")

		type_name = tokens[2]
		values = tokens[3:]
		return self.file_index.create_record(type_name, values)

	def _handle_delete(self, tokens: list[str]) -> OperationResult:
		# delete record <type> <pk-value>
		if len(tokens) != 4 or tokens[1].lower() != "record":
			return OperationResult(False, message="delete: expected 'delete record <type> <pk-value>'")
		return self.file_index.delete_record(tokens[2], tokens[3])

	def _handle_search(self, tokens: list[str]) -> OperationResult:
		# search record <type> <pk-value>
		if len(tokens) != 4 or tokens[1].lower() != "record":
			return OperationResult(False, message="search: expected 'search record <type> <pk-value>'")

		res = self.file_index.search_record(tokens[2], tokens[3])
		if not res.success:
			return res

		# Requirement: matching records -> output.txt append, one per line
		if (not self._suppress_query_output) and res.records:
			self._append_output_lines(self._format_records(res.records))
		return res

	def _handle_range_search(self, tokens: list[str]) -> OperationResult:
		# range_search <type> <field> <low> <high>
		if len(tokens) != 5:
			return OperationResult(False, message="range_search: expected 'range_search <type> <field> <low> <high>'")

		res = self.file_index.range_search(tokens[1], tokens[2], tokens[3], tokens[4])
		if not res.success:
			return res
		if (not self._suppress_query_output) and res.records:
			self._append_output_lines(self._format_records(res.records))
		return res

	def _handle_explain(self, tokens: list[str], original: str) -> OperationResult:
		# explain <any DML command>
		if len(tokens) < 2:
			return OperationResult(False, message="explain: missing inner command")

		inner = original[len(tokens[0]) :].strip()
		if not inner:
			return OperationResult(False, message="explain: missing inner command")

		# Snapshot stats before
		before = self._collect_stats()

		# Inner command darf für explain NICHT normal in output.txt schreiben.
		prev = self._suppress_query_output
		self._suppress_query_output = True
		try:
			inner_result = self._dispatch(inner)
		finally:
			self._suppress_query_output = prev
		if not self._is_success(inner_result):
			# Requirement: on failure, do not print query results.
			return OperationResult(False, message="explain: inner command failed")

		after = self._collect_stats()
		delta = self._delta_stats(before, after)

		strategy = self._active_index_strategy(after)
		est_io = self._estimate_io(inner, inner_result)

		records_lines: list[str] = []
		if isinstance(inner_result, OperationResult) and inner_result.records:
			records_lines = self._format_records(inner_result.records)

		explain_text = self._format_explain(
			query=inner,
			strategy=strategy,
			estimated_io=est_io,
			records=records_lines,
			reads=delta.disk_reads,
			writes=delta.disk_writes,
			hits=delta.buffer_hits,
			misses=delta.buffer_misses,
			pages_accessed=(inner_result.pages_accessed if isinstance(inner_result, OperationResult) else 0),
		)

		self._append_output_lines(explain_text.splitlines())
		return OperationResult(True, message="explain")

	def _handle_stats(self, tokens: list[str]) -> StatsResult | OperationResult:
		# stats
		# stats reset
		if len(tokens) == 1:
			stats = self._collect_stats()
			self._write_stats_output(stats)
			return stats

		if len(tokens) == 2 and tokens[1].lower() == "reset":
			for obj in (self.disk, self.buffer, self.file_index):
				reset = getattr(obj, "reset_stats", None)
				if callable(reset):
					try:
						reset()
					except Exception:
						# never crash
						pass
			return OperationResult(True, message="stats reset")

		return OperationResult(False, message="stats: expected 'stats' or 'stats reset'")

	# -----------------
	# Output / Logging
	# -----------------
	def _append_log(self, operation: str, success: bool) -> None:
		# Append-only
		ts = int(time.time())
		try:
			self._log_path.parent.mkdir(parents=True, exist_ok=True)
			with self._log_path.open("a", newline="", encoding="utf-8") as f:
				writer = csv.writer(f)
				writer.writerow([ts, operation, "success" if success else "failure"])
		except Exception:
			# Logging darf das System nicht crashen.
			return

	def _append_output_lines(self, lines: Iterable[str]) -> None:
		try:
			self._output_path.parent.mkdir(parents=True, exist_ok=True)
			with self._output_path.open("a", encoding="utf-8") as f:
				for line in lines:
					f.write(f"{line}\n")
		except Exception:
			# Output darf das System nicht crashen.
			return

	def _write_stats_output(self, stats: StatsResult) -> None:
		# Overwrite on every stats command
		hit_rate = 0.0
		if stats.buffer_requests > 0:
			hit_rate = (stats.buffer_hits / stats.buffer_requests) * 100.0

		text = (
			"=== STATISTICS ===\n"
			f"Disk I/O: {stats.disk_reads} reads, {stats.disk_writes} writes\n"
			f"Buffer Pool: {stats.buffer_requests} requests, {stats.buffer_hits} hits, {stats.buffer_misses} misses ({hit_rate:.2f}% hit rate)\n"
			f"Evictions: {stats.evictions} ({stats.dirty_writebacks} dirty writebacks)\n"
			f"Index: {stats.index_strategy}, {stats.index_nodes_visited} nodes visited\n"
			f"Records: {stats.records_scanned} scanned, {stats.records_returned} returned\n"
		)
		try:
			self._stats_output_path.parent.mkdir(parents=True, exist_ok=True)
			self._stats_output_path.write_text(text, encoding="utf-8")
		except Exception:
			return

	# -----------------
	# Stats helpers
	# -----------------
	def _collect_stats(self) -> StatsResult:
		"""Aggregiert Stats aus disk/buffer/file_idx (wenn verfügbar)."""

		agg = StatsResult(True)

		for obj in (self.disk, self.buffer, self.file_index):
			get_stats = getattr(obj, "get_stats", None)
			if not callable(get_stats):
				continue
			try:
				part = get_stats()
			except Exception:
				continue

			if isinstance(part, StatsResult):
				agg.disk_reads += part.disk_reads
				agg.disk_writes += part.disk_writes
				agg.buffer_requests += part.buffer_requests
				agg.buffer_hits += part.buffer_hits
				agg.buffer_misses += part.buffer_misses
				agg.evictions += part.evictions
				agg.dirty_writebacks += part.dirty_writebacks

				if part.index_strategy:
					agg.index_strategy = part.index_strategy
				agg.index_nodes_visited += part.index_nodes_visited
				agg.records_scanned += part.records_scanned
				agg.records_returned += part.records_returned
				continue

			if isinstance(part, OperationResult):
				# FileIndexManager liefert OperationResult als Stats-Carrier.
				agg.index_nodes_visited += part.index_nodes_visited
				agg.records_scanned += part.records_scanned
				agg.records_returned += part.records_returned
				if not agg.index_strategy:
					agg.index_strategy = str(getattr(self.file_index, "index_strategy", ""))

		return agg

	def _delta_stats(self, before: StatsResult, after: StatsResult) -> StatsResult:
		return StatsResult(
			True,
			disk_reads=max(0, after.disk_reads - before.disk_reads),
			disk_writes=max(0, after.disk_writes - before.disk_writes),
			buffer_requests=max(0, after.buffer_requests - before.buffer_requests),
			buffer_hits=max(0, after.buffer_hits - before.buffer_hits),
			buffer_misses=max(0, after.buffer_misses - before.buffer_misses),
			evictions=max(0, after.evictions - before.evictions),
			dirty_writebacks=max(0, after.dirty_writebacks - before.dirty_writebacks),
			index_strategy=after.index_strategy,
			index_nodes_visited=max(0, after.index_nodes_visited - before.index_nodes_visited),
			records_scanned=max(0, after.records_scanned - before.records_scanned),
			records_returned=max(0, after.records_returned - before.records_returned),
		)

	def _active_index_strategy(self, stats: StatsResult) -> str:
		if stats.index_strategy:
			return stats.index_strategy
		return str(getattr(self.file_index, "index_strategy", ""))

	# -----------------
	# Formatting
	# -----------------
	def _format_records(self, records: list[Any]) -> list[str]:
		"""Eine Record-zeilenweise Darstellung.

		QueryProcessor darf Records nicht interpretieren; nur stringifizieren.
		"""

		lines: list[str] = []
		for rec in records:
			if isinstance(rec, (list, tuple)):
				lines.append(" ".join(str(x) for x in rec))
			else:
				lines.append(str(rec))
		return lines

	def _format_explain(
		self,
		query: str,
		strategy: str,
		estimated_io: int,
		records: list[str],
		reads: int,
		writes: int,
		hits: int,
		misses: int,
		pages_accessed: int,
	) -> str:
		lines: list[str] = []
		lines.append("--- PLAN ---")
		lines.append(f"Query: {query}")
		lines.append(f"Strategy: {strategy}")
		lines.append(f"Estimated I/O: {estimated_io}")
		lines.append("--- RESULT ---")
		lines.extend(records)
		lines.append("--- STATS ---")
		lines.append(f"Actual I/O: {reads} reads, {writes} writes")
		lines.append(f"Buffer Hits: {hits}")
		lines.append(f"Buffer Misses: {misses}")
		lines.append(f"Pages Scanned: {pages_accessed}")
		return "\n".join(lines)

	def _estimate_io(self, query: str, result: OperationResult | StatsResult) -> int:
		# Simple estimate: default 1. If OperationResult provides pages_accessed, use that.
		if isinstance(result, OperationResult) and result.pages_accessed > 0:
			return result.pages_accessed
		return 1

	# -----------------
	# Result helpers
	# -----------------
	def _is_success(self, result: OperationResult | StatsResult) -> bool:
		return bool(getattr(result, "success", False))


__all__ = ["QueryProcessor"]

