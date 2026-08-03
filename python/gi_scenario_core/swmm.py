"""Line-preserving SWMM input parsing and typed, auditable mutations."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:;.*)?$")


class SwmmParseError(ValueError):
    pass


class SwmmMutationError(ValueError):
    pass


@dataclass(slots=True)
class DataRow:
    section: str
    line_index: int
    raw: str
    tokens: list[str]
    comment: str


@dataclass(slots=True)
class SwmmSection:
    name: str
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SemanticChange:
    command: str
    section: str
    object_id: str
    field: str
    before: str | None
    after: str | None


def split_comment(line: str) -> tuple[str, str]:
    """Split a SWMM line at the first semicolon outside double quotes."""

    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            return line[:index], line[index:]
    return line, ""


def tokenize(line: str) -> tuple[list[str], str]:
    content, comment = split_comment(line)
    if not content.strip():
        return [], comment
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    for char in content:
        if char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            # Backslashes are literal in SWMM/Windows paths, not shell escapes.
            current.append(char)
    if quoted:
        raise SwmmParseError(f"unterminated quote in line {line!r}")
    if current:
        tokens.append("".join(current))
    return tokens, comment


class SwmmDocument:
    """A parsed document that renders byte-for-byte until deliberately changed."""

    def __init__(
        self,
        preamble: list[str],
        sections: list[SwmmSection],
        *,
        newline: str = "\n",
        trailing_newline: bool = True,
        encoding: str = "utf-8",
        bom: bool = False,
    ) -> None:
        self.preamble = preamble
        self.sections = sections
        self.newline = newline
        self.trailing_newline = trailing_newline
        self.encoding = encoding
        self.bom = bom
        self.changes: list[SemanticChange] = []

    @classmethod
    def parse(cls, text: str) -> SwmmDocument:
        bom = text.startswith("\ufeff")
        if bom:
            text = text[1:]
        newline = "\r\n" if "\r\n" in text else "\n"
        trailing = text.endswith(("\n", "\r"))
        raw_lines = text.splitlines()
        preamble: list[str] = []
        sections: list[SwmmSection] = []
        current: SwmmSection | None = None
        seen: set[str] = set()
        for line_number, line in enumerate(raw_lines, start=1):
            match = SECTION_RE.match(line)
            if match:
                name = match.group(1).strip().upper()
                if not name:
                    raise SwmmParseError(f"empty section header on line {line_number}")
                if name in seen:
                    raise SwmmParseError(f"duplicate section [{name}] on line {line_number}")
                seen.add(name)
                current = SwmmSection(name=name, header=line)
                sections.append(current)
            elif current is None:
                preamble.append(line)
            else:
                current.lines.append(line)
        return cls(preamble, sections, newline=newline, trailing_newline=trailing, bom=bom)

    @classmethod
    def from_path(cls, path: str | Path) -> SwmmDocument:
        raw = Path(path).read_bytes()
        try:
            bom = raw.startswith(b"\xef\xbb\xbf")
            text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
            encoding = "cp1252"
        document = cls.parse(text)
        document.encoding = encoding
        document.bom = bom if encoding == "utf-8" else False
        return document

    def render(self) -> str:
        lines = list(self.preamble)
        for section in self.sections:
            lines.append(section.header)
            lines.extend(section.lines)
        result = self.newline.join(lines)
        if self.trailing_newline and lines:
            result += self.newline
        return result

    def to_bytes(self) -> bytes:
        content = self.render().encode(self.encoding)
        return (b"\xef\xbb\xbf" + content) if self.bom and self.encoding == "utf-8" else content

    def section_names(self) -> list[str]:
        return [section.name for section in self.sections]

    def get_section(self, name: str) -> SwmmSection | None:
        normalized = name.strip().strip("[]").upper()
        return next((item for item in self.sections if item.name == normalized), None)

    def require_section(self, name: str) -> SwmmSection:
        section = self.get_section(name)
        if section is None:
            raise SwmmMutationError(f"section [{name.strip('[]').upper()}] was not found")
        return section

    def data_rows(self, name: str) -> Iterator[DataRow]:
        section = self.get_section(name)
        if section is None:
            return
        for index, raw in enumerate(section.lines):
            tokens, comment = tokenize(raw)
            if tokens:
                yield DataRow(section.name, index, raw, tokens, comment)

    def object_ids(self, name: str) -> list[str]:
        return [row.tokens[0] for row in self.data_rows(name)]

    def duplicate_ids(self, name: str) -> list[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in self.object_ids(name):
            key = value.casefold()
            if key in seen:
                duplicates.add(value)
            seen.add(key)
        return sorted(duplicates)

    @staticmethod
    def _format_tokens(original: str, tokens: list[str], comment: str) -> str:
        leading = original[: len(original) - len(original.lstrip())]
        body = "  ".join(_quote_token(token) for token in tokens)
        if comment:
            return f"{leading}{body}  {comment}"
        return f"{leading}{body}"

    def replace_token(
        self,
        section_name: str,
        line_index: int,
        token_index: int,
        value: str | float,
        *,
        command: str,
        field_name: str,
    ) -> SemanticChange:
        section = self.require_section(section_name)
        try:
            original = section.lines[line_index]
        except IndexError as exc:
            raise SwmmMutationError(f"line {line_index} is outside [{section.name}]") from exc
        tokens, comment = tokenize(original)
        if token_index >= len(tokens):
            raise SwmmMutationError(
                f"token {token_index} is outside [{section.name}] row {line_index}"
            )
        before = tokens[token_index]
        tokens[token_index] = str(value)
        section.lines[line_index] = self._format_tokens(original, tokens, comment)
        change = SemanticChange(
            command=command,
            section=section.name,
            object_id=tokens[0],
            field=field_name,
            before=before,
            after=str(value),
        )
        self.changes.append(change)
        return change

    def set_lid_unit_count(
        self,
        subcatchment_id: str,
        lid_control: str,
        unit_count: int,
    ) -> SemanticChange:
        if unit_count < 0:
            raise SwmmMutationError("LID unit count cannot be negative")
        for row in self.data_rows("LID_USAGE"):
            if (
                len(row.tokens) >= 3
                and row.tokens[0].casefold() == subcatchment_id.casefold()
                and row.tokens[1].casefold() == lid_control.casefold()
            ):
                return self.replace_token(
                    "LID_USAGE",
                    row.line_index,
                    2,
                    unit_count,
                    command="SetLIDUnitCount",
                    field_name="number_of_units",
                )
        raise SwmmMutationError(
            f"LID usage {subcatchment_id}/{lid_control} was not found; use add_lid_usage"
        )

    def add_lid_usage(
        self,
        *,
        subcatchment_id: str,
        lid_control: str,
        number: int,
        area: float,
        width: float,
        initial_saturation: float = 0,
        from_impervious: float = 100,
        route_to_pervious: bool = False,
        report_file: str = "*",
        drain_to: str = "*",
        from_pervious_flag: bool = False,
    ) -> SemanticChange:
        if number < 0 or area <= 0 or width <= 0:
            raise SwmmMutationError("LID number, area, and width must be physically valid")
        if subcatchment_id.casefold() not in {
            value.casefold() for value in self.object_ids("SUBCATCHMENTS")
        }:
            raise SwmmMutationError(f"subcatchment {subcatchment_id!r} was not found")
        controls = {row.tokens[0].casefold() for row in self.data_rows("LID_CONTROLS")}
        if lid_control.casefold() not in controls:
            raise SwmmMutationError(f"LID control {lid_control!r} was not found")
        for row in self.data_rows("LID_USAGE"):
            if row.tokens[:2] and [item.casefold() for item in row.tokens[:2]] == [
                subcatchment_id.casefold(),
                lid_control.casefold(),
            ]:
                raise SwmmMutationError("the requested LID usage already exists")
        section = self.get_section("LID_USAGE")
        if section is None:
            section = SwmmSection("LID_USAGE", "[LID_USAGE]", [""])
            self.sections.append(section)
        tokens = [
            subcatchment_id,
            lid_control,
            str(number),
            _number(area),
            _number(width),
            _number(initial_saturation),
            _number(from_impervious),
            "1" if route_to_pervious else "0",
            report_file,
            drain_to,
            "1" if from_pervious_flag else "0",
        ]
        new_line = "  ".join(tokens)
        section.lines.append(new_line)
        change = SemanticChange(
            command="AddLIDUsage",
            section="LID_USAGE",
            object_id=f"{subcatchment_id}/{lid_control}",
            field="row",
            before=None,
            after=new_line,
        )
        self.changes.append(change)
        return change

    def set_simulation_window(self, start_date: str, end_date: str) -> list[SemanticChange]:
        options = self.require_section("OPTIONS")
        targets = {"START_DATE": start_date, "END_DATE": end_date}
        found: set[str] = set()
        changes: list[SemanticChange] = []
        for row in list(self.data_rows("OPTIONS")):
            option = row.tokens[0].upper()
            if option in targets and len(row.tokens) > 1:
                found.add(option)
                changes.append(
                    self.replace_token(
                        "OPTIONS",
                        row.line_index,
                        1,
                        targets[option],
                        command="SetSimulationWindow",
                        field_name=option.lower(),
                    )
                )
        for option, value in targets.items():
            if option not in found:
                line = f"{option}  {value}"
                options.lines.append(line)
                change = SemanticChange(
                    "SetSimulationWindow", "OPTIONS", option, option.lower(), None, value
                )
                self.changes.append(change)
                changes.append(change)
        return changes

    def unified_diff(self, original: str, *, fromfile: str = "baseline.inp") -> str:
        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                self.render().splitlines(keepends=True),
                fromfile=fromfile,
                tofile="alternative.inp",
            )
        )


def _number(value: float) -> str:
    return f"{value:.12g}"


def _quote_token(value: str) -> str:
    if not value or any(char.isspace() for char in value) or ";" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def parse_float(token: str, *, context: str) -> float:
    try:
        return float(token)
    except ValueError as exc:
        raise SwmmParseError(f"expected a number for {context}, found {token!r}") from exc


def iter_named_rows(document: SwmmDocument, sections: Iterable[str]) -> Iterator[DataRow]:
    for section in sections:
        yield from document.data_rows(section)
