"""Select one fixed Verus postcondition VC without importing future scope."""
from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from typing import Iterator

import z3


class UnsupportedTranscript(ValueError):
    """The transcript cannot be replayed as one unambiguous determinism VC."""


@dataclass(frozen=True)
class Command:
    text: str
    head: str
    offset: int
    note: str = ""


@dataclass
class Query:
    function: str
    commands: tuple[Command, ...]
    settings: tuple[Command, ...]
    scope: tuple[int, ...]
    marker: int
    offset: int
    diagnostics: list[str] = field(default_factory=list)

    @property
    def smt2(self) -> str:
        return "\n".join(command.text for command in (*self.settings, *self.commands))

    @property
    def parser_smt2(self) -> str:
        """SMT syntax without process-global Z3 set-option side effects."""
        return "\n".join(
            command.text for command in (*self.settings, *self.commands)
            if command.head != "set-option"
        )


_SETTING_TOKEN = re.compile(r';[^\n]*|"(?:[^"]|"")*"|\|[^|]*\||[()]|[^\s();]+')


def _option_parts(command: Command) -> tuple[str, str]:
    tokens = [
        match.group(0) for match in _SETTING_TOKEN.finditer(command.text)
        if not match.group(0).startswith(";")
    ]
    if (len(tokens) != 5 or tokens[:2] != ["(", "set-option"]
            or tokens[-1] != ")" or not tokens[2].startswith(":")):
        raise UnsupportedTranscript(f"Unsupported solver option syntax: {command.text}")
    return tokens[2][1:], tokens[3]


def _option_value(name: str, value: str, kind: int):
    if kind == z3.Z3_PK_BOOL and value in {"true", "false"}:
        return value == "true"
    if kind == z3.Z3_PK_UINT and re.fullmatch(r"\d+", value):
        number = int(value)
        if number < 2**32:
            return number
    if kind == z3.Z3_PK_DOUBLE:
        try:
            number = float(value)
        except ValueError:
            pass
        else:
            if math.isfinite(number):
                return number
    if kind in {z3.Z3_PK_SYMBOL, z3.Z3_PK_STRING}:
        if value.startswith('"') and value.endswith('"'):
            return value[1:-1].replace('""', '"')
        if value.startswith("|") and value.endswith("|"):
            return value[1:-1]
        return value
    raise UnsupportedTranscript(f"Unsupported value {value!r} for solver option {name!r}")


def apply_solver_options(query: Query, solver: z3.Solver) -> bool | None:
    """Apply supported settings locally, never via global SMT set-option."""
    descriptions = solver.param_descrs()
    available = {
        descriptions.get_name(index) for index in range(descriptions.size())
    }
    aliases = {
        "produce-models": "model",
        "produce-proofs": "proof",
        "produce-unsat-cores": "unsat_core",
    }
    pattern_inference = None
    for command in query.settings:
        if command.head != "set-option":
            continue
        name, raw_value = _option_parts(command)
        if name == "pi.enabled":
            pattern_inference = _option_value(name, raw_value, z3.Z3_PK_BOOL)
            continue
        if name in {"print-success", "global-decls", "global_decls"}:
            value = _option_value(name, raw_value, z3.Z3_PK_BOOL)
            if name != "print-success" and value:
                raise UnsupportedTranscript("Global declarations across push/pop are unsupported")
            continue
        candidates = (
            aliases.get(name, name), name.removeprefix("smt."),
            name.removeprefix("rewriter."),
        )
        local_name = next((candidate for candidate in candidates if candidate in available), None)
        if local_name is None:
            raise UnsupportedTranscript(f"Solver option {name!r} has no solver-local equivalent")
        value = _option_value(name, raw_value, descriptions.get_kind(local_name))
        solver.set(local_name, value)
    return pattern_inference


def validate_pattern_inference(
    query: Query, solver: z3.Solver, requested: bool | None,
) -> None:
    """Guard Z3's global-only inference option without changing global defaults."""
    if requested is None or requested == (z3.get_param("pi.enabled") == "true"):
        return
    # Verus supplies explicit triggers. These do not require automatic pattern
    # inference; an unannotated quantifier needs a setting we cannot isolate
    # through Z3's solver-local API, so reject it rather than silently changing it.
    stack = list(solver.assertions())
    visited: set[int] = set()
    while stack:
        expression = stack.pop()
        if expression.get_id() in visited:
            continue
        visited.add(expression.get_id())
        if z3.is_quantifier(expression):
            if not expression.is_lambda() and expression.num_patterns() == 0:
                raise UnsupportedTranscript(
                    "Global-only pi.enabled differs from this process's setting, "
                    "and the query contains an unannotated quantifier"
                )
            stack.append(expression.body())
        else:
            stack.extend(expression.children())
    query.diagnostics.append(
        "Kept explicit quantifier triggers without changing global-only pi.enabled"
    )


