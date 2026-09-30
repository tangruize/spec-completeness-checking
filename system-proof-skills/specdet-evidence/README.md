# Specdet evidence for a system proof agent

[中文](README.zh-CN.md) · [English skill](SKILL.md) · [中文 skill](SKILL.zh-CN.md)

Load one skill language, not both. This package documents a local tool invocation; it adds no agent adapter, workflow or specification-quality classifier.

## Purpose and terms

`specdet` tests whether the selected contract can permit two observably different outputs for the same input:

```text
P(input) && Q(input, output1) && Q(input, output2) && !E(output1, output2)
```

`P` is the precondition, `Q` the postcondition, and `E` the chosen output-equivalence relation. Outputs include returns and final mutable-parameter states. This is evidence about **contract determinism**, not a test that all requirements or the system's top-level goal have been captured.

| Term | Meaning |
|---|---|
| Global | All admissible modeled inputs for this selected contract and observation relation; not the entire system or unmentioned heap. |
| Observation relation | The caller-justified equivalence used to compare outputs and post-state. It must state which dimensions are compared, abstracted or ignored. |
| Concrete / abstract determinism | The default fixes concrete inputs. Optional abstract analysis compares inputs with equal selected Views; this is a different question and normally requires a concrete-determinism prerequisite. |
| Witness / counterexample | One input and two distinct outputs that satisfy the original contract. Nondeterminism may be intentional. |
| Source-verified constructive witness | Verus checks concrete constructors, original conditions and output distinctness. This is a certificate, not a fabricated SMT SAT result. |
| Local slice | An additional input restriction. Slice UNSAT does not establish global determinism and may be vacuous if the slice is infeasible. |
| Conditional alternative | A proof assuming a valid input without constructing one. Without feasibility evidence, it is not an unconditional counterexample. |
| Frame condition | A clause specifying what state remains unchanged. A fixed return does not establish an unchanged heap. |
| UNKNOWN / inconclusive | The available proof, translation and search did not decide the question. It is neither a defect report nor a completeness certificate. |

No built-in comparison profile is a universally correct notion of “same output.” Before interpreting a result, derive the observation relation from the direct caller or system property and record every ignored dimension. A profile name is only tool configuration, not a semantic claim suitable for a proof map. Changing the relation changes the question, so evidence obtained under one relation must not be presented as evidence for another.

## Where it helps the system proof agent

The system-proof workflow needs both **specification judgment** and **proof-map-guided reintegration**. `specdet` contributes one bounded evidence source to that loop:

```text
candidate contract
→ determinism attack
→ proof/witness or explicit blocker
→ caller/proof-map reintegration
→ retain, revise or reject the candidate
```

Its useful roles are:

- **Attack provisional specifications:** produce a replayable pair of contract-permitted outputs when a result or frame may be missing.
- **Discriminate candidates:** show that one revision still permits a caller-relevant freedom, or that another has uniqueness support under the same observation relation.
- **Turn proof feedback into a spec question:** separate “the proof needs a lemma” from “the contract itself permits multiple results.”
- **Make local evidence auditable:** bind the claim to source hashes, `problem_id`, the justified observation relation, proof/witness certificate and budgets, then return it to the direct caller rather than counting a local result as top-level closure.
- **Support autonomous iteration:** preserve failed attempts, timeouts and old witnesses so the agent can revise and replay before asking a Human.
- **Compress Human review:** when mechanical evidence is decisive, the remaining question can become “is this freedom intentional for the caller?” rather than an unstructured request to review the whole specification.

This tool does not choose the candidate, decide intent or update the proof map by itself. The agent uses its output together with implementation proof, tests, source analysis, protocol invariants and bounded Human review. Because skills can also bias planning or add overhead, invoke this skill only for a named caller-relevant uniqueness question; skill-on/off ablation remains useful in experiments.

## Potential limitations

| Limitation | Consequence for the agent |
|---|---|
| Determinism is narrower than adequacy | A unique output can still be the wrong behavior, omit required success, or rely on an infeasible precondition. |
| The result is observation-relative | A coarse relation may merge differences the caller cares about; an overly concrete relation may expose representation freedom that the system intentionally abstracts. |
| One contract is not a protocol | Cross-operation preservation, lifecycle, concurrency, callbacks, failure compensation and `await`-spanning properties need other analysis. |
| Contract evidence is not implementation conformance | A deterministic postcondition does not prove the implementation satisfies it; a native implementation verification is separate evidence. |
| Source, extract and model have different trust | A sealed extract records omissions/stubs; an authored model needs an independent source-to-model correspondence argument. |
| Solver/search coverage is incomplete | UNKNOWN, timeout, unsupported constructors or a finite candidate budget do not imply determinism or absence of witnesses. |
| Feasibility and termination are normally unchecked | Global UNSAT can be vacuous if no valid input exists; a conditional alternative is not an unconditional counterexample. |
| Language/backend support is bounded | Async contracts, opaque ownership/resources, some macros and quantifier-heavy goals can remain unsupported or inconclusive. |

## Setup

