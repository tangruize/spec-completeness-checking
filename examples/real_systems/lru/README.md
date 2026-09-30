# LRU specification experiment

Source: [`jeromefroe/lru-rs`](https://github.com/jeromefroe/lru-rs), v0.18.5, pinned to [`f1e972197053a6814e77b77afacb51f03fc03170`](https://github.com/jeromefroe/lru-rs/tree/f1e972197053a6814e77b77afacb51f03fc03170). The upstream implementation is MIT-licensed, copyright 2016 Jerome Froelich. [Source provenance](source.json) records its file hashes, license, and operation-to-model correspondence; the upstream checkout is ignored, not vendored.

## Scope and preserved intent

This is an **authored abstract contract**, not verification of the original unsafe Rust implementation. It models normally returning `LruCache<u64,u64>` operations over positive capacity, a finite map, and duplicate-free MRU-to-LRU order.

- `put`: replace/promote an existing key and return its old value; otherwise return `None`, even when evicting the LRU.
- `get`: return the hit's value and promote its key; preserve state on a miss.
- `pop`: remove the requested key, returning its old value or `None`, while preserving survivor order.

The unchanged observation includes **the returned Option and the entire post-state: capacity, map, and recency order**. `Cache::View` is `(capacity, entries@, order@)`. Reference identity, allocation, hashing representation, panics, and generic-key representation differences are outside this abstraction.

The genuine [initial candidate](spec/initial.rs), [initial assumptions](initial-assumptions.json), and [SHA-256 seal](initial-seal.json) predate checking. The [compatible encoding](spec/compatible.rs) changes only `seq![key]` to `Seq::<u64>::empty().push(key)`; [native equality proof](proofs/encoding_equivalence.rs) establishes equivalence. No semantic clause was weakened to manufacture a discovery.

**No unexpected semantic omission was found.** The separately labeled [known control](controls/known-control.json) deliberately removes `pop`'s return clause after preserving the initial run. Its supplied witness remains manual evidence. The final checker search independently detects this designed omission after generic search/replay fixes informed by control feedback.

## Latest recorded evidence

[results.json](results.json) is the sole public measurement summary. The final two checker runs use the same stable source fingerprint; the independent native/oracle checks retain their earlier recorded results.

| Evidence | Recorded result |
|---|---|
| Agent-free specdet, complete compatible model | `put`, `get`, `pop`: **3/3 deterministic**, 6.083 s |
| Agent-free specdet, unchanged known control | **Confirmed nondeterministic**, 9/16 attempts, 7.885 s |
| Previously recorded independent native checks | **14 verified, zero errors**: 3 determinism, 9 sampled feasibility, 1 encoding equality, 1 supplied control witness |
| Actual pinned Rust crate versus executable model | **18,060 transitions across 903 states pass** |

The automatic search selects key `0`, an empty capacity-one cache, identical pre/post states, and returns `Some(0)` versus `Some(1)`. `candidate-0008` (zero-based, ninth attempt) receives a **`source_verified_constructive` certificate**, one verified goal, with a 0.746 s native replay. Its [generated witness source](proofs/automatic_control_witness.rs) is preserved byte-for-byte. No witness or seed was supplied and the budget remained 16. The SMT baseline remains **UNKNOWN**, not SAT.

All four frozen source contracts, general goal templates, and full generated equality definitions match preserved prior runs byte-for-byte; [results.json](results.json) records source/equality/proof seals. The Map/Seq invariant and remove/filter replay blocker is resolved for this control. The original `seq!` lowering limitation was not rerun; the three small `blockers/*.toml` profiles retain historical integration reproducers.

The Rust oracle exhausts capacities `{1,2,3}`, keys `{0,1,2,3}`, and values `{0,1,2}` and checks return, capacity, contents, and iteration order. This is finite, hand-transcribed differential evidence—not a refinement proof. The nine feasibility proofs are sampled, not universal feasibility.

## Reproduce

With the checker's Verus Python dependencies installed, run from this directory. The recorded toolchain is Verus `0.2026.05.13.fae8859` with Rust `1.95.0-x86_64-unknown-linux-gnu`; use a native, nonmutating verifier.

```bash
VERUS=/path/to/native/verus
python run.py --verus "$VERUS" --profile compatible.toml --label complete --out runs/repro
python run.py --verus "$VERUS" --profile control.toml --label control --out runs/repro --wall-seconds 120
```

`--out` chooses a base directory under ignored `runs/`. Each invocation creates a unique directory containing `measured.json` and the checker's raw source snapshots, proof attempts, solver logs, and reports. The runner reads report `.payload.results`, caps process logs at 1 MiB, and applies an outer wall timeout. Profiles disable assistance; normal checker execution uses no agent. The original profile is `specdet.toml`; limits remain 16 candidates, eight search rounds, 1,000 ms per SMT query, and 20 seconds per verifier invocation.

Exit codes retain checker meaning: 0 deterministic, 1 confirmed nondeterminism, 2 execution/support error, 3 inconclusive. Do not chain expected nonzero experiments with `&&`; future core improvements may change the recorded outcomes.

The saved automatic witness can also be checked directly, without searching or cloning upstream:

```bash
mkdir -p runs/certificate
"$VERUS" proofs/automatic_control_witness.rs --out-dir runs/certificate
```

The public-source oracle additionally needs the pinned checkout:

```bash
git clone https://github.com/jeromefroe/lru-rs source-checkout
git -C source-checkout checkout --detach f1e972197053a6814e77b77afacb51f03fc03170
python validate.py --verus "$VERUS"
```

`validate.py --native-only` checks the four independent proof programs without cloning upstream; the automatic witness has the separate command above. Full validation checks source/proof hashes and compiles the oracle offline with upstream default features disabled. Runner-only tests: `python -m unittest -q test_runner`.

`generate_checks.py --compatible-run PATH --control-run PATH` regenerates the four independent proof programs from fresh `measured.json` files, checking frozen source hashes and the control's exact missing clause. [Proof provenance](proofs/provenance.json) distinguishes general determinism, sampled feasibility, and the supplied witness; the automatic witness's provenance is in [results.json](results.json).

Historical exports, raw logs, and checker-source inventories are preserved locally under ignored `history/` and `runs/`; they are intentionally absent from a fresh clone. Reproduction regenerates raw artifacts with `--out` rather than relying on unpublished historical paths.
