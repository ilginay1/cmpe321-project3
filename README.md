# CMPE 321 Project 3 — Modular DBMS Engine

A four-layer database engine built with **Python 3** and the **standard library only**. A single entry point (`archive.py`) wires the layers together using `config.json`.

## Quick start

```bash
python archive.py config.json input.txt
```

On Windows you may use `python` instead of `python3`.

After a run, check:

| File | Purpose |
|------|---------|
| `output.txt` | Query results and `explain` output (cleared at start of each run) |
| `stats_output.txt` | Snapshot written by the `stats` command |
| `log.csv` | Append-only operation log (persists across runs) |
| `catalog.json` | System catalog (schemas) |
| `<type_name>.bin` | On-disk relation and index pages |

## Configuration (`config.json`)

| Field | Values | Meaning |
|-------|--------|---------|
| `page_size` | e.g. `4096` | Fixed page size in bytes |
| `max_records_per_page` | e.g. `10` | Slot cap per page |
| `buffer_pool_size` | `4`, `8`, `16`, … | Number of page frames in memory |
| `replacement_policy` | `"LRU"`, `"MRU"` | Buffer eviction policy |
| `index_strategy` | `"heap_scan"`, `"hash_index"`, `"bplus_tree"` | How records are found |

Example default:

```json
{
  "page_size": 4096,
  "max_records_per_page": 10,
  "buffer_pool_size": 16,
  "replacement_policy": "LRU",
  "index_strategy": "bplus_tree"
}
```

Change **only** `config.json` between runs to compare policies or indexes; keep the same `input.txt`.

## Supported commands (`input.txt`)

One command per line (see project PDF for full grammar):

- `create type <name> <n_fields> <pk_pos> <field> <type> ...`
- `create record <type> <pk> <field_values...>`
- `delete record <type> <pk>`
- `search record <type> <pk>`
- `range_search <type> <field> <low> <high>` (integer fields only)
- `explain <any command above>`
- `stats` / `stats reset`

## Layer architecture

```
QueryProcessor
    → FileIndexManager   (schemas, records, indexes, slotted pages)
        → BufferManager  (LRU/MRU cache, dirty pages, flush)
            → DiskSpaceManager  (fixed-size pages in .bin files)
```

- **Disk Space Manager** — Only layer that performs real file I/O; tracks read/write counts; `log_write` stub on every write.
- **Buffer Manager** — Page cache; all upper layers access disk through this layer.
- **File & Index Manager** — Catalog, slotted pages, `heap_scan` / `hash_index` / `bplus_tree`.
- **Query Processor** — Parses commands, delegates to FileIndexManager, writes `output.txt` / `stats_output.txt` / `log.csv`.

Inter-layer calls return **Result** objects (`common/result.py`) carrying success, data, and metrics.

## Workload generator

For experiments and stress tests:

```bash
python workload_generator.py --mode random --records 100 --queries 50 > workload.txt
```

Modes: `sequential`, `random`, `range`, `mixed` (see `CMPE321_Proje_3.pdf` §10).

## Experiments and tests

| Script | Purpose |
|--------|---------|
| `python run_experiments.py` | Runs Experiments 1–3; writes `experiments/RESULTS.md` |
| `python test_part_a_storage_buffer.py` | Strict DiskSpaceManager + BufferManager tests |
| `python run_strategy_tests.py` | Compares output across index strategies |

Reproduction details: **`record.txt`**.

## AI usage

See **`ai_usage.md`** for tools used and verification steps.

## Submission layout

```
archive.py
disk_space_manager/
buffer_manager/
file_index_manager/
query_processor/
common/
workload_generator.py
config.json
input.txt
README.md
record.txt
ai_usage.md
individual_contribution_*.md
experiments/RESULTS.md
```

## Authors

Group 44 — see `individual_contribution_*.md` files for per-member tasks.