Use the [maintained fork](https://github.com/tangruize/spec-completeness-checking/tree/improve-real-system-evidence), branch `improve-real-system-evidence`. Installation requires Python 3.11+, Git and a C compiler for the pinned grammar, plus a separately installed, project-compatible Verus toolchain.

These instructions match commit [`3585e71`](https://github.com/tangruize/spec-completeness-checking/commit/3585e713ac1bbfd6eca53cdfa6118d93fac2b452), including compact evidence output and the default whole-run deadline.

```bash
cd /path/to/spec-completeness-checking
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[verus]'
.venv/bin/python -m specdet doctor --verus /path/to/verus
git rev-parse HEAD
```

In this workspace, the maintained checkout is `/home/ruize/system-proof-agent/spec-completeness-checking`; `tools/spec-completeness-checking` is a separate older copy. Use the maintained checkout's `.venv/bin/python`, or install the fork in your own environment.

## Run

Start from a matching [project profile](https://github.com/tangruize/spec-completeness-checking/tree/improve-real-system-evidence/examples/real_systems). A self-contained file uses `verus.single_file`; a crate needs `verus.native` with its entrypoint, or `verus.cargo` with its real package/features/injection file. Profiles must also preserve dependencies and the matching Rust/Verus configuration.

```bash
.venv/bin/python -m specdet analyze \
  --config /path/to/specdet.toml --target 'src/component.rs:operation' \
  --verus /path/to/verus --llm-fallback off --offline \
  --run-timeout 60 --timeout 30 --solver-timeout-ms 1000 \
  --max-rounds 8 --counterexample-candidates 16 \
  --out /path/to/evidence --compact-json
```

Config-relative paths resolve against the config file. Keep outputs outside source inputs. Target include patterns select declarations, not the entire snapshot footprint: exclude build products, old runs and unrelated trees; do not use a session-artifacts directory as the project root.

The **default whole-analysis budget is 60 seconds**, shared across preparation and all targets. Override with `--run-timeout 180` or `[limits] run_timeout_seconds = 180`. `--timeout` remains per verifier call. Expiry stops owned processes and saves partial evidence; cleanup can add a short tail to the measured time. Cancellation currently requires POSIX main-thread execution; invoke the CLI from other threads. Separate profile-runner probes or implementation checks are not automatically part of `analyze()`'s budget.

Capture both stdout JSON and the process exit code; do not discard valid evidence merely because the exit code is nonzero.

| Exit | Meaning |
|---|---|
| `0` | All applicable analyzed targets are deterministic. |
| `1` | Confirmed nondeterminism is present. |
| `2` | Configuration, execution or support failure. |
| `3` | Unresolved results, including a whole-run timeout. |
| `130` | Interrupted invocation. |

Execution failures take precedence over semantic results; retained nondeterminism can still produce `1` on a timed-out run. Always read `status` and each target's `verdict`.

## Read and retain evidence

`--compact-json` emits `format = "specdet.summary.v1"`. Long values and extra diagnostics have explicit omission/truncation markers; complete artifacts are not truncated.

| Field | Evidence supplied |
|---|---|
| `target.source_digest`, `problem_id` | Source identity and the frozen contract/observation question. |
| `status`, `verdict`, `baseline` | Execution state, semantic result and unchanged original query outcome/reason. |
| `decisive_evidence`, `counterexample` | Proof/query basis or a verified witness, bindings, digests and certificate links. UNKNOWN baseline plus a verified constructive witness is valid. |
| `coverage` | Policy, ignored dimensions, translation trust and feasibility/heap boundaries. `trusted_translation` does not prove that an authored model matches the original system. |
| `resources`, `duration_ms`, `run_timeout_seconds` | Usage, limits and actual elapsed time. Interrupted batch counts can be `used = null` with `known_used` as a lower bound. |
| `diagnostics`, `artifacts`, `full_report`, `pending_targets` | Blockers, detailed evidence locations, complete report and selected targets not started before timeout. |

A missing opaque-pointer constructor is not exhausted witness search: Nanvix's `NonNull<u8>` case reports zero candidates attempted. Distinguish parser/support failures, per-call timeout, whole-run expiry and solver UNKNOWN. A null baseline means no recorded baseline query, not a solver UNKNOWN result.

```bash
.venv/bin/python -m specdet report --run /path/to/RUN_ID --compact-json
.venv/bin/python -m specdet report --run /path/to/RUN_ID --json
.venv/bin/python -m specdet replay --run /path/to/RUN_ID --reexecute \
  --verus /path/to/verus --run-timeout 180 --out /path/to/new-evidence
```

Report reading returns process code `0` on success but retains the original semantic code and duration. On-disk JSON artifacts use a digest-checked envelope: read `payload`. Proof links may name an attempt directory containing `check.json`; witness links name the replay certificate. Reexecution starts again from the frozen snapshot; it does not resume an interrupted stage. If preparation ended before a complete snapshot was recorded, rerun from the original profile instead. `report/replay` consume normal analyzer runs, not custom profile-probe summaries.

## Handoff and boundaries

The caller supplies the caller-goal/proof-map edge, source revision plus dirty-worktree hashes, checker revision, and **native / sealed-extract / authored-model** provenance. Attach the tool's compact result and artifact links to that record. The checker does not discover caller intent or decide which proof-map edge is closed.

Native contract evidence is not implementation verification. Extracts must record dependency/body substitutions; models need a separate correspondence argument, and finite conformance tests are not refinement proofs. Feasibility, termination, unmentioned heap and higher-level adequacy remain outside the conclusion unless separately established. Async contracts, arbitrary opaque ownership construction and some macros remain unsupported.

Keep the original candidate, all semantic/observation changes and source assumptions visible. Whether a freedom is acceptable or a missing requirement, and whether to strengthen a specification, are decisions for the agent/project—not this tool or skill. Raw snapshots can contain private source; keep them local rather than publishing them with the compact record.
