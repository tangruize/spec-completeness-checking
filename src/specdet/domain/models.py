from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import TypeAlias, Union

JsonValue: TypeAlias = Union[
    None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]
]
JsonObject: TypeAlias = dict[str, JsonValue]
SCHEMA_VERSION = 1


def json_value(value: object) -> JsonValue:
    """Serialize only explicit JSON-compatible values and dataclass records."""
    if isinstance(value, Enum):
        return json_value(value.value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite numbers are not valid artifact values")
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Artifact object keys must be strings")
        return {key: json_value(item) for key, item in value.items()}
    raise TypeError(f"Unsupported artifact value: {type(value).__name__}")


def as_object(value: object) -> JsonObject:
    result = json_value(value)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def canonical_json(value: object) -> str:
    return json.dumps(
        json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_text(data: JsonObject, key: str, *, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


class Stage(str, Enum):
    CONFIG = "config"
    DISCOVER = "discover"
    PREPARE = "prepare"
    EXTRACT = "extract"
    OBSERVATIONS = "observations"
    LOWER = "lower"
    PROOF_GENERATION = "proof_generation"
    PROOF_CHECKING = "proof_checking"
    QUERY = "query"
    SEARCH = "search"
    COUNTEREXAMPLE = "counterexample"
    REPORT = "report"


class SolverStatus(str, Enum):
    SAT = "sat"
    UNSAT = "unsat"
    UNKNOWN = "unknown"
    NOT_RUN = "not_run"
    UNSUPPORTED = "unsupported"


class Verdict(str, Enum):
    DETERMINISTIC = "deterministic"
    NONDETERMINISTIC = "nondeterministic"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class Diagnostic:
    stage: Stage
    code: str
    message: str
    severity: str = "error"
    details: JsonObject = field(default_factory=dict)


class StageError(Exception):
    def __init__(self, diagnostic: Diagnostic, *, recoverable: bool = False):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
        self.recoverable = recoverable


@dataclass(frozen=True)
class TargetRef:
    language: str
    file: str
    name: str
    line: int
    qualified_name: str = ""
    kind: str = "function"
    source_digest: str = ""
    module: str = ""

    @property
    def id(self) -> str:
        return digest(self)[:24]

    @classmethod
    def from_dict(cls, data: JsonObject) -> TargetRef:
        line = data.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ValueError("Target line must be a positive integer")
        return cls(
            language=require_text(data, "language"),
            file=require_text(data, "file"),
            name=require_text(data, "name"),
            line=line,
            qualified_name=require_text(data, "qualified_name", default=""),
            kind=require_text(data, "kind", default="function"),
            source_digest=require_text(data, "source_digest", default=""),
            module=require_text(data, "module", default=""),
        )


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    path: str
    digest: str
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class Contract:
    target: TargetRef
    source_digest: str
    native: JsonObject
    diagnostics: tuple[Diagnostic, ...] = ()
    trusted_translation: bool = True

    @property
    def id(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class ObservationPlan:
    policy: str
    native: JsonObject
    ignored_dimensions: tuple[str, ...] = ()
    analysis_kind: str = "concrete_determinism"
    coverage: JsonObject = field(default_factory=dict)
    inputs: tuple[JsonObject, ...] = ()

    @property
    def id(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class Obligation:
    problem_id: str
    target: TargetRef
    native: JsonObject
    trusted_translation: bool = True

    @property
    def id(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class ProofCandidate:
    problem_id: str
    obligation_digest: str
    proof: str = ""
    helpers: str = ""
    origin: str = "baseline"
    proposal_id: str = ""

    @property
    def id(self) -> str:
        return digest(self)


@dataclass(frozen=True)
class CheckEvidence:
    problem_id: str
    status: SolverStatus
    role: str
    query_digest: str = ""
    constraints: tuple[JsonObject, ...] = ()
    reason: str = ""
    duration_ms: float = 0
    artifact: str = ""


@dataclass(frozen=True)
class ProofCheckEvidence:
    problem_id: str
    candidate_id: str
    status: str
    verified_goals: int = 0
    duration_ms: float = 0
    diagnostics: tuple[Diagnostic, ...] = ()
    artifact: str = ""
    native: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResult:
    evidence: tuple[CheckEvidence, ...] = ()
    confirmed_constraints: tuple[JsonObject, ...] = ()
    candidate_constraints: tuple[JsonObject, ...] = ()
    rounds: int = 0
    exhausted: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class CounterexampleEvidence:
    problem_id: str
    obligation_digest: str
    status: str
    verified_goals: int
    bindings: JsonObject
    type_arguments: JsonObject
    artifact: str
    certificate_digest: str
    bindings_digest: str
    kind: str = "source_verified_constructive"


@dataclass(frozen=True)
class CounterexampleSearchResult:
    witness: CounterexampleEvidence | None = None
    attempts: int = 0
    exhausted: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class StageOutcome:
    stage: Stage
    status: str
    diagnostics: tuple[Diagnostic, ...] = ()
    input_digest: str = ""
    artifact: ArtifactRef | None = None


@dataclass
class AnalysisReport:
    target: TargetRef
    run_id: str
    problem_id: str = ""
    status: str = "running"
    verdict: Verdict = Verdict.NOT_EVALUATED
    baseline: CheckEvidence | None = None
    proofs: list[ProofCheckEvidence] = field(default_factory=list)
    search: SearchResult | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    annotations: list[JsonObject] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    coverage: JsonObject = field(default_factory=dict)
    assistance: JsonObject = field(default_factory=dict)
    analysis_kind: str = "concrete_determinism"
    concrete_result: JsonObject | None = None
    counterexample: CounterexampleEvidence | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> JsonObject:
        return as_object(self)
