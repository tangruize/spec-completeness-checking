# VeruSAGE contract regression corpus

This corpus selects **27 distinct source functions: three from each of nine
repositories**. It is intentionally not a claim of 27 determinism proofs.
The supplied anvil-controller and node-replication snapshots have no executable
contracts, and anvil-library has only two distinct executable functions.
Seven original proof functions therefore exercise explicit discovery exclusions.

Fixtures live in `tests/fixtures/verusage/`. Analysis needs only this standalone
tool, its existing dependencies, and an explicitly configured verifier. It does
not load the original projects, import the old implementation, install packages,
or contact a live LLM.

## Run

From the standalone tool root:

```sh
# Structural provenance/discovery/outcome checks; no corpus checkout or verifier.
.venv/bin/python -m unittest discover -s tests -p test_verusage_regression.py -v

# Use the native, non-mutating verifier ELF, not a source-rewriting shell wrapper.
export SPECDET_VERUS="$VERUS_ROOT/rust_verify"
export SPECDET_RUST_TOOLCHAIN="1.95.0-x86_64-unknown-linux-gnu"
export SPECDET_VERUS_RUN_DIR="examples/verusage/runs/my-regression"
.venv/bin/python -m unittest discover -s tests -p test_verusage_regression.py -v

# Reusable runner, retaining full API/verifier/query evidence.
.venv/bin/python -m examples.verusage.run \
  --analysis concrete --run-dir examples/verusage/runs/my-concrete-run

# Include default concrete-prerequisite + abstract verification.
SPECDET_VERUS_ABSTRACT=1 \
  .venv/bin/python -m unittest discover -s tests -p test_verusage_regression.py -v

.venv/bin/python -m examples.verusage.run \
  --analysis both --repo ironkv --repo memory-allocator \
  --run-dir examples/verusage/runs/my-view-run

# Diagnose paired lowering independently; does NOT validate the prerequisite workflow.
.venv/bin/python -m examples.verusage.run \
  --analysis abstract --direct-abstract \
  --run-dir examples/verusage/runs/my-direct-view-run
```

`SPECDET_RUST_TOOLCHAIN` makes the existing `ToolchainConfig` resolve runtime
libraries through rustup; no sysroot or verifier path is embedded in the tests.
The runner refuses non-ELF wrappers. It uses `Config`, `VerusBackend`, and
`specdet.api.analyze`, with 1-second solver queries, 8 search rounds, and a
30-second verifier-process timeout. The atmosphere range constructor also uses
one source-sealed, fixed offline proof candidate through the normal assistance
and replay boundary; other cases use mechanical strategies. No live model is
called. Candidate statements are checked by Verus against the original frozen
obligation, not trusted as evidence.

`--case` and `--repo` may be repeated. Unknown selectors fail instead of producing
an empty green run. Abstract cases with View inputs use the concrete-prerequisite
workflow. No-View cases disable that prerequisite solely to test the explicit
`no_abstract_inputs` applicability result.
`--direct-abstract` is an explicit diagnostic override; its reports are labeled
`abstract-direct` and are not substitutes for a passing default-workflow run.

The unittest runner skips real verification unless `SPECDET_VERUS` is set. It
also skips real abstract analysis unless `SPECDET_VERUS_ABSTRACT=1`. Successful
implicit test workspaces are removed; an explicit `SPECDET_VERUS_RUN_DIR` or a
failed run retains evidence. All automatic workspaces are under this example's
`runs/`, never a system temporary directory.

## Selections and source-based expectations

`D` means uniqueness of the configured **observable output**, not uniqueness of
hidden representation or verification of the original implementation. `N/U`
means a genuinely underconstrained contract: confirmed SAT is nondeterminism,
while bounded-search UNKNOWN is accepted only as **inconclusive**, without
semantic coverage. `P` is an original proof-function discovery exclusion.
`—` means no nontrivial input View, not an abstract proof.

