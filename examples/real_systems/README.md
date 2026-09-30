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

## Tool-native output regression

On 2026-09-30, the same sealed inputs were rerun after adding tool-native compact output and timing. Each row compares human output, `--compact-json` and complete `--json` for the **same new run**. Sizes are UTF-8 bytes, not estimated tokens; times are shared-host observations rather than an isolated speed benchmark.

| Case | Reconfirmed result | Analysis time | Human bytes | Compact JSON bytes | Full JSON bytes |
|---|---|---:|---:|---:|---:|
| LRU complete model | Three deterministic targets | 6.725 s | 2,127 | 12,559 | 62,393 |
| LRU known-omission control | Verified witness at candidate 9/16; baseline UNKNOWN | 8.408 s | 1,698 | 8,046 | 31,178 |
| Nanvix extract | Three deterministic; `alloc` inconclusive | 6.098 s | 3,312 | 18,804 | 262,420 |
| Native mimalloc | Two deterministic; two inconclusive | 74.797 s | 3,341 | 18,790 | 158,538 |
| Native HFS `open_file` | Global query remains UNKNOWN | 97.902 s | 1,357 | 5,827 | 224,315 |

The comparison checked target/source hashes and problem IDs, verdicts and raw baselines, policy/exclusions/trust/feasibility, decisive evidence and witness digests, real used/limit counters, positive target/whole-run durations, and existence/digests of linked artifacts. In particular, Nanvix retains its unsupported `NonNull<u8>` constructor and **0/8 attempts**, rather than claiming budget exhaustion. Repeated recovery warnings are bounded without suppressing distinct blockers. Synthetic tests also cover huge logs/observations/witnesses, local UNSAT, untrusted translations, old metadata, failures and interruptions.

The HFS row reruns the normal global analysis only; it does not rerun or promote the separate conditional error-alternative probe. The LRU control remains a deliberate omission, not an unexpected discovery. Native/extract/model scope and source-publication boundaries remain unchanged. Human byte counts include the separately bounded project diagnostics on stderr; both JSON modes put recorded diagnostics in stdout.
