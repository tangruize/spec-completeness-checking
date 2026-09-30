# spec-determinism-tool

**English** | [Chinese (Simplified)](README-CN.md)

`specdet` is a standalone specification determinism analysis tool. It checks
whether a contract permits only one **observational equivalence class** of
outputs for the same inputs. It does not establish implementation correctness,
specification adequacy, or termination.

Verus is the first language backend. The core orchestration does not depend on
Verus ASTs, Z3 objects, or an LLM provider. See
[`ARCHITECTURE.md`](ARCHITECTURE.md), currently in Chinese, for the design and
migration constraints.

For system-proof agents, see the compact [evidence guide](docs/agent-evidence.md), [reusable skill](.github/skills/specdet-evidence/SKILL.md), and [HFS/Nanvix/mimalloc/LRU profiles](examples/real_systems/README.md). They distinguish native proofs, sealed extracts, authored models and conditional claims; they do not assign specification-quality verdicts.

## Installation and basic usage

Python 3.11 or later is required. Verus is an external tool: configure it
explicitly or make it available on `PATH`. The tool does not implicitly look
for a Nanvix or VeruSAGE installation.

Installing the Verus extra also requires Git and a C compiler: its grammar is pinned to a public source revision rather than an unavailable PyPI release.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[verus]"

specdet --help
specdet doctor --verus /path/to/verus
specdet discover examples/basic/contracts.rs
specdet analyze --config examples/basic/specdet.toml \
  --verus /path/to/verus --out ./specdet-results
```

Use a configuration file and a target selector to narrow the analysis:

```bash
specdet analyze --config examples/basic/specdet.toml \
  --verus /path/to/verus \
  --target 'contracts.rs:identity'
```

Relative paths in a configuration file are resolved against that file, not the
working directory. `--target` accepts function names, qualified names, files,
`file:function@line` selectors, or explicit glob patterns. Ambiguous function
names require a location. Analyzing a file directly lists all matching `exec`
declarations; declarations without postconditions are explicitly reported as
`no_contract`. The example configuration uses `visibility = "public"` to
exclude the unannotated `main`. Without an input path or configuration
argument, analysis reads `specdet.toml` from the current directory rather than
silently scanning the entire working directory.

`verus.single_file` handles self-contained files. `verus.native` preserves
module context through an explicit `build.entrypoint`. `verus.cargo` uses the
project's package, features, and `injection_file` configuration. The latter two
modes require the correct project and toolchain context; they do not silently
fall back to single-file analysis.

Configure `adapters.verus.executable` or `toolchain_root`, and specify
`rust_toolchain` when necessary. The tool uses `rustup` to resolve the
corresponding runtime library directory. Choose a verifier launcher that does
not modify its inputs: if the verifier rewrites generated source, the tool
refuses to attribute the result to the frozen analysis goal.

## Pipeline and results

```text
Freeze inputs / discover targets / prepare an isolated working copy
  -> extract
  -> observation plan
  -> generate a frozen determinism function (det fn)
  -> proof generation -> proof checking
  -> baseline query / schema search
  -> evidence-based report
