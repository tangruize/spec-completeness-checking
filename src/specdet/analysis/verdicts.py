from __future__ import annotations

from specdet.domain.models import (
    AnalysisReport, Diagnostic, SolverStatus, Stage, Verdict, digest,
)


def classify(report: AnalysisReport, *, trusted_translation: bool = True) -> Verdict:
    baseline = report.baseline
    proved = any(
        proof.status == "verified" and proof.verified_goals > 0
        and proof.problem_id == report.problem_id
        for proof in report.proofs
    )
    sat = baseline is not None and baseline.status == SolverStatus.SAT
    unsat = baseline is not None and baseline.status == SolverStatus.UNSAT
    witness = report.counterexample
    source_counterexample = False
    if witness is not None:
        if witness.problem_id != report.problem_id:
            report.diagnostics.append(Diagnostic(
                Stage.REPORT, "problem_mismatch", "Counterexample belongs to a different problem",
            ))
            return Verdict.INCONCLUSIVE
        source_counterexample = (
            witness.kind == "source_verified_constructive"
            and witness.status == "verified"
            and witness.verified_goals > 0
            and bool(witness.certificate_digest)
            and witness.bindings_digest == digest([witness.bindings, witness.type_arguments])
        )
    if baseline is not None and baseline.problem_id != report.problem_id:
        report.diagnostics.append(Diagnostic(
            Stage.REPORT, "problem_mismatch", "Baseline belongs to a different problem",
        ))
        return Verdict.INCONCLUSIVE
    if report.search is not None:
        for evidence in report.search.evidence:
            if evidence.problem_id != report.problem_id:
                report.diagnostics.append(Diagnostic(
                    Stage.REPORT, "problem_mismatch", "Search evidence belongs to a different problem",
                ))
                return Verdict.INCONCLUSIVE
            if evidence.status == SolverStatus.SAT and evidence.role in {"baseline", "refinement"}:
                sat = True
            if evidence.status == SolverStatus.UNSAT and evidence.role == "baseline":
                unsat = True
    if (sat or source_counterexample) and (unsat or proved):
        report.diagnostics.append(Diagnostic(
            Stage.REPORT, "inconsistent_evidence",
            "A confirmed counterexample and a determinism proof refer to the same problem",
        ))
        return Verdict.INCONCLUSIVE
    verdict = (
        Verdict.NONDETERMINISTIC if sat or source_counterexample
        else Verdict.DETERMINISTIC if unsat or proved
        else Verdict.INCONCLUSIVE if baseline is not None or report.proofs
        else Verdict.NOT_EVALUATED
    )
    if not trusted_translation and verdict in {Verdict.DETERMINISTIC, Verdict.NONDETERMINISTIC}:
        report.annotations.append({
            "kind": "model_only_result", "model_verdict": verdict.value,
            "reason": "Translation to the analyzed model is not confirmed",
        })
        return Verdict.INCONCLUSIVE
    return verdict