_FUNCTION_MARKER = re.compile(r"^;+\s*Function-([\w-]+)\s+(\S+)")
_DECLARATION = re.compile(
    r"^\(\s*(?:declare-const|declare-fun)\s+(\|[^|]*\||[^\s()]+)"
)
_LOCATION_LABEL = re.compile(r"^%%location_label%%\d+$")


def _events(text: str) -> Iterator[tuple[str, str, int]]:
    """Yield balanced top-level commands and comments, respecting SMT quoting."""
    pos = 0
    size = len(text)
    while pos < size:
        if text[pos].isspace():
            pos += 1
            continue
        if text[pos] == ";":
            end = text.find("\n", pos)
            if end < 0:
                end = size
            yield "comment", text[pos:end], pos
            pos = end
            continue
        if text[pos] != "(":
            raise UnsupportedTranscript(f"Unexpected SMT text at offset {pos}")
        start = pos
        depth = 0
        quote = ""
        while pos < size:
            char = text[pos]
            if quote:
                if char == quote:
                    if quote == '"' and pos + 1 < size and text[pos + 1] == '"':
                        pos += 2
                        continue
                    quote = ""
                pos += 1
                continue
            if char in ('"', "|"):
                quote = char
            elif char == ";":
                end = text.find("\n", pos)
                pos = size if end < 0 else end
                continue
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    pos += 1
                    yield "command", text[start:pos], start
                    break
            pos += 1
        else:
            raise UnsupportedTranscript(f"Unbalanced SMT command at offset {start}")


def declaration_name(command: Command) -> str | None:
    match = _DECLARATION.match(command.text)
    return match.group(1).strip("|") if match else None


def _matches(function: str, fn_name: str, crate_name: str) -> bool:
    if "::" in fn_name:
        return function in {fn_name, f"{crate_name}::{fn_name}"}
    parts = function.split("::")
    if parts[-1] != fn_name:
        return False
    return not crate_name or len(parts) == 1 or parts[0] in {
        crate_name, crate_name.replace("-", "_"),
    }


def _scope_count(command: Command) -> int:
    match = re.fullmatch(r"\(\s*(?:push|pop)\s*(\d+)?\s*\)", command.text)
    if not match:
        raise UnsupportedTranscript(f"Unsupported scope command: {command.text}")
    return int(match.group(1) or "1")


def _diagnostic_recheck(initial: Query, current: Query) -> bool:
    if initial.scope != current.scope or initial.marker != current.marker:
        return False
    before = tuple(command.text for command in initial.commands)
    after = tuple(command.text for command in current.commands)
    if after[:len(before)] != before:
        return False
    labels = {
        name for command in initial.commands
        if (name := declaration_name(command)) and _LOCATION_LABEL.fullmatch(name)
    }
    if len(labels) != 1:
        return False
    label = re.escape(next(iter(labels)))
    blocker = re.compile(rf"\(\s*assert\s*\(\s*not\s+{label}\s*\)\s*\)")
    return all(blocker.fullmatch(extra) for extra in after[len(before):])


