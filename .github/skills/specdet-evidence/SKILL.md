---
name: specdet-evidence
description: "Use when a system-proof caller edge may depend on whether one Verus contract uniquely determines an observable return or post-state; run bounded source-linked determinism evidence without deciding specification adequacy."
---

# Collect caller-oriented contract determinism evidence

Use this skill only when output uniqueness can change one named caller/proof-map edge: attacking a provisional contract, comparing revisions, diagnosing a possible missing frame/result relation, or replaying an earlier witness. Do not run it merely because a function is reachable. Cross-operation, lifecycle, concurrency and `await`-spanning properties belong in protocol/invariant analysis.

Read [the evidence guide](../../../docs/agent-evidence.md) for invocation, terminology, result codes and artifact layout.

1. Record the active goal, direct caller, blocking edge, preserved candidate revision, caller-relevant observation and native/extract/model provenance. If the observation is ambiguous, stop.
2. Run one exact target using the real native/Cargo context or an explicitly labeled extract/model; keep source snapshots and outputs separate.
3. Inspect execution status separately from semantic verdict. Preserve the raw baseline, problem ID, decisive proof/witness, coverage, ignored dimensions, feasibility/translation boundary and budgets.
4. Return one proof-map effect: `challenges candidate`, `supports uniqueness only`, or `does not decide`. Then name the next authoritative check.
5. Stop after one replayable caller-relevant result or the budget. Do not broaden into unrelated targets without a new caller hypothesis.

`nondeterministic` challenges the candidate only under the selected observation and is not automatically a bug. `deterministic` supports uniqueness only; it does not establish adequacy, feasibility, termination or implementation correctness. UNKNOWN, timeout, unsupported construction and local UNSAT do not close the global question. A verified witness may coexist with an UNKNOWN baseline.

The tool reports elapsed time itself. Its default whole-analysis budget is 60 seconds; `--run-timeout` overrides it, while `--timeout` remains per verifier call. Preserve `timed_out`, completed evidence and `pending_targets` separately. Reading an existing report shows its original analysis duration and semantic exit code, while successful reading itself exits `0`. A concrete witness and an UNKNOWN baseline can coexist; local UNSAT alone does not close a global question.
