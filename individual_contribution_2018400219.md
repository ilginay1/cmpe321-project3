# Individual Contribution Report

**Name:** Ebru Ozcaki  
**Student ID:** 2018400219  
**Group Number:** 44  

## Tasks and Contributions

I completed the remaining project deliverables and the analysis experiments after the core DBMS engine was implemented by my teammate. My main contributions include:

- **`run_experiments.py`** — Automates Experiment 1 (LRU vs MRU), Experiment 2 (index strategy comparison), and Experiment 3 (buffer pool size sensitivity). Generates workloads, runs `archive.py`, parses `stats_output.txt`, and writes `experiments/results.json` and `experiments/RESULTS.md`.
- **`record.txt`** — Documents exact commands, config fields, and result tables so experiments can be reproduced independently.
- **`README.md`** — Project overview, how to run, config options, layer diagram, and helper scripts.
- **`VIDEO_GUIDE.md`** — Structured script for the explanation and analysis sections of the project video.
- **`ai_usage.md`** — Updated honest disclosure of GitHub Copilot, ChatGPT, and Cursor Agent usage.
- **Experiment analysis** — Interpretation of sequential flooding (LRU vs MRU on repeated range scans), index strategy I/O comparison, and buffer pool sensitivity tables used in the report/video.

I verified the automated pipeline by running `python run_experiments.py` and cross-checking disk I/O and hit rates against `stats_output.txt`. I also ran `python test_part_a_storage_buffer.py` and `python run_strategy_tests.py` to confirm the existing engine still passes integration tests.

## Collaboration

My teammate Ilginay Gürcan (2025690684) implemented and tested the four DBMS layers (DiskSpaceManager, BufferManager, FileIndexManager, QueryProcessor), `workload_generator.py`, persistence, and the main test suite. I focused on experiments, documentation, and submission artifacts while reviewing the codebase to prepare the video explanation.

## Challenges

Understanding why MRU outperformed LRU on the **sequential** workload required reading the buffer eviction logic and relating it to repeated full-table range scans with a 16-page pool. Another challenge was keeping experiment runs reproducible (cleaning `catalog.json` and `.bin` between runs, fixed seed in the workload generator).

## Self-Assessment

I can explain how `archive.py` wires the layers, how to change `config.json` to switch policies and indexes, and how to reproduce all three experiments. I reviewed the QueryProcessor and BufferManager code paths used during `stats` and `explain` for the video.

## AI usage

GitHub Copilot and ChatGPT were used for learning and drafting (see `ai_usage.md`). Cursor Agent helped generate `run_experiments.py` and documentation; all outputs were run locally and checked against the real `stats_output.txt` format.