```

Proof generation and proof checking are separate stages. The baseline is
retained before applying mechanical strategies, supplied candidates, or
explicitly enabled LLM assistance. Failure diagnostics can feed back into
proof generation. Producing proof text, proving only a narrowed slice, or
ignoring proof obligations introduced by a new helper does not establish
global determinism.

| Verdict | Meaning |
|---|---|
| `deterministic` | The original problem is UNSAT, or a valid complete proof establishes the same frozen goal. |
| `nondeterministic` | The original problem or a valid refinement has SAT evidence, or a concrete counterexample has been confirmed against the original specification. This does not automatically imply a specification bug. |
| `inconclusive` | No decision was established, for example because the solver returned unknown, assistance was unsuccessful, or translation fidelity was not confirmed. |
| `not_evaluated` | There is insufficient valid analysis evidence, for example because a contract is missing or an input is unsupported. |

Execution status and semantic verdict are stored separately. Later proofs do
not overwrite the raw baseline. Unconfirmed search constraints are recorded
separately from SAT-confirmed slices. Project allowlists, `permitted`
annotations, and LLM self-assessments cannot change solver conclusions.

## Tool-native output and elapsed time

The default human output shows each target's verdict, original baseline, decisive evidence or actionable blockers, observation exclusions, unchecked scope, actual budget usage and elapsed time. Repeated recovery warnings do not crowd out distinct failure causes. The final stdout line reports whole-run elapsed time.

Use `--compact-json` for a versioned, bounded-detail evidence summary, or keep `--json` for the complete recorded report:

```bash
specdet analyze --config specdet.toml --verus /path/to/verus --compact-json
specdet report --run /path/to/evidence/RUN_ID --compact-json
specdet report --run /path/to/evidence/RUN_ID --json
```

`--compact-json` is available on `analyze`, `discover`, `report`, `replay` and `assist`; it is mutually exclusive with `--json`. Its `format` is `specdet.summary.v1`. It preserves source/problem identities, execution status, semantic verdict, raw baseline, evidence and small witness values, coverage, budgets and artifact links, without embedding verifier logs or generated source. Long previews and additional diagnostics are explicitly marked as truncated or omitted. Output still scales with the number of selected targets. Full reports and artifacts are never truncated. No agent adapter or extra model call is involved.

`duration_ms` records analysis wall time, including preparation but excluding final report serialization and CLI presentation; each target also records its elapsed time. `resources` counts usage across all target phases and revisions, including a concrete prerequisite for abstract analysis. Per-verifier and per-query timeouts are **not a whole-run deadline**. Handled failures and interruptions report invocation elapsed time; compact mode also returns structured status and exit code (`130` for interruption). Reading an old report does not invent missing timings or budgets. `report` displays the stored analysis duration and semantic exit code, while the report-reading command itself returns `0` on success. See the [evidence guide](docs/agent-evidence.md) for fields and interpretation.

## Real underconstrained functions and concrete counterexamples

Version 0.3 adds bounded constructor search and source-specification
counterexample certificates. It generates concrete inputs and two sets of
outputs from the frozen types, then asks the real Verus verifier to establish
the following in an independent working copy:

```text
P(input) && Q(input, output1) && Q(input, output2) && !E_out(output1, output2)
```

The replay function has no candidate preconditions and does not substitute a
call to the target implementation for its contract. Vectors, arrays, tuples,
structs, and ghost sequences are constructed from explicit values. Generic
instantiations must satisfy the original bounds. Candidates with equal outputs
or outputs that violate the original postcondition are rejected.
`--counterexample-candidates 0` disables this path. The default limit is 16
candidates; exhausting the budget does not establish determinism.

The original full Verus SMT query can still return `unknown` even when a
concrete witness has been confirmed. Background boxing axioms involve infinite
domains, which can prevent finite-model search from producing SAT. Supplying a
candidate does not guarantee a SAT result either. These outcomes are therefore
kept distinct:

```text
baseline.status = unknown
counterexample.kind = source_verified_constructive
counterexample.status = verified
verdict = nondeterministic
```

This does not claim that the original SMT query returned SAT. Counterexample
JSON retains the concrete bindings, generic instantiation, original problem
ID, certificate digest, and replayable Rust harness. The CLI displays the
actual counterexample and certificate path.

A constructor's type text must be a single valid type; it cannot contain
statements or assumptions. Generic replay currently rejects source-relative
`self::` and `super::` paths explicitly, because moving them into an
instantiation module could change the meaning of the original contract or
bounds. Unsupported cases remain unresolved rather than producing invalid
certificates.

Strict end-to-end cases use three original VeruSAGE functions:

| Source | Underconstraint | Automatically found and source-verified witness |
|---|---|---|
| `vest::init_vec_u8` | Only the vector length is constrained. | `n=1`, with outputs `[0]` and `[1]`. |
| `memory-allocator::CommitMask::next_run` | The contract does not require the first or maximal run and allows empty intervals. | The same mask and `idx=0`, with outputs `(0,0)` and `(1,0)`. |
| `atmosphere::Array::new` | `wf()` constrains only the ghost sequence length. | `Array<bool,1>` values with Views `[false]` and `[true]`. |

The tests do not supply predefined output pairs. They must automatically
detect `nondeterministic` through the complete API, produce a nonempty
source-level certificate, and match the original contract. An UNKNOWN result
without a counterexample certificate does not pass these tests. Provenance,
original file SHA256 hashes, and the sole compatibility annotation are recorded
in `tests/fixtures/incomplete/manifest.json`. The original repositories and
contracts were not modified, strengthened, or weakened.

Analysis exit codes are `0` when all targets are deterministic, `1` when
confirmed nondeterminism exists, `2` for configuration, execution, or support
problems, and `3` when results remain inconclusive. Execution errors take
precedence over semantic conclusions. Successfully reading a report returns
`0`.

## Optional stage-level LLM fallback

By default, the tool does not call a model or read an implicit legacy LLM cache.

```bash
specdet analyze --config specdet.toml \
  --llm-fallback live --provider copilot --model MODEL

