#!/usr/bin/env python3
"""Run Project 3 analysis experiments and write reproducible result tables.

Usage:
  python run_experiments.py
  python run_experiments.py --records 200 --queries 100
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
ARCHIVE = [sys.executable, str(ROOT / "archive.py")]
WORKLOAD_GEN = [sys.executable, str(ROOT / "workload_generator.py")]

OUTPUT_PATH = ROOT / "output.txt"
STATS_PATH = ROOT / "stats_output.txt"
LOG_PATH = ROOT / "log.csv"
CATALOG_PATH = ROOT / "catalog.json"
RESULTS_JSON = ROOT / "experiments" / "results.json"
RESULTS_MD = ROOT / "experiments" / "RESULTS.md"


@dataclass
class RunMetrics:
	label: str
	disk_reads: int = 0
	disk_writes: int = 0
	total_io: int = 0
	buffer_requests: int = 0
	buffer_hits: int = 0
	buffer_misses: int = 0
	hit_rate_pct: float = 0.0
	index_strategy: str = ""
	records_scanned: int = 0
	records_returned: int = 0


def _remove_if_exists(path: Path) -> None:
	try:
		path.unlink()
	except FileNotFoundError:
		return


def _clean_runtime_state() -> None:
	for path in (OUTPUT_PATH, STATS_PATH, CATALOG_PATH):
		_remove_if_exists(path)
	for p in ROOT.glob("*.bin"):
		_remove_if_exists(p)


def _write_config(
	*,
	replacement_policy: str = "LRU",
	index_strategy: str = "bplus_tree",
	buffer_pool_size: int = 16,
) -> None:
	config = {
		"page_size": 4096,
		"max_records_per_page": 10,
		"buffer_pool_size": buffer_pool_size,
		"replacement_policy": replacement_policy,
		"index_strategy": index_strategy,
	}
	CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _generate_workload(mode: str, records: int, queries: int, seed: int) -> str:
	proc = subprocess.run(
		WORKLOAD_GEN
		+ ["--mode", mode, "--records", str(records), "--queries", str(queries), "--seed", str(seed)],
		cwd=str(ROOT),
		text=True,
		capture_output=True,
		check=False,
	)
	if proc.returncode != 0:
		raise RuntimeError(f"workload_generator failed: {proc.stderr}")
	return proc.stdout.strip() + "\nstats\n"


def _parse_stats(text: str) -> RunMetrics:
	m = RunMetrics(label="")
	read_m = re.search(r"Disk I/O:\s*(\d+)\s*reads,\s*(\d+)\s*writes", text)
	if read_m:
		m.disk_reads = int(read_m.group(1))
		m.disk_writes = int(read_m.group(2))
		m.total_io = m.disk_reads + m.disk_writes

	pool_m = re.search(
		r"Buffer Pool:\s*(\d+)\s*requests,\s*(\d+)\s*hits,\s*(\d+)\s*misses\s*\(([\d.]+)% hit rate\)",
		text,
	)
	if pool_m:
		m.buffer_requests = int(pool_m.group(1))
		m.buffer_hits = int(pool_m.group(2))
		m.buffer_misses = int(pool_m.group(3))
		m.hit_rate_pct = float(pool_m.group(4))

	index_m = re.search(r"Index:\s*([^,]+),", text)
	if index_m:
		m.index_strategy = index_m.group(1).strip()

	rec_m = re.search(r"Records:\s*(\d+)\s*scanned,\s*(\d+)\s*returned", text)
	if rec_m:
		m.records_scanned = int(rec_m.group(1))
		m.records_returned = int(rec_m.group(2))
	return m


def _run_workload_text(workload_text: str, label: str) -> RunMetrics:
	_clean_runtime_state()
	input_path = ROOT / "experiments" / "_tmp_input.txt"
	input_path.parent.mkdir(parents=True, exist_ok=True)
	input_path.write_text(workload_text, encoding="utf-8")

	proc = subprocess.run(
		ARCHIVE + [str(CONFIG_PATH.name), str(input_path.relative_to(ROOT))],
		cwd=str(ROOT),
		text=True,
		capture_output=True,
	)
	if proc.returncode != 0:
		raise RuntimeError(f"archive.py failed for {label}: {proc.stderr}")

	if not STATS_PATH.exists():
		raise RuntimeError(f"stats_output.txt missing after run: {label}")

	stats_text = STATS_PATH.read_text(encoding="utf-8")
	metrics = _parse_stats(stats_text)
	metrics.label = label
	return metrics


def experiment1(records: int, queries: int, seed: int) -> list[RunMetrics]:
	results: list[RunMetrics] = []
	for mode in ("sequential", "random"):
		workload = _generate_workload(mode, records, queries, seed)
		for policy in ("LRU", "MRU"):
			_write_config(replacement_policy=policy, index_strategy="bplus_tree", buffer_pool_size=16)
			label = f"exp1_{mode}_{policy}"
			results.append(_run_workload_text(workload, label))
	return results


def experiment2(records: int, queries: int, seed: int) -> list[RunMetrics]:
	results: list[RunMetrics] = []
	workloads = {
		"equality": _generate_workload("random", records, queries, seed),
		"range": _generate_workload("range", records, queries, seed),
	}
	for query_type, workload in workloads.items():
		for strategy in ("heap_scan", "hash_index", "bplus_tree"):
			_write_config(replacement_policy="LRU", index_strategy=strategy, buffer_pool_size=16)
			label = f"exp2_{query_type}_{strategy}"
			results.append(_run_workload_text(workload, label))
	return results


def experiment3(records: int, queries: int, seed: int) -> list[RunMetrics]:
	results: list[RunMetrics] = []
	workload = _generate_workload("mixed", records, queries, seed)
	for pool_size in (4, 8, 16, 32, 64):
		_write_config(replacement_policy="LRU", index_strategy="bplus_tree", buffer_pool_size=pool_size)
		label = f"exp3_pool_{pool_size}"
		results.append(_run_workload_text(workload, label))
	return results


def _format_table_exp1(rows: list[RunMetrics]) -> str:
	by: dict[str, dict[str, RunMetrics]] = {}
	for r in rows:
		parts = r.label.split("_", 2)
		if len(parts) < 3:
			continue
		mode, policy = parts[1], parts[2]
		by.setdefault(mode, {})[policy] = r

	lines = [
		"| Workload | LRU I/Os | LRU Hit Rate | MRU I/Os | MRU Hit Rate |",
		"|----------|----------|--------------|----------|--------------|",
	]
	for mode in ("sequential", "random"):
		lru = by.get(mode, {}).get("LRU")
		mru = by.get(mode, {}).get("MRU")
		lines.append(
			f"| {mode.capitalize()} | "
			f"{lru.total_io if lru else '—'} | "
			f"{(f'{lru.hit_rate_pct:.1f}%') if lru else '—'} | "
			f"{mru.total_io if mru else '—'} | "
			f"{(f'{mru.hit_rate_pct:.1f}%') if mru else '—'} |"
		)
	return "\n".join(lines)


def _format_table_exp2(rows: list[RunMetrics]) -> str:
	by: dict[str, dict[str, RunMetrics]] = {}
	for r in rows:
		parts = r.label.split("_", 2)
		if len(parts) < 3:
			continue
		qtype, strategy = parts[1], parts[2]
		by.setdefault(qtype, {})[strategy] = r

	lines = [
		"| Query Type | heap_scan | hash_index | bplus_tree |",
		"|------------|-----------|------------|------------|",
	]
	for qtype in ("equality", "range"):
		row = by.get(qtype, {})
		cells = []
		for strategy in ("heap_scan", "hash_index", "bplus_tree"):
			m = row.get(strategy)
			if m is None:
				cells.append("—")
			elif qtype == "range" and strategy == "hash_index":
				cells.append(f"{m.total_io} (heap fallback)")
			else:
				cells.append(str(m.total_io))
		lines.append(f"| {qtype.capitalize()} | " + " | ".join(cells) + " |")
	return "\n".join(lines)


def _format_table_exp3(rows: list[RunMetrics]) -> str:
	lines = [
		"| Buffer Size | Total I/Os | Hit Rate |",
		"|-------------|------------|----------|",
	]
	for r in sorted(rows, key=lambda x: int(x.label.rsplit("_", 1)[-1])):
		size = r.label.rsplit("_", 1)[-1]
		lines.append(f"| {size} | {r.total_io} | {r.hit_rate_pct:.1f}% |")
	return "\n".join(lines)


def _write_results_md(exp1: list[RunMetrics], exp2: list[RunMetrics], exp3: list[RunMetrics], args: argparse.Namespace) -> None:
	RESULTS_MD.parent.mkdir(parents=True, exist_ok=True)
	body = f"""# Experiment Results

