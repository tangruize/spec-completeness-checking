---
name: specdet-evidence
description: "Use primarily to attack top-level intent contracts and assumed or opaque TCB contracts for underconstrained returns or post-state; use selectively at verified intermediate layers only when the active proof or abstraction boundary depends on output uniqueness or state preservation."
---

# Test one candidate contract for observable underconstraint

[中文](SKILL.zh-CN.md) · [Full usage and terminology](README.md)

Use this skill for one named caller/proof-map question. It is not a mandatory scan for every reachable function and not a specification-completeness oracle.

## Where it has the most value

Prioritize contracts by their role in the proof stack:

| Boundary | Value of this attack | Why |
|---|---|---|
| Top-level intent/goal contract | High | The proof establishes only the written goal; it cannot mechanically establish that the written goal captures Human intent. Attack any result/state freedom that could weaken the intended goal. |
| Assumed, external or opaque TCB contract | High | No verified body independently checks the contract. An omitted result relation or frame condition can leave an intended guarantee absent from the trusted boundary. |
| Verified intermediate contract | Selective | If the implementation satisfies it and the top-level theorem succeeds using it, unmentioned dimensions may be irrelevant to the active goal. Attack it only when it blocks the proof frontier, defines a reused abstraction boundary, hides effects from callers, or is itself consumed as an assumption. |

Do not conclude that intermediate specifications never matter. A proof may bypass a weak contract by unfolding code, while another caller can use only the contract. Conversely, do not spend completeness budget on every internal helper when the active top-level property neither observes nor depends on the omitted dimension.

This attack addresses **underconstraint**: whether the contract permits multiple caller-distinguishable results or post-states. It does not detect an **overstrong or false assumption** whose promised behavior is not implemented. TCB review therefore also needs source/model correspondence, implementation validation where possible, and Human review of trusted assumptions.

## How it helps while writing a specification

A system-proof agent often starts with a provisional contract inferred from the caller, implementation and surrounding invariants. Before investing in implementation proof, this skill attacks one question:

> Does the candidate actually determine the part of the result and post-state that the direct caller relies on?

Use the evidence in the specification-writing loop:

| Writing stage | What the attack can reveal | Agent action |
|---|---|---|
| First candidate | Two caller-distinguishable return values both satisfy the contract | Trace the difference to a caller requirement; draft the smallest justified result relation. |
| Mutable operation | The return is fixed but two caller-distinguishable post-states are allowed | Draft a state relation or frame condition preserving the required state. |
| Proof failure | A verified alternative output exists | Treat the contract, not only the proof, as a revision candidate. |
| Candidate revision | An old witness still works | The revision did not remove that freedom; revise again or record that it is intentional. |
| Candidate revision | The exact old witness is rejected and a complete global uniqueness proof succeeds | Record support that this ambiguity was removed, then continue with feasibility and implementation proof. |

For example, a draft may say only that an allocation succeeds. If the caller requires the returned identifier to name the newly inserted object, two different identifiers satisfying the draft expose a missing caller-result relation. A mutator may constrain its return while leaving unrelated map entries unconstrained; two allowed post-states expose a possible missing frame condition. Conversely, if the caller intentionally accepts either output, preserve the freedom rather than strengthening the contract.

The tool does not write the missing clause by itself. It makes an allowed freedom concrete. The agent must connect the differing output/state dimension to a caller requirement, propose the minimal clause justified by that requirement, replay the witness, and then prove the implementation against the revision.

## When to use

Use `specdet` when at least one of these can change the active caller edge:

- a provisional or revised contract may leave the return value or mutable post-state unconstrained;
- two candidate specifications need a concrete uniqueness distinction;
- a proof failure suggests a missing frame or result relation rather than only a missing lemma;
- an earlier witness should become a regression against a successor candidate;
- a Human question can be compressed to “is this demonstrated freedom intentional?”

Do not use it for a property spanning multiple operations, lifecycle phases, callbacks, concurrency or an interval across `await`; record that as a protocol/invariant obligation instead. Do not run it merely because a function is reachable.

## Required context

Record before invoking:

