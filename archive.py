from __future__ import annotations

import json
import sys
from pathlib import Path

from buffer_manager import BufferManager
from disk_space_manager import DiskSpaceManager
from file_index_manager import FileIndexManager
from query_processor import QueryProcessor


def _load_config(config_path: str) -> dict:
	path = Path(config_path)
	raw = path.read_text(encoding="utf-8")
	if not raw.strip():
		# TODO: Optional stricter validation once config format is fixed.
		return {}
	data = json.loads(raw)
	if not isinstance(data, dict):
		raise TypeError("config must be a JSON object")
	return data


def main(argv: list[str]) -> int:
	if len(argv) < 3:
		print("Usage: python archive.py <config.json> <input.txt>", file=sys.stderr)
		return 2

	# Robust: relative Argumente relativ zur Script-Location auflösen.
	base_dir = Path(__file__).resolve().parent
	config_path = Path(argv[1])
	input_path = Path(argv[2])
	if not config_path.is_absolute():
		config_path = base_dir / config_path
	if not input_path.is_absolute():
		input_path = base_dir / input_path

	config = _load_config(str(config_path))

	disk = DiskSpaceManager(config)
	buffer = BufferManager(config, disk)
	file_idx = FileIndexManager(config, buffer)
	qp = QueryProcessor(config, file_idx, buffer, disk)

	with input_path.open("r", encoding="utf-8") as f:
		for raw_line in f:
			line = raw_line.strip()
			if not line:
				continue
			# Requirement: jede nicht-leere Eingabezeile verarbeiten.
			qp.process(line)

	# Requirement: am Ende flush() aufrufen.
	buffer.flush()
	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv))