Generated by `run_experiments.py` with `--records {args.records} --queries {args.queries} --seed {args.seed}`.

Fixed settings unless noted:
- `page_size`: 4096
- `max_records_per_page`: 10
- Experiment 1 & 2 buffer pool: 16
- Experiment 1 & 3 index: bplus_tree
- Experiment 2 & 3 replacement policy: LRU

Total I/O = disk reads + disk writes (from `stats` at end of each workload).

## Experiment 1 — LRU vs MRU

{_format_table_exp1(exp1)}

**Interpretation (sequential flooding):** Repeated full range scans with a small buffer hurt LRU because early scan pages are evicted before the next scan restarts. MRU evicts the trailing page of the current scan and retains earlier pages, improving hit rate on the next scan. Random lookups reuse a smaller hot set; MRU still achieved fewer I/Os in our run.

## Experiment 2 — Index Strategy Comparison

{_format_table_exp2(exp2)}

**Interpretation:** Equality lookups benefit from hash and B+ tree indexes (fewer records/pages scanned than heap scan). Range queries on integer fields use the B+ tree directly; hash_index falls back to heap scan (higher I/O). heap_scan always scans more data pages.

## Experiment 3 — Buffer Pool Size Sensitivity

Workload: `mixed` with bplus_tree and LRU.

