# Individual Contribution Report

**Name:** Ilginay Gürcan  
**Student ID:** 2025690684 
**Group Number:** 44 

## Tasks and Contributions

In this project, I contributed to the implementation, testing, integration, and documentation of the modular DBMS engine. I first set up the local project structure, initialized the Git repository, created and managed branches, connected the project to GitHub, and cleaned the repository by configuring `.gitignore` for generated files.

I worked on the core structure of the DBMS engine, including the shared `Result` objects and the required `archive.py` entry point. I implemented and tested the `QueryProcessor`, which handles commands such as `create type`, `create record`, `delete record`, `search record`, `range_search`, `explain`, `stats`, and `stats reset`. I also made sure that query results are written correctly to `output.txt`, statistics are written to `stats_output.txt`, and all operations are logged persistently in `log.csv`.

A large part of my work focused on the `FileIndexManager`. I implemented schema handling, type creation, record insertion, deletion, search, range search, and validation of failure cases. This included duplicate type detection, duplicate primary key detection, missing records, missing types, invalid value counts, and invalid range searches on non-integer fields. I also implemented and tested the three index strategies: `heap_scan`, `hash_index`, and `bplus_tree`. I verified that all strategies produce the same query output while showing different statistics for scanned records and index usage.

I also contributed to the lower storage layers of the system. I implemented and tested the `DiskSpaceManager` with fixed-size page handling, binary file I/O, page allocation, disk read/write counters, and the required `log_write` stub. I implemented and tested the `BufferManager`, including buffer pool management, LRU and MRU replacement policies, hit/miss tracking, evictions, dirty page handling, dirty writebacks, and flushing. After that, I helped integrate persistent storage so that records are stored through the Buffer Manager and Disk Space Manager and remain available after restarting the program.

To verify the correctness of the system, I created and ran several tests. These included strategy tests for `heap_scan`, `hash_index`, and `bplus_tree`, a strict storage and buffer manager test, a persistence test across program restarts, and tests using both small examples and a six-field relation closer to the project specification. I also implemented a `workload_generator.py` script that generates sequential, random, range, and mixed workloads. 

Besides implementation, I contributed to code cleanup, refactoring, and documentation. I improved code readability, fixed inconsistent statistics counting, adjusted output behavior, cleaned repository files, improved test scripts, and helped prepare the AI usage report and contribution documentation.

## Challenges

One of the main challenges was keeping the system modular while making sure all layers worked together correctly. The Query Processor, File & Index Manager, Buffer Manager, and Disk Space Manager had to communicate consistently through result objects, and changes in one layer often affected the others.

Another challenge was debugging small but important correctness issues. For example, `output.txt` initially accumulated results from previous runs, statistics were not always counted consistently, and the B+ tree initially accepted range searches on string fields. These problems were found through testing and fixed step by step. Persistence was also an important challenge, because the system had to keep records available after restarting the program, not only during one in-memory run.

## Self-Assessment

Through this work, I gained a better understanding of how a DBMS engine is structured internally, including query processing, indexing, page-based storage, buffer management, persistence, logging, and statistics collection. I also improved my ability to debug systematically and verify correctness through reproducible test scripts.

One thing I learned is that integration testing is just as important as implementing individual components. Even if a component works alone, it still has to be tested together with the rest of the system.
