# Current Nanvix bitmap contracts

This case checks four real contracts from a local Nanvix checkout based on commit `1bc5bdb9b3528997264811fea59235a41f2af840` with recorded working-tree changes. The published `source-selection.json` contains only the reviewed source-file hashes and revision. The runner generates `inputs/provenance.json` locally with copied byte ranges, original target headers, and transformations; it does not assume the working tree equals the base commit or upstream Nanvix.

## Publication boundary

The runner, profile, tests, hash metadata, and measured summary may be published. **Nanvix source bytes, original contract headers, extracted implementations, and verifier source snapshots are not bundled.** `inputs/` and `results/` are gitignored. The runner requires `--source-root` and constructs its dependency-closed extract from that read-only local checkout. A different source hash fails closed because the extraction spans require review; no checkout is downloaded or silently replaced with a simplified model.

## Scope and proof boundary

The generated `inputs/sealed/` is a dependency-closed **contract extract**, not a native Nanvix build or an invented bitmap model. It retains the exact `Bitmap` fields, `BitmapView`, View definition, invariants, relevant specification functions, `RawArray` representation and original uninterpreted View, original opaque storage specification, error types, and errno constants. Type declarations are co-located using import/module scaffolding. No target precondition or postcondition is weakened, strengthened, or rewritten.

The getter bodies are unchanged. Only `alloc` and `set` bodies are replaced with explicitly annotated `external_body` stubs. Unselected implementation methods, unused proof lemmas, tests, and build metadata are omitted. The checker reasons about the source contracts, not those stubs. Direct verification of this extract reported **2 verified, 0 errors** for the retained executable bodies; this is not verification of the two mutator implementations.

The observation policy is `verus-observable-v1`: getter return values are observed exactly; mutation checks observe the result variant, successful payload, and final `BitmapView`. Error payloads and bitmap representation beyond its View are explicitly ignored. Global/unmentioned heap, contract feasibility, termination, and allocation liveness are not established. Determinism is not a specification-quality or agent-quality verdict; nondeterminism would not automatically be a bug.

## Run

From the checker repository root, with its Python dependencies installed in `.venv`:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python examples/real_systems/nanvix/run.py \
  --source-root /path/to/nanvix-checkout \
  --verus /path/to/verus/rust_verify \
  --rust-toolchain 1.95.0 \
  --out examples/real_systems/nanvix/results
```

Use the native non-mutating verifier, not a source-rewriting launcher. The runner supplies the Rust runtime toolchain through the checker. Change the paths/version for your installation. `--source-root` is required; all original source hashes are checked before and after analysis. Add `--target set` to select one function. No LLM or network access is used. Evidence written inside the checker checkout must be gitignored; evidence must not be written into the Nanvix source repository.

`specdet.toml` also works with the normal checker CLI after generating the local inputs and configuring the verifier and Rust toolchain. It has an explicit small project root and only the four selected targets. `run.py` records extraction time, checker wall time, tool/source hashes, whether checker code changed during execution, observations, raw statuses, and evidence paths in each run's `measured-result.json`. Full verifier logs, generated obligations, snapshots, solver queries, and reports remain local in that run directory.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover \
  -s examples/real_systems/nanvix -p test_case.py
```

The tests check the publication boundary and, when local inputs exist, the seal and two initially encountered extraction failures using the real clauses: `usage`'s comma-separated cast comparisons must remain separate expressions, and `set`'s nested `match` must remain one complete expression. The source-dependent checks explicitly skip if local inputs have not been generated; they do not silently use an invented fixture.

## Measured evidence

The final integration run used the checkout's `.venv/bin/python`, native Verus `0.2026.05.13.fae8859`, Rust `1.95.0`, a 30-second verifier timeout, a 1-second solver timeout, four search rounds, and at most eight counterexample candidates. Local input generation took **0.014 seconds** and checker wall time was **6.491 seconds**; checker exit code **3** preserves the unresolved target. Checker source hashes, the profile, extraction code, and source-selection metadata were unchanged throughout this run.

| Target | Execution | Verdict | Evidence |
|---|---|---|---|
| `Bitmap::number_of_bits` | completed | deterministic | Original baseline UNSAT; one verified determinism goal |
| `Bitmap::usage` | completed | deterministic | Original baseline UNSAT; one verified determinism goal |
| `Bitmap::alloc` | completed | inconclusive | Original baseline UNKNOWN (`incomplete quantifiers`); no verified counterexample |
| `Bitmap::set` | completed | deterministic | Original baseline UNSAT; one verified determinism goal |

These are **three decisive contract results out of four selected targets**, not four successes. `alloc` is neither certified nondeterministic nor a reported specification bug. Repeating bounded search is not a substitute for evidence.

For `alloc`, `counterexample-search.json` records **zero candidate attempts** and `unsupported_witness: No concrete constructor catalog for ptr::NonNull<u8> (unknown)`. Search also rejects capacity-range and set-emptiness constraints on the input bitmap View as uncompiled observation dimensions. Missing constructor support, not candidate-budget exhaustion, is the immediate witness blocker. The original opaque `RawArrayStorage` and uninterpreted `RawArray::view` must not be replaced with an invented sequence model to manufacture a source-level witness.

The final-source check repeated the unchanged four-target profile once. All four semantic verdicts and the opaque-pointer constructor blocker match the preceding **17.777-second** run. The earlier **8.170-second** run had the same verdicts but lacked mutable-poststate enumeration. These are individual bounded-run timings, not a performance benchmark; none establishes allocator nondeterminism.

A separate narrow native-source probe took **2.621 seconds** and failed before proof checking because the standalone profile did not provide the `raw_array` and `sys` crates. That is an incomplete native dependency profile, not proof that the Nanvix project fails to verify. The source-sealed extract resolves that experiment's dependency boundary without claiming to discharge the residual native Cargo build or mutator implementation proofs.

`measured-results.json` contains compact measurements and exact retained evidence locations, not source code or proof bodies. The full private artifacts are retained under the session's `files/nanvix-evidence/` directory. To generate local inputs without running the checker, use `seal.py --source-root /path/to/nanvix --write`; subsequent `seal.py --source-root /path/to/nanvix` checks them. Running the source-bound analysis requires access to the hash-matching checkout. Adapting this profile to changed source requires reviewing the extraction spans and updating `source-selection.json`, not bypassing the hash check.
