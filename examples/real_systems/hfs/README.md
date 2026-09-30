# HFS v3: native contract evidence

This profile reads a local HFS v3 checkout and leaves it unchanged. It uses the actual Cargo crate, its pinned verifier, `Volume::open_file`, and every extracted pre/postcondition. It does not replace `Volume` or its invariant with a scalar toy model. Source copies and logs are written only to the requested evidence directory.

```bash
python examples/real_systems/hfs/run.py \
  --source-root /path/to/hfs-v3 --out /path/to/hfs-evidence --timeout 120
```

The profile produces two distinct results:

| Question | Recorded outcome |
|---|---|
| Under `verus-observable-v1`, does the original contract determine the returned success handle's public slot and final Volume View? | Original obligation unproved; SMT baseline UNKNOWN, `incomplete quantifiers`. No global determinism or counterexample verdict. |
| For any input satisfying the original precondition, do two different errors with unchanged receiver each satisfy every original postcondition? | One native proof verified, for `NotMounted` versus `ReadOnly`. This is **conditional**, not a constructed feasible input or unconditional nondeterminism witness. |

The second probe is deliberately supplied by the experiment, not automatically discovered by constructor search. It demonstrates an admissible all-error behavior under the original precondition. Whether the API should exclude it depends on caller requirements and environmental failure assumptions.

The observations differ: the generic baseline merges `Err` payloads and ignores representation beyond `Volume::view`; the conditional probe distinguishes error discriminants. Neither analysis establishes relational equivalence of a returned handle's file contents through its final Volume. The probe retains all original clauses by transforming the frozen obligation, not by copying only its error arm.

## Recorded measurement

Source revision: `2edf31f1` on the local v3 branch. `libhfs/src/volume.rs` SHA-256: `6c56e26c11f89b695b8209e4aa860a0748375b2cccfb6b0a9d5222b72ed069ab`. Verifier: `0.2026.07.27.31579f0`, Rust `1.97.1`.

The final source-qualified run took **157.994 s**: snapshot 5.225 s; preparation/lowering, including snapshot, 27.316 s; conditional probe including worktree preparation 68.974 s; baseline 54.696 s. The original SMT query itself returned UNKNOWN in approximately 0.884 s. Do not attribute all wall time to the solver or advertise this native profile as a seconds-only check. Derived-analysis caching reduced repeated preprocessing, but the different runs are not controlled total-runtime benchmarks.

Conditional probe SHA-256: `cdc6790c64433a7c6e386c86a19b51f1d9466702b8891c4aef9dc46056ea22aa`. Frozen determinism problem: `a9d7c68f22b3560ab1592d51b64575d13342b905ccb40525d9af67b24f3715bb`. The separate 1,902 dependency goals printed by Cargo are not counted as proofs of this HFS target.

`summary.json` records the baseline, conditional certificate and phase timings; `contract.json`, `observations.json` and `obligation.json` preserve the exact question. `conditional-probe/evidence.json` records `pre_feasibility = not_established` and `nondeterminism_verdict = not_claimed`. The runner returns exit 3 for this bounded inconclusive outcome, not a success-shaped zero.
