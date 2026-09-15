"""Construct and certify source-level counterexamples without changing raw SMT results."""
from __future__ import annotations

from itertools import islice, product
from pathlib import Path

from specdet.domain.models import (
    CounterexampleEvidence, CounterexampleSearchResult, Diagnostic, JsonObject,
    Obligation, Stage, digest,
)
from specdet.storage.artifacts import ArtifactStore
from specdet.storage.workspace import PreparedProject

from .witness_replay import UnsupportedWitness, replay_witness, witness_goal
from .witness_values import candidate_bindings


def _instantiations(obligation: Obligation) -> list[JsonObject]:
    parameters = witness_goal(obligation).generics
    if not parameters:
        return [{}]
    catalogs = [
        ("bool", "u8") if kind == "type" else (1, 0)
        for _, kind, _ in parameters
    ]
    return [
        dict(zip((name for name, _, _ in parameters), values))
        for values in islice(product(*catalogs), 4)
    ]


def find_counterexample(
    backend, project: PreparedProject, obligation: Obligation, artifact_dir: Path,
    *, max_candidates: int, accepted: JsonObject | None = None,
) -> CounterexampleSearchResult:
    if max_candidates <= 0:
        return CounterexampleSearchResult(exhausted=True)
    store = ArtifactStore(artifact_dir)
    attempts = 0
    diagnostics = []
    seen: set[str] = set()

    def candidates():
        if accepted is not None:
            yield accepted["bindings"], accepted.get("type_arguments", {})
        for specialization in _instantiations(obligation):
            for values in candidate_bindings(
                obligation, max_candidates=max_candidates, type_arguments=specialization,
            ):
                yield values, specialization

    try:
        for bindings, arguments in candidates():
            key = digest([bindings, arguments])
            if key in seen:
                continue
            seen.add(key)
            if attempts >= max_candidates:
                break
            directory = artifact_dir / f"candidate-{attempts:04d}"
            attempts += 1
            result = replay_witness(
                backend, project, obligation, bindings, directory, type_arguments=arguments,
            )
            store.event({
                "event": "source_witness_attempt", "attempt": attempts,
                "candidate_digest": key, "status": result["status"],
                "artifact": str(directory),
            })
            if result["modified_inputs"]:
                raise UnsupportedWitness("Verifier modified the frozen replay inputs")
            if result["status"] == "verified" and result["verified_goals"] > 0:
                evidence = CounterexampleEvidence(
                    problem_id=obligation.problem_id, obligation_digest=obligation.id,
                    status="verified", verified_goals=int(result["verified_goals"]),
                    bindings=bindings, type_arguments=arguments,
                    artifact=str(directory / "replay.json"),
                    certificate_digest=digest(result),
                    bindings_digest=digest([bindings, arguments]),
                )
                summary = CounterexampleSearchResult(evidence, attempts)
                store.artifact("result.json", "counterexample_search", summary, (obligation.id,))
                return summary
            if result["status"] not in {"unproved"}:
                diagnostics.append(Diagnostic(
                    Stage.COUNTEREXAMPLE, "candidate_replay_failed",
                    f"Candidate replay ended with {result['status']}",
                    "warning", {"artifact": str(directory)},
                ))
    except UnsupportedWitness as error:
        diagnostics.append(Diagnostic(
            Stage.COUNTEREXAMPLE, "unsupported_witness", str(error), "warning",
        ))
    summary = CounterexampleSearchResult(
        attempts=attempts, exhausted=attempts >= max_candidates,
        diagnostics=tuple(diagnostics),
    )
    store.artifact("result.json", "counterexample_search", summary, (obligation.id,))
    return summary
