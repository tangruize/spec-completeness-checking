# Real source-level incompleteness regressions

These are full end-to-end tests of three genuine VeruSAGE executable contracts,
not artificial specifications or manually injected SAT solver responses:

| Original source | Actual contract gap |
|---|---|
| `vest/verified/utils/utils__init_vec_u8.rs` | Only output length is specified; elements remain free. |
| `memory-allocator/verified/commit_mask/commit_mask__impl__next_run.rs` | Zero-length runs at different start indices are permitted; first/maximal-run clauses are commented out. |
| `atmosphere/verified/array/array_set__impl0__new.rs`, `Array::new` | `wf()` only fixes ghost sequence length, not its contents. |

Complete source copies and original SHA256 values are recorded in
`tests/fixtures/incomplete/manifest.json`. The Array file has one explicitly
declared Verus legacy-mutable-postcondition compatibility annotation; removing
that prefix reproduces the original bytes. No requires, ensures, View, function
body or source-defined axiom is changed.

```bash
specdet analyze --config examples/incompleteness/specdet.toml \
  --verus /path/to/non-mutating/verus
```

The expected analysis exit code is **1**, because these contracts are
nondeterministic. Inputs and both outputs come from a bounded source-type
constructor catalog; the tests do not supply expected output pairs.

The recorded end-to-end result is in [`results.json`](results.json): all three
functions are `nondeterministic` with verified source certificates, while their
original full SMT queries remain explicitly `unknown`.

Confirmation requires a fresh source harness with no candidate preconditions:

```text
assert P(input)
assert Q(input, output1) && Q(input, output2)
assert !E_out(output1, output2)
```

The actual verifier must establish every assertion, including output
inequality. Constructor facts and constant-evaluation hints are themselves
checked, not assumed. Generic choices are explicit and checked against the
original generic/where bounds. Equal-output and wrong-length controls fail.

## What “confirmed” means

This uses the source-certificate acceptance criterion selected for this task:

```text
semantic verdict:       nondeterministic
counterexample kind:    source_verified_constructive
counterexample status:  verified
original SMT status:   preserved independently (typically unknown)
```

The full Verus SMT includes boxing inverses which require infinite `Poly` and
`Type` domains. Bounded experiments preserving every original assertion did
not produce a raw full-VC SAT model, even with concrete witness constraints.
The source certificate is **not** relabeled as a raw SMT SAT result. Neither a
toy Boolean formula nor a reduced `ValEq`/finite projection is used as evidence
for the original SMT query.

## Regression commands

```bash
SPECDET_VERUS=/path/to/rust_verify \
SPECDET_RUST_TOOLCHAIN=YOUR_INSTALLED_RUST_TOOLCHAIN \
python -m unittest discover -s tests -p 'test_real_incomplete_sources.py'
```

The test requires the public analysis API to return `nondeterministic`, a
verified concrete witness, a matching original problem identity, and a
source-bound certificate. UNKNOWN without that certificate cannot pass.
`test_source_witness_replay.py` additionally checks rejection of invalid
candidates.

The certificate implementation was reviewed and hardened before delivery:
constructor text must parse as exactly one type expression and cannot introduce
statements or assumptions; generic replay with source-relative `self::` or
`super::` paths is rejected until namespace-preserving specialization is
available; exhausted mechanical catalogs can request candidate assistance only
while verification budget remains. The complete post-fix suite passes 389
tests, including actual source replays and invalid-certificate regressions.
