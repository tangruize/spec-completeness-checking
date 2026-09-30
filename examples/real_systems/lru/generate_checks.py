#!/usr/bin/env python3
"""Extract independent proof checks from the already-frozen checker goals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from run import CHECKER, ROOT, sha256, write_json

sys.path.insert(0, str(CHECKER / "src"))

from specdet.adapters.verus.witness_replay import witness_goal
from specdet.domain.models import Obligation, TargetRef


def load_goals(measured_path: Path) -> dict:
    measured = json.loads(measured_path.read_text())
    summary = json.loads((ROOT / measured["report"]).read_text())["payload"]
    result = {}
    for item in summary["results"]:
        target_dir = Path(summary["run_dir"]) / "targets" / TargetRef(**item["target"]).id
        reference = next(a for a in item["artifacts"] if a["kind"] == "obligation")
        payload = json.loads((target_dir / reference["path"]).read_text())["payload"]
        obligation = Obligation(
            payload["problem_id"], TargetRef(**payload["target"]),
            payload["native"], payload["trusted_translation"],
        )
        result[item["target"]["name"]] = (obligation, witness_goal(obligation))
    return result


def cache(capacity: int, items: list[tuple[int, int]]) -> str:
    entries = "Map::<u64, u64>::empty()"
    order = "Seq::<u64>::empty()"
    for key, value in items:
        entries += f".insert({key}u64, {value}u64)"
        order += f".push({key}u64)"
    return f"Cache {{ capacity: {capacity}usize, entries: Ghost({entries}), order: Ghost({order}) }}"


def option(value: int | None) -> str:
    return "Option::<u64>::None" if value is None else f"Option::<u64>::Some({value}u64)"


def proof(name: str, goal, capacity: int, before: list, after: list, key: int,
          value: int | None, first: int | None, second: int | None, distinct: bool) -> str:
    bindings = {
        "pre_cache": cache(capacity, before),
        "key": f"{key}u64",
        "post1_cache": cache(capacity, after),
        "r1": option(first),
        "post2_cache": cache(capacity, after),
        "r2": option(second),
    }
    if value is not None:
        bindings["value"] = f"{value}u64"
    assert set(bindings) == {parameter for parameter, _ in goal.parameters}
    lines = [
        f"proof fn {name}() {{",
        "    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,",
        "        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};",
        "    reveal_with_fuel(Seq::<_>::filter, 5);",
    ]
    for parameter, typ in goal.parameters:
        lines.append(f"    let {parameter}: {typ} = {bindings[parameter]};")
    for parameter in ("pre_cache", "post1_cache", "post2_cache"):
        lines.append(f"    assert({parameter}.entries@.dom() =~= {parameter}.order@.to_set());")
        lines.append(f"    assert({parameter}.wf());")
    for requirement in goal.requires:
        lines.append(f"    assert({requirement});")
    # Extensionality is a proof hint only: the following assertion still checks
    # the exact, unmodified frozen postcondition.
    hint = goal.posts.replace(".entries@ == ", ".entries@ =~= ").replace(".order@ == ", ".order@ =~= ")
    lines.append(f"    assert({hint});")
    lines.append(f"    assert({goal.posts});")
    if distinct:
        lines.append(f"    assert({goal.distinctness});")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compatible-run", type=Path, required=True)
    parser.add_argument("--control-run", type=Path, required=True)
    args = parser.parse_args()
    complete = load_goals(ROOT / args.compatible_run)
    control = load_goals(ROOT / args.control_run)
    for goals, source in (
        (complete, ROOT / "spec/compatible.rs"),
        (control, ROOT / "controls/pop_return_omitted.rs"),
    ):
        for obligation, _ in goals.values():
            if (
                not obligation.trusted_translation
                or obligation.native["source_digest"] != sha256(source)
                or obligation.native["source"] != source.read_text()
            ):
                raise ValueError("frozen obligation does not match the current source")
    complete_pop = complete["pop"][0].native["function_spec"]
    control_pop = control["pop"][0].native["function_spec"]
    omitted = "result == lookup(old(cache), key)"
    if (
        complete_pop["requires"] != control_pop["requires"]
        or omitted not in complete_pop["ensures"]
        or [clause for clause in complete_pop["ensures"] if clause != omitted]
        != control_pop["ensures"]
    ):
        raise ValueError("control differs from the original pop by more than its return clause")
    determinism = ROOT / "proofs/model_determinism.rs"
    determinism.write_text(
        '// Frozen unguarded determinism goals; no inserted assumptions or candidate hints.\n'
        'include!("../spec/compatible.rs");\n\nverus! {\n\n'
        + "\n\n".join(obligation.native["template"] for obligation, _ in complete.values())
        + "\n\n}\n"
    )
    # Each tuple is a concrete normally returning operation, not a supplied
    # counterexample to the sealed candidate.
    cases = [
        ("put_empty", "put", 2, [], [(0, 0)], 0, 0, None),
        ("put_nonfull", "put", 3, [(1, 1), (0, 0)], [(2, 2), (1, 1), (0, 0)], 2, 2, None),
        ("put_update_full", "put", 2, [(1, 1), (0, 0)], [(0, 2), (1, 1)], 0, 2, 0),
        ("put_evict", "put", 2, [(1, 1), (0, 0)], [(2, 2), (1, 1)], 2, 2, None),
        ("put_capacity_one", "put", 1, [(0, 0)], [(1, 1)], 1, 1, None),
        ("get_hit", "get", 2, [(1, 1), (0, 0)], [(0, 0), (1, 1)], 0, None, 0),
        ("get_miss", "get", 2, [(1, 1)], [(1, 1)], 0, None, None),
        ("pop_hit", "pop", 2, [(1, 1), (0, 0)], [(1, 1)], 0, None, 0),
        ("pop_miss", "pop", 1, [], [], 0, None, None),
    ]
    text = ['// Generated from frozen source-derived postconditions; no target is called.',
            'include!("../spec/compatible.rs");', "", "verus! {", ""]
    for name, operation, capacity, before, after, key, value, returned in cases:
        _, goal = complete[operation]
        text.extend([proof(name, goal, capacity, before, after, key, value,
                           returned, returned, False), ""])
    text.extend(["}", ""])
    satisfiability = ROOT / "proofs/model_satisfiability.rs"
    satisfiability.write_text("\n".join(text))

    obligation, goal = control["pop"]
    text = [
        "// Deliberately supplied witness for the labeled mutation, NOT automatically discovered.",
        'include!("../controls/pop_return_omitted.rs");', "", "verus! {", "",
        obligation.native["det_spec"]["equal_fn_def"], "",
        proof("known_pop_return_omission", goal, 1, [], [], 0, None, None, 0, True),
        "", "}", "",
    ]
    witness = ROOT / "proofs/known_omission_witness.rs"
    witness.write_text("\n".join(text))
    write_json(ROOT / "proofs/provenance.json", {
        "compatible_spec_sha256": sha256(ROOT / "spec/compatible.rs"),
        "control_spec_sha256": sha256(ROOT / "controls/pop_return_omitted.rs"),
        "determinism_proof_sha256": sha256(determinism),
        "satisfiability_proof_sha256": sha256(satisfiability),
        "known_omission_witness_sha256": sha256(witness),
        "satisfiability_cases": [case[0] for case in cases],
        "known_omission_witness_is_user_supplied": True,
        "control_problem_id": obligation.problem_id,
        "method": "Replay frozen general determinism templates. Concrete sample/witness proofs assert original preconditions and both postconditions; the known control additionally asserts original observation inequality. Concrete proofs have no preconditions, assumptions, admissions, or target calls.",
        "scope": "General contract determinism, sampled feasibility, and a supplied witness for the known mutation; not refinement of the original Rust implementation.",
    })
    print("Generated frozen determinism proofs, exact-goal feasibility checks, and the labeled supplied witness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