{_format_table_exp3(exp3)}

**Interpretation:** Larger buffer pools reduce total I/O and increase hit rate until working set fits in memory; gains diminish after the workload's hot set is cached.
"""
	RESULTS_MD.write_text(body, encoding="utf-8")


def main() -> int:
	parser = argparse.ArgumentParser(description="Run CMPE321 Project 3 experiments.")
	parser.add_argument("--records", type=int, default=200, help="Records for workload generator.")
	parser.add_argument("--queries", type=int, default=100, help="Queries for workload generator.")
	parser.add_argument("--seed", type=int, default=42, help="Random seed for workloads.")
	args = parser.parse_args()

	print("Running Experiment 1 (LRU vs MRU)...")
	exp1 = experiment1(args.records, args.queries, args.seed)
	print("Running Experiment 2 (index strategies)...")
	exp2 = experiment2(args.records, args.queries, args.seed)
	print("Running Experiment 3 (buffer pool sizes)...")
	exp3 = experiment3(args.records, args.queries, args.seed)

	RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
	payload = {
		"parameters": {"records": args.records, "queries": args.queries, "seed": args.seed},
		"experiment1": [asdict(r) for r in exp1],
		"experiment2": [asdict(r) for r in exp2],
		"experiment3": [asdict(r) for r in exp3],
	}
	RESULTS_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
	_write_results_md(exp1, exp2, exp3, args)

	print(f"Wrote {RESULTS_JSON}")
	print(f"Wrote {RESULTS_MD}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
