# Native mimalloc commit-mask evidence

This profile checks **four original methods in `/home/ruize/mimalloc-argus-run/src/commit_mask.rs` through the real `src/lib.rs` crate**, not a projected contract or an extracted substitute. All 32 source files are snapshotted byte-for-byte. No implementation, precondition, postcondition, body stub, wrapper normalization, or observation override was introduced. Only the checker’s synthetic uniqueness goal is injected into its isolated copy.

## Measured results

The source repository HEAD was `b0445a794f78802e065629af472a26b7d279d161`, with existing working-tree changes. **HEAD alone does not identify these inputs.** [`source-seal.json`](source-seal.json) records every source SHA256, exact selected declaration byte/line ranges, and original clause counts/digests, without publishing contract text. In particular, `commit_mask.rs` is `27b752574d51f1620f925cf573ef810d4fa0c6b7d5bf623be226f2f6ae288dbd`.

| Original method | Original lines | Verdict | Raw baseline | Verus process |
|---|---:|---|---|---:|
| `CommitMask::clear` | 127–161 | deterministic | UNSAT; 1 verified goal | 11.426 s |
| `CommitMask::set` | 163–199 | deterministic | UNSAT; 1 verified goal | 11.845 s |
| `CommitMask::create` | 201–286 | inconclusive | UNKNOWN: incomplete quantifiers | 9.732 s |
| `CommitMask::next_run` | 335–532 | inconclusive | UNKNOWN: incomplete quantifiers | 9.462 s |

The final four-target integration run with the current project `.venv` took **66.912 s**, with semantic exit code **3**, not a pass. Independent native verification of the unchanged `commit_mask` module took **8.889 s: 38 verified, 0 errors**. That is partial module verification, not verification of the whole allocator or completion of its TOP-level obligations. All original clause counts/digests, complete output-observation templates, all four frozen goal IDs, and all four baseline query digests exactly match the preceding sealed run.

Both the target source and common core remained unchanged during the final run: `original_sources_unchanged = true` and `checker_core_unchanged = true`. All 32 source hashes were checked before and after execution; only validation metadata was added to the source seal, never replacement source hashes. The earlier repair run’s concurrent-edit caveat remains attached to its historical evidence, not this final run.

Native verification used the ELF `rust_verify`, **Verus 0.2026.09.18.c22087b**, Rust **1.98.1**, and the project’s **Z3 4.16.0**. The checker’s separate Python SMT queries used **Z3 5.1.0** from the project `.venv` under **Python 3.12.3**, with `tree-sitter-verus` **0.23.2** installed from public grammar commit `8639c4e4196ce36dd8a40c57fbad6200735bd2d1`. The existing compiler-matched `libc` artifact was reused; this run performed no target build or dependency installation. [`evidence.json`](evidence.json) contains environment and tool identities, tested checker-core hashes, actual commands, proof counts, timings, raw query outcomes, and paths/digests for persistent detailed artifacts.

## Reproduce

From the checker repository root, using the matching dirty source tree and its existing native dependency:

```bash
TOOLS=/home/ruize/.cache/argus-verus-c22087b/source
TARGET=/home/ruize/mimalloc-argus-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" \
.venv/bin/python examples/real_systems/mimalloc/run.py \
  --source-root "$TARGET" \
  --source-seal examples/real_systems/mimalloc/source-seal.json \
  --verus "$TOOLS/target-verus/release/rust_verify" \
  --rust-toolchain 1.98.1 \
  --z3 "$TOOLS/z3" \
  --libc "$TARGET/.build/verifier/debug/deps/liblibc-9cd8a66bfd45f04e.rlib" \
  --verify-implementation \
  --out examples/real_systems/mimalloc/runs/native-rerun
```

The output directory must be new. `--function clear` (repeatable) narrows the run. The runner uses the real API exit code and reads persisted `summary.json` **`.payload.results`**. Per-verifier timeout is 30 seconds, per-SMT-query timeout 1,000 ms, search budget four queries, maximum proof attempts two, and constructive counterexample candidates zero. Assistance is off and execution is offline. Native implementation verification is an optional separate check.

Omit `--source-seal` when intentionally evaluating another source revision; the new run’s `inputs.json` freezes that working tree’s hashes, and its results apply only to those inputs.

The fork should contain only this runner, documentation, and hash-only input/result summaries. Run outputs include private original source snapshots and must stay in the gitignored `runs/` directory or an external local artifact directory; do not force-add them.

## Interpretation and remaining limitations

`clear` specifies exact wordwise difference; `set` specifies exact union, including coverage properties. Their output equivalence observes **all eight mutated mask words**, not merely the unit return value. Both complete original goals verified without narrowing.

`create` specifies every bit of a requested interval. `next_run` uses the current `commit_mask_run` predicate, which specifies the first maximal run and the no-run sentinel `(512, 0)`. The checker README’s older upstream witness `(0, 0)` versus `(1, 0)` is **not valid for this working source**. Neither inconclusive result is a counterexample or evidence of a specification bug.

### Fixed-array refinement repair

Previously, the bounded search spent all three refinement queries on impossible mask lengths 0, 1, and 2, despite the native field being `[usize; 8]`. The generic repair is confined to `native/extract/narrow.py` and the small AST-based `native/extract/array_bounds.py` helper; schema rendering and goal generation are unchanged.

Known literal array extents now go directly to valid element slots, capped at the existing eight precompiled slots. Empty arrays generate no element probes. Symbolic or unevaluated constant extents are skipped with a diagnostic instead of guessed. Variable-length `Seq`, slices, and `Vec` Views retain their original length search.

The source-preserved rerun replaces all six impossible length queries with first-word bounds `[0,16]`, `[0,8]`, and `[0,4]` for the two unresolved methods. Those queries still return **UNKNOWN**, not counterexamples or determinism proofs. For `create`, its zero-mask precondition already implies these bounds; no claim of additional semantic coverage is made. Better proof automation and constraint-aware scheduling remain useful.

The original repair comparison took 105.225 s before versus 95.772 s after; these are historical shared-host observations, not an isolated performance benchmark. Exact before/after queries are in `evidence.json`, and the final `.venv` run reconfirms the same element scheduling without increasing any budget. At the repair step, validation passed **51 focused tests, 35 parameterized subtests**, and the existing narrowing self-tests, covering fixed/zero/large/symbolic extents, nested array type syntax, unchanged dynamic length search, and the four-query budget.

The initial native attempt exposed merged comma-separated quantified attribute clauses (`clear`: 2 rather than 3 postconditions; `set`: 2 rather than 4; `create`: 2 rather than 3 preconditions). The parent’s common-core parser repair resolved this before the final run; the source was not rewritten to work around it. Unpinned Z3 initially selected 4.12.5; the explicit solver setting above resolves that independent toolchain problem.

Discovery still reports partial coverage for unrelated source macros/attributes. The selected original clause counts and frozen goals were inspected. Feasibility is not independently checked by the completeness checker. These are concrete shared-input bitset-utility results, **not** a claim that allocator address placement must be deterministic or that tracked-resource abstractions are defective.
