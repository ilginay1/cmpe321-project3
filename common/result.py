from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass
class OperationResult:
	"""Result für Query/DDL/DML-Operationen auf höheren Schichten."""

	success: bool
	message: str = ""

	# Payload
	records: list[Any] = field(default_factory=list)

	# Safe Defaults für Metriken (0 bedeutet "nicht erfasst")
	pages_accessed: int = 0
	records_scanned: int = 0
	records_returned: int = 0
	index_nodes_visited: int = 0


@dataclass
class TypeResult:
	"""Result für Schema/Typ-Operationen (z.B. CREATE TABLE, DESCRIBE)."""

	success: bool
	message: str = ""
	schema: Mapping[str, Any] | None = None


@dataclass
class StatsResult:
	"""Result für Statistiken/Profiling über alle Schichten."""

	success: bool
	message: str = ""

	disk_reads: int = 0
	disk_writes: int = 0
	buffer_requests: int = 0
	buffer_hits: int = 0
	buffer_misses: int = 0
	evictions: int = 0
	dirty_writebacks: int = 0

	index_strategy: str = ""
	index_nodes_visited: int = 0
	records_scanned: int = 0
	records_returned: int = 0


@dataclass
class PageResult:
	"""Container für Lower-Layer Page-Reads.

	TODO: Später ggf. pin/dirty/lsn, checksums, etc.
	"""

	success: bool
	message: str = ""
	page_id: Any | None = None
	data: bytes = b""


@dataclass
class WriteResult:
	"""Container für Lower-Layer Writes/Flushes."""

	success: bool
	message: str = ""
	page_id: Any | None = None
	bytes_written: int = 0


__all__ = [
	"OperationResult",
	"TypeResult",
	"StatsResult",
	"PageResult",
	"WriteResult",
]

