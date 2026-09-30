---
name: specdet-evidence
description: "Use primarily to attack top-level intent contracts and assumed or opaque TCB contracts for underconstrained returns or post-state; use selectively at verified intermediate layers when the active proof depends on output uniqueness or state preservation."
---

# Collect caller-oriented contract determinism evidence

Prioritize top-level intent/goal contracts, whose proof cannot establish that the statement captures Human intent, and assumed/external/opaque TCB contracts, whose behavior is not constrained by a verified body. Use it selectively for verified intermediate contracts: attack them when they block the proof frontier, form a reused abstraction boundary, hide caller-visible effects, or are consumed as assumptions. If the implementation satisfies an intermediate contract and the top-level theorem succeeds using it, omitted dimensions may be irrelevant to the active goal; do not scan every internal helper.

This attack finds underconstraint, not false or overstrong assumptions. TCB review also needs source/model correspondence, implementation validation where possible and Human review of trusted assumptions. Cross-operation, lifecycle, concurrency and `await`-spanning properties belong in protocol/invariant analysis.

During specification writing, ask whether the candidate determines the result and post-state that the direct caller relies on. Two permitted caller-distinguishable returns suggest a missing result relation; a fixed return with two permitted caller-distinguishable post-states suggests a missing state relation or frame condition. Trace the difference to an actual caller requirement, add only the smallest clause justified by that requirement, replay the witness, and then prove implementation conformance. If the caller intentionally accepts both outcomes, preserve the freedom instead of strengthening the contract.

Read [the evidence guide](../../../docs/agent-evidence.md) for invocation, terminology, result codes and artifact layout.

1. Record the active goal, direct caller, blocking edge, preserved candidate revision, caller-relevant observation and native/extract/model provenance. If the observation is ambiguous, stop.
2. Run one exact target using the real native/Cargo context or an explicitly labeled extract/model; keep source snapshots and outputs separate.
3. Inspect execution status separately from semantic verdict. Preserve the raw baseline, problem ID, decisive proof/witness, coverage, ignored dimensions, feasibility/translation boundary and budgets.
4. Return one proof-map effect: `challenges candidate`, `supports uniqueness only`, or `does not decide`. Then name the next authoritative check.
5. Stop after one replayable caller-relevant result or the budget. Do not broaden into unrelated targets without a new caller hypothesis.

`nondeterministic` challenges the candidate only under the selected observation and is not automatically a bug. `deterministic` supports uniqueness only; it does not establish adequacy, feasibility, termination or implementation correctness. UNKNOWN, timeout, unsupported construction and local UNSAT do not close the global question. A verified witness may coexist with an UNKNOWN baseline.

The tool reports elapsed time itself. Its default whole-analysis budget is 60 seconds; `--run-timeout` overrides it, while `--timeout` remains per verifier call. Preserve `timed_out`, completed evidence and `pending_targets` separately. Reading an existing report shows its original analysis duration and semantic exit code, while successful reading itself exits `0`. A concrete witness and an UNKNOWN baseline can coexist; local UNSAT alone does not close a global question.