def select_query(text: str, fn_name: str, crate_name: str) -> Query:
    """Snapshot effective SMT state at the target's first fixed-goal query.

    A Verus error-reporting recheck that only blocks the same sole location
    label is recognizable and ignored. Multiple independent VCs, additional
    proof assertions and unrecognized control commands are unsupported.
    """
    frames: list[list[Command]] = [[]]
    scope_ids = [0]
    next_scope = 0
    settings: dict[str, Command] = {}
    current_function: str | None = None
    marker = 0
    comments: list[str] = []
    selected: Query | None = None
    for kind, value, offset in _events(text):
        if kind == "comment":
            comments.append(value)
            match = _FUNCTION_MARKER.match(value)
            if match:
                marker += 1
                current_function = match.group(2) if match.group(1) == "Def" else None
            continue
        match = re.match(r"\(\s*([^\s()]+)", value)
        if match is None:
            raise UnsupportedTranscript(f"Empty SMT command at offset {offset}")
        command = Command(value, match.group(1), offset, "\n".join(comments))
        comments.clear()
        head = command.head
        if head == "push":
            for _ in range(_scope_count(command)):
                next_scope += 1
                frames.append([])
                scope_ids.append(next_scope)
        elif head == "pop":
            count = _scope_count(command)
            if count >= len(frames):
                raise UnsupportedTranscript(f"Unbalanced pop at offset {offset}")
            if count:
                del frames[-count:]
                del scope_ids[-count:]
        elif head in {"set-option", "set-logic", "set-info"}:
            option = re.match(r"\(\s*\S+\s+([^\s()]+)", value)
            if option is None:
                raise UnsupportedTranscript(f"Invalid solver setting at offset {offset}")
            if re.search(r":(?:global-decls|global_decls)\s+true\b", value):
                raise UnsupportedTranscript("Global declarations across push/pop are unsupported")
            settings[f"{head}:{option.group(1)}"] = command
        elif head in {"check-sat", "check-sat-assuming", "check-sat-using"}:
            if current_function is None or not _matches(current_function, fn_name, crate_name):
                continue
            if head != "check-sat" or not re.fullmatch(r"\(\s*check-sat\s*\)", value):
                raise UnsupportedTranscript(f"Unsupported target check: {value}")
            query = Query(
                function=current_function,
                commands=tuple(command for frame in frames for command in frame),
                settings=tuple(settings.values()),
                scope=tuple(scope_ids),
                marker=marker,
                offset=offset,
            )
            if selected is not None:
                if not _diagnostic_recheck(selected, query):
                    raise UnsupportedTranscript(
                        f"Multiple target VCs for {fn_name}; an explicit robust selector is required"
                    )
                selected.diagnostics.append(
                    f"Ignored diagnostic recheck of the same postcondition at offset {offset}"
                )
            else:
                selected = query
        elif head.startswith("get-") or head in {"echo", "exit"}:
            continue
        elif head == "assert" or head.startswith(("declare-", "define-")):
            frames[-1].append(command)
        else:
            raise UnsupportedTranscript(f"Unsupported SMT command {head!r} at offset {offset}")
    if len(frames) != 1:
        raise UnsupportedTranscript("Unclosed SMT push scope")
    if selected is None:
        raise UnsupportedTranscript(
            f"No Function-Def query for {crate_name}::{fn_name}"
        )
    return selected


def validate_fixed_goal(query: Query, solver: z3.Solver, fn_name: str) -> None:
    """Require the one labeled postcondition of the generated equality check."""
    labels = [
        (name, command.note)
        for command in query.commands
        if (name := declaration_name(command)) and _LOCATION_LABEL.fullmatch(name)
    ]
    if len(labels) != 1 or "postcondition not satisfied" not in labels[0][1]:
        raise UnsupportedTranscript(
            "Expected one generated postcondition VC; arbitrary proof assertions, "
            "multiple location labels and unlabeled queries are unsupported"
        )
    label = labels[0][0]
    goals: list[z3.ExprRef] = []
    stack = list(solver.assertions())
    visited: set[int] = set()
    while stack:
        expression = stack.pop()
        identity = expression.get_id()
        if identity in visited:
            continue
        visited.add(identity)
        if z3.is_implies(expression):
            premise = expression.arg(0)
            if z3.is_const(premise) and str(premise.decl().name()) == label:
                goals.append(expression.arg(1))
        if z3.is_quantifier(expression):
            stack.append(expression.body())
        else:
            stack.extend(expression.children())
    if len(goals) != 1:
        raise UnsupportedTranscript("Cannot isolate the generated postcondition goal")
    goal = goals[0]
    while z3.is_implies(goal):
        goal = goal.arg(1)
    expected = fn_name.rsplit("::", 1)[-1] + "_equal"
    name_pattern = rf"(?:^|[!:.]){re.escape(expected)}(?:\.\??)?$"
    if (not z3.is_app(goal)
            or not re.search(name_pattern, str(goal.decl().name()))):
        raise UnsupportedTranscript(
            f"The target postcondition is not the generated {expected} equality goal"
        )