| Repository | Selected functions | Concrete expectations | Abstract expectations |
|---|---|---|---|
| anvil-controller | `matching_pods_equal_to_matching_pod_entries_values`; `invariants_since_phase_i_is_stable`; `only_interferes_with_itself_equivalent_to_lifted_only_interferes_with_itself_action` | P, P, P | —, —, — |
| anvil-library | `VerusClone::verus_clone`; `vec_filter`; `map_values_weakens_no_duplicates` | D, N/U, P | D, N/U or concrete prerequisite blocked, — |
| atmosphere | `page_index2page_ptr`; `VaRange4K::new`; `VaRange4K::index` | D, D, D | —, —, D |
| ironkv | `do_vec_u8s_match`; `EndPoint::clone_up_to_view`; `endpoints_contain` | D, D, D | D, D, D |
| memory-allocator | `CommitMask::empty`; `CommitMask::is_empty`; `CommitMask::any_set` | D, D, D | —, D, D |
| node-replication | `pop_rid`; `max_of_set`; `rids_match_pop` | P, P, P | —, —, — |
| nrkernel | `ArchExec::entry_size`; `x86_arch_exec`; `ArchExec::entry_base` | D, D, D | D, —, D |
| storage | `PersistentMemoryRegions::get_num_regions`; `PersistentMemoryRegions::get_region_size`; `get_region_sizes` | D, D, D | D, D, D |
| vest | `compare_slice`; `init_vec_u8`; `set_range` | D, N/U, D | D, —, D |

The manifest explains each expectation. In particular:

* `vec_filter` specifies a **multiset**, not order: two selected distinct values
  can be returned in either order.
* `init_vec_u8` specifies **only length**. For `n = 1`, `[0]` and `[1]` both
  satisfy its contract. Its zero-filling implementation cannot strengthen that
  contract.
* `get_region_sizes` fixes length and every element, but a prover may need
  explicit sequence extensionality/quantifier instantiation.
* The proof-only `max_of_set` specifies an upper bound, not the least upper
  bound. It is not recast as an executable deterministic accessor.

## Provenance and transformations

`selection.json` is the curation input; `tests/fixtures/verusage/manifest.json` is
the sealed, generated provenance. The manifest records:

* Original path **relative to `source-projects`**, full-source SHA256 and size.
* Exact selected function/owner/mode, original line and byte spans, original
  signature/requires/ensures snippet, and body digest.
* Fixture SHA256, exact copied source/fixture spans, dependencies, every copied
  spec function's original context and digest, and precise omitted line ranges.
* Declared line edits, body-stubbing classification, compatibility annotations,
  semantic expectations, and corpus coverage limitations.

The 23 small files comprise 18 byte-identical full-source snapshots, two
dependency-closed extracts, two explicitly **contract-only** extracts, and one
full source with an explicit compatibility annotation:

* `anvil-library/vec_filter.rs`: only the filtering implementation is stubbed
  because it calls the removed `lemma_seq_properties`. Its generic trait,
  signature, callback requirements and multiset postcondition are unchanged.
* `storage/region_sizes.rs`: unrelated `deps_hack`/size/byte-conversion machinery
  is omitted. The region types, both distinct `view` contexts, and all trait and
  function contracts are unchanged. Only the executable loop body is stubbed
  because its `iter.end` syntax is obsolete.
* `atmosphere/va_range_index.rs` and `ironkv/endpoint_clone.rs`: exact
  dependency-closed source spans; no implementation or spec body is changed.
  Selected external bodies were already external in the originals.
* `vest/set_range.rs`: the **only** addition is
  `#![verifier::deprecated_postcondition_mut_ref_style(true)]`. This documented
  Verus compatibility annotation explicitly preserves the original meaning of
  bare mutable parameters in postconditions. No clauses, `old`/`final` usage,
  equality, View, or implementation is rewritten.

The two atmosphere `VaRange4K::view` definitions really differ in their original
flattened source contexts: the constructor file has a body, while the accessor
file has an external declaration. They are not interchangeable. Likewise,
`is_bit_set` in the selected `any_set` file is originally external, unlike its
definition in the other CommitMask fixtures. Tests preserve these distinctions.

To audit against an available original checkout, without changing any sources:

```sh
.venv/bin/python -m examples.verusage.seal \
  --corpus /path/to/source-projects --check
```

