# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal, fail-closed YAML subset loader for CI workflow policy checks.

PADOS does not depend on PyYAML. The CI authority boundary tests only need to
read the workflow files PADOS itself authors, so this loader accepts a strict
subset: block mappings and sequences, single-line flow collections, plain and
quoted scalars, and literal/folded block scalars. Anything else (anchors,
aliases, tags, multi-document streams, complex keys, tab indentation, duplicate
keys, multi-line flow or plain scalars) raises ``YamlSubsetError``; an
unparseable workflow is a policy failure, never a silent pass.

Keys are always strings (``on`` stays ``"on"``). Plain scalars resolve to
``bool`` (true/false), ``None`` (null, ~, empty), ``int`` or ``str``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class YamlSubsetError(ValueError):
    def __init__(self, code: str, line: int) -> None:
        super().__init__(f"{code}:line {line}")
        self.code = code
        self.line = line


@dataclass
class _Line:
    number: int
    indent: int
    text: str  # content without indentation, comment stripped, right-stripped


_INT = re.compile(r"-?[0-9]+")
_BLOCK_HEADER = re.compile(r"([|>])([+-]?)")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "/": "/", "0": "\0"}


def _strip_comment(text: str, number: int) -> str:
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if quote is not None:
            if char == quote:
                if quote == "'" and text[index + 1:index + 2] == "'":
                    index += 2
                    continue
                quote = None
        elif char in "'\"" and (index == 0 or text[index - 1] in " \t[{,:-"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
        index += 1
    if quote is not None:
        raise YamlSubsetError("UNTERMINATED_QUOTE", number)
    return text.rstrip()


def _double_quoted(body: str, number: int) -> str:
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            escape = body[index + 1:index + 2]
            if escape not in _ESCAPES:
                raise YamlSubsetError("UNSUPPORTED_ESCAPE", number)
            out.append(_ESCAPES[escape])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _plain(raw: str, number: int) -> Any:
    if not raw or raw in {"null", "~", "Null", "NULL"}:
        return None
    if raw in {"true", "True", "TRUE"}:
        return True
    if raw in {"false", "False", "FALSE"}:
        return False
    if raw[0] in "&*!%@`|>" or raw.startswith(("? ", "- ")) or raw in {"?", "-"}:
        raise YamlSubsetError("UNSUPPORTED_SCALAR", number)
    if _INT.fullmatch(raw):
        return int(raw)
    return raw


class _Flow:
    """Single-line flow collection parser (``{a: b, c: [d, e]}``)."""

    def __init__(self, text: str, number: int) -> None:
        self.text = text
        self.pos = 0
        self.number = number

    def fail(self, code: str) -> YamlSubsetError:
        return YamlSubsetError(code, self.number)

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] == " ":
            self.pos += 1

    def parse(self) -> Any:
        value = self.value(stops="")
        self.skip()
        if self.pos != len(self.text):
            raise self.fail("TRAILING_FLOW_CONTENT")
        return value

    def value(self, stops: str) -> Any:
        self.skip()
        if self.pos >= len(self.text):
            raise self.fail("UNTERMINATED_FLOW")
        char = self.text[self.pos]
        if char == "{":
            return self.mapping()
        if char == "[":
            return self.sequence()
        if char in "'\"":
            return self.quoted()
        start = self.pos
        while self.pos < len(self.text):
            here = self.text[self.pos]
            if here in "{}[]" or (here in stops and here != ":"):
                break
            if here == ":" and ":" in stops and self.text[self.pos + 1:self.pos + 2] in {" ", ",", "}", ""}:
                break
            self.pos += 1
        return _plain(self.text[start:self.pos].strip(), self.number)

    def quoted(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if quote == '"' and char == "\\":
                out.append(self.text[self.pos:self.pos + 2])
                self.pos += 2
                continue
            if char == quote:
                if quote == "'" and self.text[self.pos + 1:self.pos + 2] == "'":
                    out.append("'")
                    self.pos += 2
                    continue
                self.pos += 1
                body = "".join(out)
                return _double_quoted(body, self.number) if quote == '"' else body
            out.append(char)
            self.pos += 1
        raise self.fail("UNTERMINATED_QUOTE")

    def mapping(self) -> dict[str, Any]:
        self.pos += 1
        result: dict[str, Any] = {}
        while True:
            self.skip()
            if self.text[self.pos:self.pos + 1] == "}":
                self.pos += 1
                return result
            key = self.value(stops=":,}")
            if isinstance(key, int) and not isinstance(key, bool):
                key = str(key)
            if not isinstance(key, str):
                raise self.fail("UNSUPPORTED_KEY")
            self.skip()
            if self.text[self.pos:self.pos + 1] != ":":
                raise self.fail("FLOW_MAPPING_MISSING_COLON")
            self.pos += 1
            if key in result:
                raise self.fail("DUPLICATE_KEY")
            result[key] = self.value(stops=",}")
            self.skip()
            if self.text[self.pos:self.pos + 1] == ",":
                self.pos += 1
            elif self.text[self.pos:self.pos + 1] != "}":
                raise self.fail("UNTERMINATED_FLOW")

    def sequence(self) -> list[Any]:
        self.pos += 1
        result: list[Any] = []
        while True:
            self.skip()
            if self.text[self.pos:self.pos + 1] == "]":
                self.pos += 1
                return result
            result.append(self.value(stops=",]"))
            self.skip()
            if self.text[self.pos:self.pos + 1] == ",":
                self.pos += 1
            elif self.text[self.pos:self.pos + 1] != "]":
                raise self.fail("UNTERMINATED_FLOW")


def _scalar(text: str, number: int) -> Any:
    if text[0] in "{[":
        return _Flow(text, number).parse()
    if text[0] in "'\"":
        flow = _Flow(text, number)
        value = flow.quoted()
        if flow.pos != len(text):
            raise YamlSubsetError("TRAILING_SCALAR_CONTENT", number)
        return value
    return _plain(text, number)


def _split_key(text: str, number: int) -> tuple[str, str] | None:
    """Return (key, rest) when ``text`` is a ``key: value`` mapping entry."""
    if text[0] in "'\"":
        flow = _Flow(text, number)
        key = flow.quoted()
        rest = text[flow.pos:]
        if rest == ":" or rest.startswith(": "):
            return key, rest[1:].strip()
        return None
    if text[0] in "{[":
        return None
    match = re.match(r"([^:]*?):(?: (.*)|$)", text)
    if match is None:
        return None
    key = match.group(1)
    if not key or key != key.strip() or key[0] in "&*!%@`?-":
        raise YamlSubsetError("UNSUPPORTED_KEY", number)
    return key, (match.group(2) or "").strip()


class _Parser:
    def __init__(self, source: str) -> None:
        self.raw = source.split("\n")
        self.lines: list[_Line | None] = []
        for number, raw in enumerate(self.raw, start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                raise YamlSubsetError("TAB_INDENTATION", number)
            stripped = raw.lstrip(" ")
            self.lines.append(_Line(number, len(raw) - len(stripped), stripped.rstrip()) if stripped.strip() else None)
        self.cleaned: dict[int, str] = {}

    def text(self, index: int) -> str:
        line = self.lines[index]
        assert line is not None
        if index not in self.cleaned:
            self.cleaned[index] = _strip_comment(line.text, line.number)
        return self.cleaned[index]

    def next_index(self, index: int) -> int:
        while index < len(self.lines) and (self.lines[index] is None or not self.text(index)):
            index += 1
        return index

    def document(self) -> Any:
        index = self.next_index(0)
        if index < len(self.lines) and self.text(index) in {"---", "..."}:
            raise YamlSubsetError("DOCUMENT_MARKER_UNSUPPORTED", self._number(index))
        if index >= len(self.lines):
            return None
        first = self.lines[index]
        assert first is not None
        if first.indent != 0:
            raise YamlSubsetError("UNEXPECTED_INDENT", first.number)
        value, index = self.block(index, 0)
        index = self.next_index(index)
        if index < len(self.lines):
            raise YamlSubsetError("UNEXPECTED_CONTENT", self._number(index))
        return value

    def _number(self, index: int) -> int:
        line = self.lines[index]
        return line.number if line is not None else index + 1

    def block(self, index: int, indent: int) -> tuple[Any, int]:
        line = self.lines[index]
        assert line is not None
        text = self.text(index)
        if text in {"---", "..."} or text.startswith(("--- ", "... ")):
            raise YamlSubsetError("DOCUMENT_MARKER_UNSUPPORTED", line.number)
        if text == "-" or text.startswith("- "):
            return self.sequence(index, indent)
        if _split_key(text, line.number) is not None:
            return self.mapping(index, indent)
        value = _scalar(text, line.number)
        nxt = self.next_index(index + 1)
        if nxt < len(self.lines) and (follow := self.lines[nxt]) is not None and follow.indent > indent:
            raise YamlSubsetError("MULTILINE_SCALAR_UNSUPPORTED", follow.number)
        return value, index + 1

    def child(self, index: int, parent_indent: int, allow_same_indent_sequence: bool) -> tuple[Any, int]:
        nxt = self.next_index(index)
        if nxt >= len(self.lines):
            return None, nxt
        line = self.lines[nxt]
        assert line is not None
        text = self.text(nxt)
        if line.indent > parent_indent:
            return self.block(nxt, line.indent)
        if allow_same_indent_sequence and line.indent == parent_indent and (text == "-" or text.startswith("- ")):
            return self.sequence(nxt, parent_indent)
        return None, nxt

    def block_scalar(self, index: int, header: str, parent_indent: int, number: int) -> tuple[str, int]:
        match = _BLOCK_HEADER.fullmatch(header)
        if match is None:
            raise YamlSubsetError("UNSUPPORTED_BLOCK_SCALAR", number)
        style, chomp = match.groups()
        if chomp == "+":
            raise YamlSubsetError("UNSUPPORTED_BLOCK_SCALAR", number)
        body: list[str] = []
        content_indent: int | None = None
        while index < len(self.raw):
            raw = self.raw[index]
            if raw.strip():
                current = len(raw) - len(raw.lstrip(" "))
                if current <= parent_indent:
                    break
                if content_indent is None:
                    content_indent = current
                if current < content_indent:
                    raise YamlSubsetError("BLOCK_SCALAR_INDENT", index + 1)
                body.append(raw[content_indent:].rstrip("\r"))
            else:
                body.append("")
            index += 1
        while body and not body[-1]:
            body.pop()
            index -= 1
        if style == "|":
            value = "\n".join(body)
        else:
            value = "\n".join(" ".join(part.split("\n")) for part in "\n".join(body).split("\n\n"))
        if chomp == "-":
            return value, index
        return value + "\n" if value else value, index

    def mapping(self, index: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while index < len(self.lines):
            index = self.next_index(index)
            if index >= len(self.lines):
                break
            line = self.lines[index]
            assert line is not None
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlSubsetError("UNEXPECTED_INDENT", line.number)
            text = self.text(index)
            split = _split_key(text, line.number)
            if split is None:
                if text == "-" or text.startswith("- "):
                    break
                raise YamlSubsetError("EXPECTED_MAPPING_ENTRY", line.number)
            key, rest = split
            if key in result:
                raise YamlSubsetError("DUPLICATE_KEY", line.number)
            if not rest:
                result[key], index = self.child(index + 1, indent, allow_same_indent_sequence=True)
            elif rest[0] in "|>":
                result[key], index = self.block_scalar(index + 1, rest, indent, line.number)
            else:
                result[key] = _scalar(rest, line.number)
                nxt = self.next_index(index + 1)
                if nxt < len(self.lines) and (follow := self.lines[nxt]) is not None and follow.indent > indent:
                    raise YamlSubsetError("MULTILINE_SCALAR_UNSUPPORTED", follow.number)
                index += 1
        return result, index

    def sequence(self, index: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while index < len(self.lines):
            index = self.next_index(index)
            if index >= len(self.lines):
                break
            line = self.lines[index]
            assert line is not None
            text = self.text(index)
            if line.indent < indent or (line.indent == indent and not (text == "-" or text.startswith("- "))):
                break
            if line.indent > indent:
                raise YamlSubsetError("UNEXPECTED_INDENT", line.number)
            if text == "-":
                item, index = self.child(index + 1, indent, allow_same_indent_sequence=False)
                result.append(item)
                continue
            content = text[2:]
            offset = 2 + len(content) - len(content.lstrip(" "))
            content = content.lstrip(" ")
            if content[0] in "|>":
                item, index = self.block_scalar(index + 1, content, indent, line.number)
                result.append(item)
                continue
            # Re-read the item content as a line at its own column (compact nested block).
            self.lines[index] = _Line(line.number, indent + offset, content)
            self.cleaned[index] = content
            item, index = self.block(index, indent + offset)
            result.append(item)
        return result, index


def load(source: str) -> Any:
    """Parse ``source`` (the subset described in the module docstring)."""
    if source.startswith("\ufeff"):
        source = source[1:]
    return _Parser(source.replace("\r\n", "\n")).document()
