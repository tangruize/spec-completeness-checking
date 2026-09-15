"""Offset-preserving masking of Rust comments and literals."""
from __future__ import annotations

import re


class LexicalError(ValueError):
    def __init__(self, message: str, offset: int):
        super().__init__(message)
        self.offset = offset


_CHAR_LITERAL = re.compile(r"'(?:\\(?:u\{[0-9A-Fa-f_]+\}|x[0-9A-Fa-f]{2}|.)|[^'\\\r\n])'")
_RAW_START = re.compile(r'r(#+)?"')


def mask_noncode(text: str) -> str:
    """Mask nested comments and literals while preserving offsets/newlines.

    Lifetimes are not character literals. Literal contents are scanned before
    looking for comment delimiters, so a quoted ``//`` cannot hide later code.
    """
    out = list(text)
    index = 0
    size = len(text)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if out[position] not in "\r\n":
                out[position] = " "

    while index < size:
        start = index
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            index = size if end < 0 else end
        elif text.startswith("/*", index):
            depth = 1
            index += 2
            while index < size and depth:
                if text.startswith("/*", index):
                    depth += 1
                    index += 2
                elif text.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise LexicalError("Unterminated block comment", start)
        elif text[index] == "r" and (match := _RAW_START.match(text, index)):
            close = '"' + (match.group(1) or "")
            end = text.find(close, match.end())
            if end < 0:
                raise LexicalError("Unterminated raw string", start)
            index = end + len(close)
        elif text[index] == '"':
            index += 1
            while index < size:
                if text[index] == "\\":
                    index += 2
                elif text[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            else:
                raise LexicalError("Unterminated string", start)
        elif text[index] == "'" and (match := _CHAR_LITERAL.match(text, index)):
            index = match.end()
        else:
            index += 1
            continue
        blank(start, index)
    return "".join(out)
