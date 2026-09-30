---
name: specdet-evidence
description: Collect bounded, source-linked contract determinism evidence for a system proof agent without deciding specification adequacy.
---

# Collect specification evidence

Use this skill when evaluating a candidate Verus contract for underconstraint, comparing candidate revisions, or recording why a completeness check is inconclusive. The checker is a local tool; do not start an agent workflow or enable an LLM fallback merely to run it.

Read [the evidence guide](../../../docs/agent-evidence.md) for invocation, terminology, result codes and artifact layout.

1. Identify the caller goal, exact target/revision and intended observations. Preserve the initial candidate before checking it.
2. Choose the existing native, Cargo or explicitly sealed/model profile. Keep source snapshots and outputs separate; record any translation or dependency abstraction.
3. Run `python -m specdet analyze` with an explicit target, verifier, output directory and finite budgets. Use `--llm-fallback off --offline` unless assistance was deliberately requested.
4. Inspect the execution status, semantic verdict, original baseline, actual output equality and ignored dimensions. Read the certificate or failure diagnostics, not just the process exit code.
5. Attach source hashes, problem ID, scope, assumptions, witness/proof, replay path and elapsed time to the relevant caller/proof-map edge. Keep an inconclusive or conditional result labeled as such.

Do not turn `deterministic` into "correct/adequate," `UNKNOWN` into "bug," or a model witness into a native implementation result. Do not weaken conditions, merge observations or add assumptions merely to obtain a successful check. Whether a demonstrated freedom is required behavior, a harmless abstraction or an important omission is a separate specification judgment.