specdet analyze --config specdet.toml \
  --llm-fallback replay --responses /path/to/recorded/generation
```

`all_applicable` covers registered routes such as target discovery, project
preparation, extraction, observation, lowering, proof generation, search, and
explanation. It is not limited to two stages, and it does not request a model
unconditionally at every stage. Each candidate must satisfy its backend's
validation and adoption rules. Candidates whose source fidelity cannot be
confirmed, or which require semantic approval, remain unadopted artifacts.
Additional budget does not grant additional trust.

Abstract-proof requests also carry the frozen input View relations, paired
and shared input bindings, source definitions of the Views, and the original
output relation. A candidate cannot narrow the problem by replacing View
equality with concrete input equality. Choosing different `abstract_inputs`
is also a semantic change requiring approval. Concrete and abstract proof
candidates are bound to their respective problem IDs and cannot be reused
across those problems as established proofs.

```toml
[assistance]
mode = "off" # off | live | replay
stage_policy = "all_applicable"
excluded_stages = []
max_rounds_per_stage = 2
max_requests_per_target = 3
max_requests_per_run = 30
request_timeout_seconds = 300
```

Replay accepts only responses with an exactly matching request digest. Missing
responses produce `replay_miss`; replay never switches to an online call.
`--offline` prohibits online providers and Cargo downloads, and rejects
conflicting live configuration.

You can rerun a specific assistance task against an existing run's snapshot:

```bash
specdet assist --run /path/to/run --task proof_generation \
  --mode replay --responses /path/to/recordings
```

Providers generate text candidates only and have no permission to modify the
input project. Diagnostics and explanations are annotations; compilation,
proof checking, SMT decoding, semantic judgments, and statistics remain the
responsibility of the mechanical implementation.

The Copilot transport uses isolated configuration and working directories and
disables tools, MCP, hooks, and additional instruction sources. Authentication
uses an environment token rather than copying an existing interactive login
configuration. CLI versions without the required isolation options are
rejected instead of falling back to an unrestricted agent. Alternatively,
explicitly configure `provider = "subprocess"`, a `command` argument array, and
`text_only = true` to connect a client that reads prompts from stdin and returns
candidate JSON on stdout.

`specdet adopt --proposal ... --validation ... --out ...` reads recorded
candidates and validation results and writes an explicit adoption record. It
does not directly modify source or turn `needs_approval` or `unverified` into
an accepted result. Subsequent execution must still revalidate candidates
against the current inputs.

## Artifacts and replay

Each run has an independent directory containing its configuration, source
snapshot manifest, target list, stage artifacts, raw verifier logs, proof
attempts, search traces, candidate adoption records, and reports. JSON artifacts
carry a schema version, input references, and a payload digest. Writes use
atomic replacement.

```bash
specdet report --run /path/to/run --json
specdet replay --run /path/to/run
specdet replay --run /path/to/run --reexecute --verus /path/to/verus
```

By default, replay reads existing reports without running tools. `--reexecute`
validates the snapshot and creates a new run without modifying the old
evidence. It does not stitch together selected old files as successful
checkpoints. Changes to tool versions, budgets, or solver behavior can produce
different results in the new execution.

## Abstract / input-view determinism

```bash
specdet analyze --config examples/abstract/specdet.toml --verus /path/to/verus
# Or override a regular configuration:
specdet analyze --config specdet.toml --kind abstract_determinism --abstract-input self
```

By default, the tool first checks determinism for the same concrete inputs.
Only after that result is proved does it check the paired-input abstract
property:

```text
V(x1, x2) && P(x1) && P(x2) && Q(x1, y1) && Q(x2, y2)
    ==> E_out(y1, y2)
```

`V` is the source-backed View equivalence relation for the selected inputs.
Unselected parameters remain shared. Each modeled execution has its own
pre-state and post-state, and outputs are still compared with the same
`E_out`. By default, the tool selects resolvable non-identity Views; you can
also configure a subset of parameters:

```toml
[analysis]
kind = "abstract_determinism"

