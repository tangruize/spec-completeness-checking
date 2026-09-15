from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .models import Stage
from .proposals import StageCapability

CAPABILITY_VERSION = 1

DEFAULT_CAPABILITIES: Mapping[Stage, StageCapability] = MappingProxyType({
    Stage.CONFIG: StageCapability(
        Stage.CONFIG, ("project_profile",), ("profile_gap",),
        "Propose a supported project profile, not tool installation or execution.",
    ),
    Stage.DISCOVER: StageCapability(
        Stage.DISCOVER, ("target_candidates", "normalization"),
        ("discovery_gap", "partial_discovery", "unsupported_syntax"),
        "Suggest source-backed targets; do not claim complete discovery.",
    ),
    Stage.PREPARE: StageCapability(
        Stage.PREPARE, ("preparation", "normalization", "project_profile"),
        ("preparation_gap", "unsupported_syntax", "profile_gap"),
        "Propose checked workspace transformations, never arbitrary scripts.",
    ),
    Stage.EXTRACT: StageCapability(
        Stage.EXTRACT, ("extraction", "type_model", "normalization"),
        ("unsupported_syntax", "extraction_gap", "type_gap"),
        "Extract the existing contract faithfully; do not invent a new contract.",
    ),
    Stage.OBSERVATIONS: StageCapability(
        Stage.OBSERVATIONS, ("observation", "equality_policy", "type_model"),
        ("observation_gap", "input_view_gap", "type_gap", "unsupported_dimension"),
        "Changed observations require semantic validation and explicit adoption.",
    ),
    Stage.LOWER: StageCapability(
        Stage.LOWER, ("obligation", "normalization"),
        ("lowering_gap", "unsupported_syntax", "unsupported_dimension", "type_gap"),
        "Preserve the frozen problem; compiling a translation is not fidelity.",
    ),
    Stage.PROOF_GENERATION: StageCapability(
        Stage.PROOF_GENERATION, ("proof",),
        ("no_candidate", "unproved", "unknown", "proof_generation_gap"),
        "Generate a proof candidate for separate mechanical proof checking.",
    ),
    Stage.PROOF_CHECKING: StageCapability(
        Stage.PROOF_CHECKING, ("diagnostic",),
        ("unproved", "unknown", "proof_check_failed"),
        "Explain diagnostics only; failed proofs normally route to generation.",
    ),
    Stage.QUERY: StageCapability(
        Stage.QUERY, ("diagnostic", "search_plan"),
        ("unknown", "unsupported_dimension", "query_gap"),
        "Suggest diagnostics or search, never fabricate solver evidence.",
    ),
    Stage.SEARCH: StageCapability(
        Stage.SEARCH, ("search_plan", "proof", "observation"),
        ("search_stalled", "unsupported_dimension", "unknown",
         "no_candidate", "observation_gap"),
        "Propose candidates that still require checking against the same problem.",
    ),
    Stage.COUNTEREXAMPLE: StageCapability(
        Stage.COUNTEREXAMPLE, ("counterexample",),
        ("counterexample_gap", "unsupported_witness"),
        "Propose concrete input and output constructors; only original-source replay can confirm them.",
    ),
    Stage.REPORT: StageCapability(
        Stage.REPORT, ("explanation",), ("explanation_gap",),
        "Append attributed explanations without changing evidence or verdicts.",
    ),
})


def capability_for(stage: Stage | str) -> StageCapability:
    return DEFAULT_CAPABILITIES[Stage(stage)]


def is_assistance_trigger(stage: Stage | str, code: str) -> bool:
    """Test an explicit gap code, not arbitrary exceptions or operational errors."""
    return code in capability_for(stage).triggers


def assistance_route(stage: Stage | str, code: str) -> Stage | None:
    stage = Stage(stage)
    if stage is Stage.PROOF_CHECKING and code in {"unproved", "unknown", "proof_check_failed"}:
        return Stage.PROOF_GENERATION
    return stage if is_assistance_trigger(stage, code) else None
