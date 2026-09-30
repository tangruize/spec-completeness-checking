# Specification evidence for a system proof agent

Use `specdet` as a bounded evidence producer attached to a candidate contract and a caller goal. It runs without an agent or LLM; assistance is optional and disabled by default. It does not decide whether a specification is good, whether nondeterminism is intentional, or whether the system's top-level goal has been proved.

## The question it answers

For fixed inputs, can the original precondition `P` and postcondition `Q` admit two observably different outputs?

```text
P(input) && Q(input, output1) && Q(input, output2) && !E(output1, output2)
```

Outputs include the return value and final values of mutable parameters. `E` is the explicit observation relation, not necessarily equality of every implementation bit.

| Term | Meaning and boundary |
|---|---|
| Observational determinism | All contract-permitted outputs agree under `E`. This does not establish intent, correctness, termination, or that a valid input exists. |
| View | A source-defined abstraction of a value. Equality through a View leaves representation outside that View unexamined. |
| Witness / counterexample | One input and two output states, with evidence that both satisfy the original contract and differ under `E`. |
| Source-verified constructive witness | Verus checks concrete constructors and the original conditions. This is a proof certificate, not a claim that the original SMT query returned SAT. |
| Conditional alternative | A proof assuming a valid input, rather than constructing one. Record input feasibility separately; it is not an unconditional counterexample. |
| UNKNOWN / inconclusive | No decision within the current translation, proof and search limits. It is neither a counterexample nor evidence that the spec is complete. |
| Frame condition | A postcondition specifying which state remains unchanged. Omitting one can admit unintended interference even when the return value is fixed. |

## One bounded invocation

Install with `python -m pip install -e ".[verus]"`. The backend extra uses a pinned public Verus grammar, so installation needs Git and a C compiler. Select a verifier compatible with the project's Verus syntax; a launcher that rewrites its inputs is rejected.

```bash
python -m specdet doctor --verus /path/to/verus
python -m specdet analyze --config /path/to/specdet.toml \
  --target 'src/component.rs:operation' --verus /path/to/verus \
  --llm-fallback off --offline --timeout 30 --solver-timeout-ms 1000 \
  --max-rounds 8 --counterexample-candidates 16 --out /path/to/evidence
python -m specdet report --run /path/to/evidence/RUN_ID --json
```

Use `verus.single_file` only for self-contained files, `verus.native` for an explicit crate entrypoint, and `verus.cargo` for a package with its real build features and injection file. See the [real-system profiles](../examples/real_systems/README.md). Discovery include patterns select targets, not the snapshot's entire contents: exclude build products, unrelated research copies and prior runs; do not use an artifacts directory as the project root.

`--timeout` limits **each verifier invocation**, not total wall time. Solver checks, constructor candidates, preparation and multiple proof attempts add to it. For a small first pass, set `[analysis.proof_generation] max_attempts = 1` and `strategies = ["baseline"]` in the profile. Record the actual elapsed time rather than describing this as a hard 30-second overall budget.

The default `verus-observable-v1` merges all `Err` payloads, ignores raw pointer identity, and uses available source Views. `verus-strict-v1` compares error payloads and pointer identity but still honors Views. Neither means "all heap state." Changing the policy changes the question; do not present a coarser-policy result as a repair of the original spec.

## Evidence to attach to the proof map

Keep a compact record with these fields, plus links to the underlying artifacts:

```text
caller_goal / blocking_edge / target
source_revision + working_tree_hashes + problem_id
scope: native | sealed_extract | authored_model
observation_policy + actual comparisons + ignored_dimensions
execution_status + semantic_verdict + raw baseline status
proof or witness certificate + replay command
input_feasibility + trusted assumptions + translation differences
elapsed time + per-call budgets + candidate attempts
```

The `problem_id` binds the frozen question. `contract.json` retains extracted conditions; observation and obligation artifacts retain the actual equality and ignored dimensions. Source snapshots, generated harnesses, process commands, exit codes and logs support replay. Artifact files have an envelope: inspect `payload`, not a guessed top-level `results` field. The report's `artifacts` entries locate the relevant files.

Exit codes are `0` for all decisive deterministic results, `1` for confirmed nondeterminism, `2` for execution/configuration/support failures, and `3` for remaining inconclusive results. A command that only reads a report returns `0` on successful reading; that does not change the semantic verdict.

Constructor search includes scalar, enum, array, sequence, finite map/set and simple mutable-state values. It interleaves candidate shapes and also tries unchanged post-states for no-op/error paths. These are untrusted proposals: replay still asserts every original precondition, both complete postconditions and output distinctness. Exhausting this small catalog establishes nothing about the unsearched domain. Opaque pointers and ownership tokens are not invented.

Container replays may enable standard vstd lemma groups, assert exact finite domain/membership facts, and prove container equality extensionally before asserting the original `==` clauses. These are checked proof hints, not replacement postconditions or inserted `assume`/`admit` statements. The certificate retains the generated code; it still depends on the configured Verus/vstd and source assumptions.

## Preserve the boundary of each result

A native result concerns the original crate's contract in its source context, not automatically the implementation's proof. A sealed extract must identify every omitted dependency, body stub and retained condition. An authored model additionally needs a separate source-to-model correspondence argument; finite conformance tests are not a refinement proof.

Attach a local result to the caller edge it addresses; do not mark the system's top-level goal closed because a helper is deterministic. Keep candidate revisions, changed assumptions and observation changes visible. Decisions about strengthening the contract, retaining nondeterminism or requesting human intent remain with the agent and project authority.

When blocked, retain the stage and smallest reproducer. For example, a parser/mode error is not a semantic witness; an opaque constructor gap is not budget exhaustion; a failed proof is not SAT. General macro substitution, async contracts, arbitrary resource construction, and relational handle-plus-container observations are not fully supported. Audited encoding changes such as replacing `seq![x]` with `Seq::empty().push(x)` require an explicit equivalence argument, not silent rewriting.
