"""Schema-driven determinism search.

One caller-managed Verus compilation produces a guarded template.
Subsequent rounds use Z3 assumptions against the same fixed global goal.
UNKNOWN candidates remain separate from satisfiable witness evidence.

Public API:
    - enumerate_schemas(det_spec) -> list[SchemaBinding]
    - render_guarded_template(det_spec, schemas) -> str
    - translate_assume(assume, schemas) -> (schema_id, k_bindings) | None
    - build_schema_ctx(smt2_path, fn_name, schemas, crate_name) -> SchemaCtx
    - SchemaCtx.check(active_bindings=None, *, timeout_ms, seed) -> raw query dict
    - run_schema_search(det_spec, schema_ctx, *, max_rounds, timeout_ms, seed) -> Witness
"""
from .schemas import (
    SchemaBinding, SchemaKind,
    enumerate_schemas, render_guarded_template, translate_assume,
)
from .search import (
    MissingSchemaBinding, SchemaCtx, SchemaSearchContext, UnsupportedPredicate,
    UnsupportedTranscript,
    run_schema_search, build_schema_ctx,
)

__all__ = [
    "SchemaBinding", "SchemaKind",
    "enumerate_schemas", "render_guarded_template", "translate_assume",
    "SchemaCtx", "SchemaSearchContext", "run_schema_search", "build_schema_ctx",
    "MissingSchemaBinding", "UnsupportedTranscript",
    "UnsupportedPredicate",
]
