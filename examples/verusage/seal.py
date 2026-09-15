"""Generate provenance metadata; never copy, normalize, or rewrite source code."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path

from .provenance import (
    FIXTURE_ROOT, MANIFEST_PATH, TOOL_ROOT, contained_path, functions,
    selected_function, sha256, span, span_bytes,
)


def capture_fixture(entry: dict, corpus: Path) -> dict:
    original = contained_path(corpus, entry["source"]).read_bytes()
    fixture = contained_path(FIXTURE_ROOT, entry["path"]).read_bytes()
    classification = entry["classification"]
    if classification == "full_source" and fixture != original:
        raise ValueError(f"{entry['path']}: full-source fixture was modified")
    source_lines = original.splitlines(keepends=True)
    declared_lines = entry.get("copied_source_lines", [[1, len(source_lines)]])
    retained = []
    covered_lines = set()
    for first, last in declared_lines:
        if not 1 <= first <= last <= len(source_lines):
            raise ValueError(f"{entry['path']}: invalid source line range {first}:{last}")
        start = sum(map(len, source_lines[:first - 1]))
        end = start + sum(map(len, source_lines[first - 1:last]))
        copied = original[start:end]
        offset = fixture.find(copied)
        if offset < 0 or fixture.find(copied, offset + 1) >= 0:
            raise ValueError(f"{entry['path']}: copied lines {first}:{last} not uniquely preserved")
        retained.append({
            "source": span(original, start, end),
            "fixture": span(fixture, offset, offset + len(copied)),
        })
        covered_lines.update(range(first, last + 1))
    omitted = []
    first = None
    for line in range(1, len(source_lines) + 2):
        if line <= len(source_lines) and line not in covered_lines:
            first = line if first is None else first
        elif first is not None:
            omitted.append([first, line - 1])
            first = None
    edits = []
    matcher = difflib.SequenceMatcher(
        a=source_lines, b=fixture.splitlines(keepends=True), autojunk=False,
    )
    for operation, a, b, c, d in matcher.get_opcodes():
        if operation == "equal":
            continue
        replacement = b"".join(matcher.b[c:d])
        edits.append({
            "operation": operation,
            "source_lines_zero_based_half_open": [a, b],
            "fixture_lines_zero_based_half_open": [c, d],
            "original_sha256": sha256(b"".join(source_lines[a:b])),
            "replacement": replacement.decode("utf-8"),
            "replacement_sha256": sha256(replacement),
        })
    spec_functions = []
    original_functions = functions(original)
    for item in functions(fixture):
        if item["mode"] != "spec":
            continue
        matches = [
            candidate for candidate in original_functions
            if candidate["qualified_name"] == item["qualified_name"]
            and span_bytes(original, candidate["span"]) == span_bytes(fixture, item["span"])
        ]
        if len(matches) != 1:
            raise ValueError(f"{entry['path']}: changed or ambiguous spec function {item['qualified_name']}")
        spec_functions.append({
            "qualified_name": item["qualified_name"],
            "source": matches[0]["span"],
            "fixture": item["span"],
            "is_view": item["name"] == "view",
            "originally_external_body": matches[0]["external_body"],
        })
    return {
        **entry,
        "full_source_sha256": sha256(original),
        "full_source_bytes": len(original),
        "full_source_lines": len(source_lines),
        "fixture_sha256": sha256(fixture),
        "fixture_bytes": len(fixture),
        "copied_spans": retained,
        "omitted_source_lines": omitted,
        "declared_edits": edits,
        "copied_spec_functions": spec_functions,
        "dependencies": entry.get("dependencies", ["The complete original source and all its definitions/imports"]),
        "transformations": entry.get("transformations", []),
        "omissions": entry.get("omissions", []),
    }


def capture(corpus: Path) -> dict:
    selection_path = TOOL_ROOT / "examples/verusage/selection.json"
    selection_bytes = selection_path.read_bytes()
    manifest = json.loads(selection_bytes)
    manifest["selection_sha256"] = sha256(selection_bytes)
    manifest["claim_scope"] = (
        "Contract determinism under the backend's explicit observations. Even full-source fixtures "
        "do not claim that the original implementations have been verified. Inconclusive queries, "
        "proof/spec discovery exclusions, absent input Views, and infrastructure failures do not "
        "count as successful abstract proofs."
    )
    manifest["fixtures"] = [capture_fixture(entry, corpus) for entry in manifest["fixtures"]]
    fixtures = {entry["path"]: entry for entry in manifest["fixtures"]}
    for case in manifest["cases"]:
        entry = fixtures[case["fixture"]]
        original = contained_path(corpus, entry["source"]).read_bytes()
        fixture = contained_path(FIXTURE_ROOT, case["fixture"]).read_bytes()
        source_target = selected_function(original, case)
        fixture_target = selected_function(fixture, case)
        if source_target["header"] != fixture_target["header"]:
            raise ValueError(f"{case['id']}: signature/requires/ensures were changed")
        changed_body = (
            span_bytes(original, source_target["span"])
            != span_bytes(fixture, fixture_target["span"])
        )
        if changed_body and entry["classification"] != "contract_only":
            raise ValueError(f"{case['id']}: undeclared target body change")
        if changed_body and not fixture_target["external_body"]:
            raise ValueError(f"{case['id']}: a stub must be explicitly external_body")
        case["repo"] = case["fixture"].split("/")[0]
        case["source"] = entry["source"]
        case["source_sha256"] = entry["full_source_sha256"]
        case["fixture_sha256"] = entry["fixture_sha256"]
        case["original"] = source_target
        case["selected"] = fixture_target
        case["implementation_body_changed"] = changed_body
        case["selected_original_snippet"] = source_target["header"]
        case["selector"] = (
            f"{Path(case['fixture']).name}:{case['function']}@"
            f"{fixture_target['span']['start_line']}"
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="Read-only original source-projects directory")
    parser.add_argument("--check", action="store_true", help="Compare with the sealed manifest without writing")
    args = parser.parse_args()
    rendered = (json.dumps(capture(args.corpus.resolve()), indent=2, ensure_ascii=False) + "\n").encode()
    if args.check:
        if MANIFEST_PATH.read_bytes() != rendered:
            raise SystemExit("Provenance differs from the sealed manifest")
    else:
        MANIFEST_PATH.write_bytes(rendered)
    print(f"{'Checked' if args.check else 'Sealed'} 27 targets from 9 repositories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
