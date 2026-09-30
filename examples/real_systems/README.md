# Real-system evidence profiles

These profiles separate tool outcomes from specification judgments. Normal checker runs are mechanical and do not require an agent. Each case documents its source revision, exact scope, budgets and remaining gaps.

| Case | Scope | Entry point |
|---|---|---|
| [HFS v3](hfs/README.md) | Original native Cargo crate; complete `open_file` clauses; an explicitly conditional error-alternative probe | `hfs/run.py --source-root ... --out ...` |
| [Nanvix](nanvix/README.md) | Source-sealed bitmap extract with recorded dependency and body boundaries | `nanvix/run.py --help` |
| [mimalloc](mimalloc/README.md) | Native commit-mask module, unchanged contracts and bodies | `mimalloc/run.py --help` |
| [LRU](lru/README.md) | Independently authored transition model derived from pinned public `lru-rs`; initial candidate and known-omission control kept separate | `lru/run.py --help` |

Run from this repository with the package installed, or set `PYTHONPATH=src`. Local system sources, copied proof code, upstream checkouts and large raw logs are not distributed in these profiles. Supply the matching local checkout and keep generated evidence outside published inputs. Recorded results are measurements, not live test status or claims about arbitrary revisions.

The [agent evidence guide](../../docs/agent-evidence.md) explains how to attach these results to a caller goal without promoting local determinism into top-level system correctness.
