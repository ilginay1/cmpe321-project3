#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
INPUT_PATH = ROOT / "input.txt"

OUTPUT_PATH = ROOT / "output.txt"
STATS_PATH = ROOT / "stats_output.txt"
LOG_PATH = ROOT / "log.csv"
CATEGORY_CATALOG = ROOT / "catalog.json"


def _write_config(index_strategy: str) -> None:
	config = {
		"page_size": 4096,
		"max_records_per_page": 10,
		"buffer_pool_size": 16,
		"replacement_policy": "LRU",
		"index_strategy": index_strategy,
	}
	CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _remove_if_exists(path: Path) -> None:
	try:
		path.unlink()
	except FileNotFoundError:
		return


def _remove_persistent_state() -> None:
	_remove_if_exists(CATEGORY_CATALOG)
	for p in ROOT.glob("*.bin"):
		_remove_if_exists(p)


def _read_text_or_missing(path: Path) -> str:
	try:
		return path.read_text(encoding="utf-8")
	except FileNotFoundError:
		return "(missing)\n"


def _run_once(strategy: str) -> None:
	_write_config(strategy)
	_remove_persistent_state()

	_remove_if_exists(OUTPUT_PATH)
	_remove_if_exists(STATS_PATH)
	_remove_if_exists(LOG_PATH)

	proc = subprocess.run(
		["python3", "archive.py", "config.json", "input.txt"],
		cwd=str(ROOT),
		text=True,
		capture_output=True,
	)

	print("=" * 80)
	print(f"STRATEGY: {strategy}")
	print(f"RETURN CODE: {proc.returncode}")
	if proc.stdout:
		print("--- STDOUT ---")
		print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
	if proc.stderr:
		print("--- STDERR ---")
		print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n")

	print("--- output.txt ---")
	print(_read_text_or_missing(OUTPUT_PATH), end="")

	print("--- stats_output.txt ---")
	print(_read_text_or_missing(STATS_PATH), end="")

	print("--- log.csv ---")
	print(_read_text_or_missing(LOG_PATH), end="")


def main() -> None:
	if not INPUT_PATH.exists():
		raise SystemExit("input.txt not found in project root")

	for strategy in ("heap_scan", "hash_index", "bplus_tree"):
		_run_once(strategy)


if __name__ == "__main__":
	main()