Without `--check`, this command regenerates **only the provenance JSON**, not
fixtures or originals. It rejects changed target headers, changed/invented spec
functions, undeclared target body changes, and copied spans that do not match.
Normal analysis and structural tests never require this checkout.

## Recorded 0.2 regression result

This section records the earlier 0.2 run. In 0.3, the separate real-source
counterexample regressions additionally confirm `init_vec_u8` by an actual
source replay rather than leaving it semantically inconclusive; its raw SMT
status is still UNKNOWN. See [`../incompleteness/`](../incompleteness/README.md).
The old results below are retained as history, not relabeled as SAT.

The completed default-workflow run matches **54/54 expectations** across 27
source selections and both analysis modes. This includes **18 concrete and 14
abstract determinism proofs**, not 54 proofs.

| Repository | Concrete proofs | Abstract proofs | Explicit remaining scope |
|---|---:|---:|---|
| anvil-controller | 0 | 0 | 3 original proof functions excluded in both modes; no exec contracts in this snapshot |
| anvil-library | 1 | 1 | `vec_filter` remains UNKNOWN; its abstract stage is skipped; 1 proof exclusion |
| atmosphere | 3 | 1 | 2 inputs have no nontrivial View; constructor proof uses the fixed offline candidate |
| ironkv | 3 | 3 | All selected input Views paired |
| memory-allocator | 3 | 2 | Constructor has no inputs to abstract |
| node-replication | 0 | 0 | 3 original proof functions excluded in both modes; no exec contracts in this snapshot |
| nrkernel | 3 | 2 | Architecture constructor has no inputs to abstract |
| storage | 3 | 3 | Trait/custom View methods and sequence extensionality are exercised |
| vest | 2 | 2 | `init_vec_u8` remains UNKNOWN; it has no nontrivial input View |

The two UNKNOWN cases are semantically underconstrained but do not have
solver-confirmed counterexamples in this bounded run. They do not contribute
decisive semantic coverage. Likewise the proof exclusions, no-View cases and
skipped prerequisite are not credited as abstract proofs.

Portable results: [`final-results.json`](final-results.json). Full current
proof/query/source evidence is under
`specdet-results/verusage-final-regression/` at the tool root.

The fixed candidate lives in
`tests/fixtures/proof_candidates/va_range_new.rs`; `reference_proofs.py` binds it
to the exact original fixture digest and current frozen request. This is an
offline interface/proof-checking regression, **not** a test of a live model's
proof-generation ability. It does not replace a contract, View, precondition,
or source implementation. Live provider authentication was unavailable in the
execution environment.

The source expectations and sealed fixtures were not weakened to reach this
result. Backend fixes addressed per-problem analysis modes, aliases,
source-derived generic/trait View evidence, slice Views, schema placeholder
rendering, and separately checked sequence proof hints.

After the all-project expectations matched, a code/architecture review found
three additional boundary bugs: alias-name shortcuts could omit output fields,
the receiver shorthand could override a real `self_` parameter, and raw-pointer
Views could become invalid pointee dereferences. All three were corrected with
new regression tests. The post-review full suite passes **378 tests**, including
these unchanged corpus expectations and real verifier execution.

## Historical first concrete run and blockers

The initial strict real-verifier run on 2026-09-11 completed all 27 selections:
**15 deterministic proofs, 7 explicit proof-function exclusions, and 5 failing
regressions**. It was **not an all-pass semantic run**; abstract verification was
not run before the parent readiness signal.

| Repository | Decisive concrete proofs | Other results |
|---|---:|---|
| anvil-controller | 0 | 3 proof exclusions |
| anvil-library | 1 | 1 proof exclusion; `vec_filter` search exception |
| atmosphere | 1 | 2 `VAddr` observation gaps |
| ironkv | 3 | |
| memory-allocator | 3 | |
| node-replication | 0 | 3 proof exclusions |
| nrkernel | 3 | |
| storage | 2 | `get_region_sizes` proof/search blocker |
| vest | 2 | `init_vec_u8` search exception |

Actionable backend blockers, intentionally **not** hidden by expected failures
or weakened assertions:

1. Both atmosphere range targets fail observation planning because the original
   alias `VAddr = usize` is not resolved. No fixture aliases are substituted.
