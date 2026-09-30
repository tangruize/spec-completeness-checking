---
name: specdet-evidence
description: Collect bounded, source-linked Verus contract determinism evidence for a system proof agent; use for underconstraint checks and evidence on a caller/proof-map edge, not specification-quality decisions.
---

# Collect contract determinism evidence

[中文](SKILL.zh-CN.md) · [Usage, evidence fields and terminology](README.md)

Use `specdet` as a mechanical evidence tool. Do not start another agent, enable LLM assistance, or replace the source contract merely to run it.

## Inputs

Obtain the caller goal/proof-map edge, exact target and source revision with working-tree hashes, intended observations, checker revision, matching project profile and Verus toolchain, and a local evidence directory. Record whether the input is **native source**, a **sealed extract**, or an **authored model**; the tool cannot infer source-to-model correspondence.

In this workspace use `/home/ruize/system-proof-agent/spec-completeness-checking/.venv/bin/python`, not the older copy under `tools/`. See the guide for portable installation.

## Procedure

1. Preserve the candidate before checking it. Choose `verus.single_file`, `verus.native` or `verus.cargo` according to the actual build context; do not silently flatten a crate.
2. Invoke `python -m specdet analyze` with the profile and target, `--compact-json --llm-fallback off --offline`, and explicit output storage. Use the [guide's command](README.md#run). The default whole-analysis budget is 60 seconds; `--run-timeout` overrides it, while `--timeout` remains per verifier call.
3. Read execution `status` separately from semantic `verdict`. Preserve the original `baseline`, `problem_id`, actual observation policy, exclusions and feasibility/translation boundaries. Check `decisive_evidence` and `counterexample`, not just an exit code or a build's total proof count.
4. Follow the relevant certificate, observation plan, query or diagnostic artifact only when necessary. Compact previews are not exhaustive; omitted/truncated markers point back to the complete report. Keep raw snapshots and logs local.
5. Attach the evidence record below to the caller/proof-map edge. Keep completed results on timeout; retain the unfinished target and `pending_targets` without inventing conclusions.

## Evidence handoff

```text
Caller-supplied: caller_goal, proof_map_edge, native/extract/model scope,
                source revision + working-tree hashes, checker revision, extraction/model assumptions
Tool-supplied:  run_dir/full_report, target/source_digest, problem_id,
                status/verdict, original baseline, proof/witness artifact + digest,
                policy/exclusions/coverage, diagnostics, durations and budgets
```

Do not convert `deterministic` into “adequate/correct,” `UNKNOWN` or a timeout into “bug,” a conditional alternative into an unconditional witness, or local UNSAT into a global proof. A verified constructive witness can coexist with an UNKNOWN baseline. Whether the demonstrated freedom matters to the caller, whether it is intentional, and whether to change the specification remain decisions for the system proof agent and project authority.