[analysis.abstract]
require_concrete = true
inputs = ["self"]
```

The final report's `concrete_result` retains the complete first-stage evidence;
the top-level report describes the abstract problem. If the concrete result
is UNKNOWN or nondeterministic, the abstract check is explicitly skipped. If
there are no abstractable inputs, the result is `not_applicable`, not a proof
of abstract determinism. The two problems have separate proof-attempt limits,
while LLM request and search budgets are shared within the target.

`--no-concrete-prerequisite` checks the abstract formula directly. In that
mode, failure may arise solely from ordinary contract underconstraint and
cannot by itself establish hidden representation dependence. This
implementation does not prove domain preservation,
`V(x1,x2) ==> (P(x1) <==> P(x2))`. Reports explicitly retain
`domain_preservation = "not_checked"` rather than conflating the two properties.

## Python API and code structure

```python
from pathlib import Path
from specdet.api import analyze
from specdet.config import load_config

summary, exit_code = analyze(load_config(Path("specdet.toml")))
```

The high-level API accepts an explicit `LanguageBackend` and session driver.
The mechanical API does not silently construct a provider merely because of
an environment variable or configuration string.

Paths below are relative to `src/specdet/`.

| Location | Responsibility |
|---|---|
| `domain/`, `ports/` | Versioned data contracts, evidence, proposals, and language and assistance interfaces. |
| `analysis/`, `api.py` | Stage state machine, proof loop, budget boundaries, batch orchestration, and result aggregation. |
| `adapters/verus/` | Verus frontend, observation, lowering, execution, candidate validation, and query interfaces. |
| `adapters/verus/native/` | Mechanical Verus logic migrated from the legacy implementation, kept private to the backend. |
| `assistance/`, `providers/` | Outer candidate controller, explicit provider calls, exact replay, and adoption records. |
| `storage/`, `cli/` | Snapshots and artifacts; configuration and command composition. |

Concrete-input and abstract/input-view determinism are supported. Dafny is not
implemented. Other languages can be added through the backend interface
without emulating Verus syntax or SMT logs.

The tool cannot automatically resolve every macro expansion, heap/reference
semantic issue, or source-translation fidelity question. Uncovered cases must
produce a gap or unsupported result, not a fabricated completeness conclusion.
Historical SpecGym, vstd, and project-specific experiment runners were not
carried over as implicit dependencies. Source-traceable VeruSAGE regression
examples are documented in
[`examples/verusage/`](examples/verusage/README.md).

## Development

```bash
python -m unittest discover -s tests
# Optional real-Verus integration, using a verifier that does not rewrite inputs:
SPECDET_VERUS=/path/to/verus python -m unittest discover -s tests
```

Tests cover the LLM-free core, stage resumption, candidate and evidence
boundaries, configuration and snapshot isolation, and migrated mechanical
modules. Real-verifier cases are skipped when `SPECDET_VERUS` is unset. They
do not download Verus or call online models.

## Version 0.2 architecture review

After all per-project regression expectations matched, an architecture review
identified and fixed three boundary errors affecting problem definitions:

| Issue | Fix |
|---|---|
| Guessing return structure from names such as `PResult` and `SResult` could omit comparisons of successful payload fields. | Removed name-based exceptions; equivalence is generated only from source-resolved type structure. |
| The `self_` selector alias could override a real parameter with that name and pair the wrong inputs. | Exact source parameter names take precedence; the receiver alias is accepted only when no parameter has that name. |
| A raw pointer's View was incorrectly treated as a dereference of its pointee. | Use vstd's pointer-data View instead of treating a raw pointer as readable memory. |

Each fix has regressions, including real-Verus rejection of incorrect goals
and verification of correct ones. The full suite at the end of the 0.2 review
had 378 passing tests, covering 54 expected outcomes across nine VeruSAGE
snapshots. Only 32 of those outcomes were determinism proofs; see the
regression report for coverage limitations. Live LLM generation quality was
not part of those results.

## Legacy implementation and backups

In the migration workspace, the legacy directory
`/home/chentianyu/intent_formalization/spec-determinism/` remains unchanged.
The new package does not run through `sys.path` manipulation, symlinks, or
imports of the legacy package.

The local migration snapshot is
`backups/legacy-source-20260911T044355Z.tar.gz`. It contains the mechanical
package, scripts, configuration, related documentation, and selected vstd
entrypoints captured during migration. Worktree status, source digests, and
the original HEAD were also preserved. Large historical experiment results
remain in the legacy directory rather than being copied indiscriminately.
`backups/` is not included in installation or distribution.