- active top-level goal, direct caller and exact blocking edge;
- exact target and preserved candidate revision;
- which return/post-state differences matter to that caller;
- native source, sealed extract or authored-model provenance;
- source and dirty-worktree hashes, checker revision and matching Verus profile;
- bounded output directory and budget.

If the intended observations are unknown, stop and resolve that ambiguity first. A convenient policy is not authority for the caller's semantics.

## Procedure

1. **Form the attack question.** State: “For the same admissible input, does this candidate permit two outputs that differ in `<caller-relevant observation>`?” If uniqueness would not help the caller, do not run.
2. **Freeze the candidate and scope.** Use `verus.single_file`, `verus.native` or `verus.cargo` according to the real source context. Never silently replace native source with a model or change clauses to make the tool succeed.
3. **Run one bounded target.**

   ```bash
   /home/ruize/system-proof-agent/spec-completeness-checking/.venv/bin/python -m specdet analyze \
     --config /path/to/specdet.toml --target 'src/component.rs:operation' \
     --verus /path/to/verus --llm-fallback off --offline \
     --run-timeout 60 --out /path/to/evidence --compact-json
   ```

4. **Classify the recorded evidence, not the prose label alone.**

   | Recorded result | System-proof use |
   |---|---|
   | `nondeterministic` with verified witness/SAT evidence | Challenges the candidate on the selected observation. Preserve the two outputs and certificate. It is not automatically a bug. |
   | `deterministic` with complete proof/original UNSAT | Supports uniqueness for this frozen contract and observation relation. It does not establish adequacy, feasibility or implementation correctness. |
   | `inconclusive` / `timed_out` | Does not decide the semantic question. Preserve partial evidence and the exact tool/solver/constructor blocker. |
   | `not_evaluated`, `failed` or `unsupported` | Supplies no determinism conclusion. Correct the input/profile or record the unsupported boundary. |

   Keep `status`, `verdict`, raw `baseline`, `problem_id`, `decisive_evidence`, coverage, exclusions and budgets separate. An UNKNOWN baseline can coexist with a verified constructive witness.
5. **Return to the direct caller.** Classify the proof-map effect as:
   - `challenges candidate`: the demonstrated freedom conflicts with a caller need;
   - `supports uniqueness only`: evidence removes one ambiguity but does not close the caller edge;
   - `does not decide`: the result is conditional, unsupported, timed out or inconclusive.
6. **Choose the next authoritative step.** If the evidence challenges the candidate, identify the differing result/state dimension, trace the caller requirement that rules it out, add only that justified result relation or frame condition, and replay the witness. Otherwise retain intentional freedom, establish input feasibility, prove implementation conformance, investigate a cross-operation invariant, or request bounded Human intent review.

## Handoff

```text
active_goal:
direct_caller / blocking_edge:
candidate_revision + provenance:
attack_question + justified_observation_relation:
status / verdict / raw_baseline:
decisive evidence or blocker:
source_digest / problem_id / run_dir:
ignored dimensions + feasibility/translation boundary:
proof-map effect: challenges candidate | supports uniqueness only | does not decide
next authoritative check:
```

Do not write “spec complete,” “goal closed,” or “bug found” as a tool result. The agent/project authority decides whether the freedom matters and whether the specification should change.

## Stop conditions

Stop after the named caller question has one replayable result, after the budget expires, or when the missing property is outside single-contract determinism. Do not broaden into unrelated targets or repeatedly increase budgets without a new caller-relevant hypothesis.

## Hard limits

- Determinism is only one dimension of underconstraint; a deterministic contract can be wrong, too strong or infeasible.
- “Global” means all admissible modeled inputs for this frozen target and observation relation, not the whole system or unmentioned heap.
- Observation relations and abstractions can hide representation differences; changing them changes the question.
- Native, extract and model evidence have different trust boundaries; model correspondence is separate work.
- Local/refinement UNSAT is not a global proof. A conditional alternative without input feasibility is not an unconditional witness.
- Async contracts, arbitrary opaque ownership/resource construction, some macros and quantifier-heavy problems may remain unsupported or UNKNOWN.
