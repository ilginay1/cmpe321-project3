#!/usr/bin/env python3

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass


SCHEMA_LINE = "create type Person 4 1 id int name str age int score int"


def _positive_int(value: str) -> int:
	try:
		n = int(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError("must be an integer") from exc
	if n <= 0:
		raise argparse.ArgumentTypeError("must be a positive integer")
	return n


def _person_record_line(person_id: int) -> str:
	# Requirements:
	# - id from 1..N (or above for mixed inserts)
	# - name alphanumeric only
	# - age int (example formula)
	# - score int (example formula)
	name = f"Name{person_id}"
	age = 18 + (person_id % 50)
	score = (person_id * 7) % 1000
	# Name<id> is alphanumeric only.
	return f"create record Person {person_id} {name} {age} {score}"


def _rand_range(rng: random.Random, low: int, high: int) -> tuple[int, int]:
	if low > high:
		low, high = high, low
	if low == high:
		return low, high
	a = rng.randint(low, high)
	b = rng.randint(low, high)
	return (a, b) if a <= b else (b, a)


@dataclass
class _MixedState:
	existing_ids: set[int]
	next_insert_id: int
	max_id_hint: int


def _choose_existing_id(rng: random.Random, state: _MixedState) -> int | None:
	if not state.existing_ids:
		return None
	# random.sample on set is fine but needs a sequence; convert cheaply.
	return rng.choice(tuple(state.existing_ids))


def _emit_create_type() -> None:
	print(SCHEMA_LINE)


def _emit_initial_inserts(n_records: int) -> None:
	for person_id in range(1, n_records + 1):
		print(_person_record_line(person_id))


def _emit_sequential_queries(q: int, n_records: int) -> None:
	for _ in range(q):
		print(f"range_search Person id 1 {n_records}")


def _emit_random_queries(rng: random.Random, q: int, n_records: int) -> None:
	for _ in range(q):
		rid = rng.randint(1, n_records)
		print(f"search record Person {rid}")


def _emit_range_queries(rng: random.Random, q: int, n_records: int) -> None:
	fields = ("id", "age", "score")
	for _ in range(q):
		field = rng.choice(fields)
		if field == "id":
			lo, hi = _rand_range(rng, 1, n_records)
		elif field == "age":
			# age = 18 + (id % 50) => 18..67
			lo, hi = _rand_range(rng, 18, 67)
		else:
			# score = (id*7)%1000 => within 0..999
			lo, hi = _rand_range(rng, 0, 999)
		print(f"range_search Person {field} {lo} {hi}")


def _emit_mixed_ops(rng: random.Random, q: int, n_records: int) -> None:
	state = _MixedState(existing_ids=set(range(1, n_records + 1)), next_insert_id=n_records + 1, max_id_hint=n_records)

	# Simple, readable weights.
	ops = (
		"search",
		"range",
		"insert",
		"delete",
	)
	weights = (0.45, 0.25, 0.20, 0.10)

	for _ in range(q):
		op = rng.choices(ops, weights=weights, k=1)[0]

		if op == "insert":
			new_id = state.next_insert_id
			state.next_insert_id += 1
			state.max_id_hint = max(state.max_id_hint, new_id)
			state.existing_ids.add(new_id)
			print(_person_record_line(new_id))
			continue

		if op == "delete":
			del_id = _choose_existing_id(rng, state)
			if del_id is None:
				# If nothing exists, fall back to insert.
				new_id = state.next_insert_id
				state.next_insert_id += 1
				state.max_id_hint = max(state.max_id_hint, new_id)
				state.existing_ids.add(new_id)
				print(_person_record_line(new_id))
				continue
			state.existing_ids.discard(del_id)
			print(f"delete record Person {del_id}")
			continue

		if op == "search":
			# Choose an existing id most of the time.
			choose_existing = bool(state.existing_ids) and (rng.random() < 0.85)
			if choose_existing:
				sid = _choose_existing_id(rng, state)
				assert sid is not None
				print(f"search record Person {sid}")
			else:
				# Occasionally search for a possibly-missing id.
				max_id = max(state.max_id_hint, 1)
				sid = rng.randint(1, max_id + max(10, n_records // 10))
				print(f"search record Person {sid}")
			continue

		# range op (int fields only)
		field = rng.choice(("id", "age", "score"))
		if field == "id":
			max_id = max(state.max_id_hint, 1)
			lo, hi = _rand_range(rng, 1, max_id)
		elif field == "age":
			lo, hi = _rand_range(rng, 18, 67)
		else:
			lo, hi = _rand_range(rng, 0, 999)
		print(f"range_search Person {field} {lo} {hi}")


def build_arg_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(description="Generate DBMS workloads (prints commands to stdout).")
	p.add_argument(
		"--mode",
		required=True,
		choices=("sequential", "random", "range", "mixed"),
		help="Workload mode.",
	)
	p.add_argument("--records", required=True, type=_positive_int, help="Number of initial records to insert.")
	p.add_argument("--queries", required=True, type=_positive_int, help="Number of queries/operations after inserts.")
	p.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility (default: 42).")
	return p


def main(argv: list[str]) -> int:
	args = build_arg_parser().parse_args(argv[1:])
	rng = random.Random(int(args.seed))

	n_records = int(args.records)
	q = int(args.queries)
	mode = str(args.mode)

	_emit_create_type()
	_emit_initial_inserts(n_records)

	if mode == "sequential":
		_emit_sequential_queries(q, n_records)
		return 0
	if mode == "random":
		_emit_random_queries(rng, q, n_records)
		return 0
	if mode == "range":
		_emit_range_queries(rng, q, n_records)
		return 0
	if mode == "mixed":
		_emit_mixed_ops(rng, q, n_records)
		return 0

	print(f"unknown mode: {mode}", file=sys.stderr)
	return 2


if __name__ == "__main__":
	raise SystemExit(main(sys.argv))