2. Native search trace rendering raises
   `ValueError: Compiled predicate cannot be rendered: 'k'` for `vec_filter` and
   `get_region_sizes`, and `'k_lo'` for `init_vec_u8`.
3. `get_region_sizes` also needs a pointwise-to-sequence equality proof. Independent
   baseline-only probes for all three search-blocked targets compile, are
   unproved, and yield **UNKNOWN (`incomplete quantifiers`)**, not SAT.

`concrete-results.json` records the run and archive digests. All source,
JSON/process, stdout/stderr, verifier, and SMT/query evidence is retained in
compressed archives under `runs/`. Only generated ELF executables are omitted,
with their member paths and digests recorded; they are not proof transcripts.
These archives are ignored artifacts, not fixture inputs. To inspect the
original evidence layout:

```sh
tar -xzf examples/verusage/runs/concrete-proof-evidence.tar.gz \
  -C examples/verusage/runs
tar -xzf examples/verusage/runs/baseline-probes-evidence.tar.gz \
  -C examples/verusage/runs
```

Re-run the strict tests after backend changes; the original expectations should
not be revised merely to obtain green tests.

## Historical first paired probes

After the readiness signal, the complete concrete/default-abstract suite and an
explicit direct-abstract diagnostic run were executed. The default workflow has
**zero completed abstract proofs**: 14 View-bearing selections fail their
concrete prerequisite with `configuration_changed` ("Build semantics changed
after the obligation was frozen"). The two atmosphere alias failures also remain.
The concrete re-run retains the earlier 15 proofs, 7 exclusions and 5 failures.

Direct probes intentionally bypass that workflow defect. They establish
**7 abstract proofs with all declared View inputs independently paired**:

| Repository | Fully paired direct abstract proofs | Remaining direct results |
|---|---:|---|
| anvil-controller | 0 | 3 proof exclusions |
| anvil-library | 0 | Generic trait View missed; `vec_filter` search exception |
| atmosphere | 0 | 1 no-View case; 2 `VAddr` observation gaps |
| ironkv | 3 | All intended inputs paired |
| memory-allocator | 2 | `empty` has no inputs |
| node-replication | 0 | 3 proof exclusions |
| nrkernel | 2 | Architecture constructor has no inputs |
| storage | 0 | Trait/generic View declarations missed for all three targets |
| vest | 0 | Slice Views missed; `init_vec_u8` has no View inputs |

These additional blockers are not treated as `not_applicable` successes:

1. `VerusClone::verus_clone`, the two `PersistentMemoryRegions` trait getters,
   and generic `get_region_sizes` report `no_abstract_inputs` despite their
   source View bounds/declarations. The clone's current concrete output plan is
   also raw `r1 == r2` with an unknown `Self` type, not a View-level relation.
   Complete source-bound trait observation handling must address returns as
   well as inputs; merely discovering an input View would not establish the
   advertised View-level result.
2. `compare_slice` fails to discover either slice View. `set_range` produces a
   native deterministic result, but pairs only `data` and leaves `input: &[u8]`
   shared. That is a **weaker input problem**, not the declared joint-input
   regression, and is therefore a strict failure rather than an eighth proof.
3. The original alias/search blockers persist. The direct `vec_filter` baseline
   is UNKNOWN (`incomplete quantifiers`) before the search exception; it is not
   a confirmed counterexample.

The runner now checks the exact expected set of paired parameter names, distinct
left/right symbols across inputs, View-equivalence relations and source-backed
View metadata. It rejects partial pairing even when the native verifier reports
determinism. Skipped prerequisites and no-View cases cannot contribute abstract
semantic coverage, and a concrete proof ID cannot be reused for an abstract goal.

`abstract-results.json` records per-case results and evidence locations. Its
archives retain all source and proof/query/process evidence, excluding only
recorded generated executables:

```sh
tar -xzf examples/verusage/runs/paired-regression-proof-evidence.tar.gz \
  -C examples/verusage/runs
tar -xzf examples/verusage/runs/direct-abstract-proof-evidence.tar.gz \
  -C examples/verusage/runs
```

These historical failures are retained to explain the regression coverage.
They are resolved in the current run above; direct probes were not substituted
for the default prerequisite workflow.
