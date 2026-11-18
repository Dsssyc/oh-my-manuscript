"""Read BibTeX entries, including balanced braced and quoted field values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from omm.errors import OmmError
from omm.workspace import normalize_newlines, read_text

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_:-]*")
_KEY = re.compile(r'[^\s,={}()#"@%]+')
_BARE_VALUE = re.compile(r'[^\s,={}()#"@%]+')


@dataclass(frozen=True, slots=True)
class BibEntry:
    entry_type: str
    citekey: str
    fields: dict[str, str]


class _BibParser:
    def __init__(self, source: str, path: Path | None) -> None:
        self.source = source
        self.path = path
        self.pos = 0
        self.strings: dict[str, str] = {}

    def error(self) -> OmmError:
        return OmmError(
            "ERR_BIB_INVALID", path=self.path, line=self.source.count("\n", 0, self.pos) + 1
        )

    def skip(self) -> None:
        while self.pos < len(self.source):
            if self.source[self.pos].isspace():
                self.pos += 1
            elif self.source[self.pos] == "%":
                end = self.source.find("\n", self.pos)
                self.pos = len(self.source) if end < 0 else end + 1
            else:
                break

    def consume(self, token: str) -> bool:
        self.skip()
        if self.source.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def require(self, token: str) -> None:
        if not self.consume(token):
            raise self.error()

    def match(self, pattern: re.Pattern[str]) -> str:
        self.skip()
        match = pattern.match(self.source, self.pos)
        if match is None:
            raise self.error()
        self.pos = match.end()
        return match[0]

    def grouped(self, closer: str) -> str:
        start = self.pos
        braces = 0
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == closer and braces == 0:
                self.pos += 1
                return self.source[start : self.pos - 1]
            if char == "\\":
                self.pos += 2
                continue
            if char == "{":
                braces += 1
            elif char == "}":
                if braces == 0:
                    raise self.error()
                braces -= 1
            self.pos += 1
        raise self.error()

    def value(self) -> str:
        pieces = []
        while True:
            if self.consume("{"):
                pieces.append(self.grouped("}"))
            elif self.consume('"'):
                pieces.append(self.grouped('"'))
            else:
                value = self.match(_BARE_VALUE)
                pieces.append(self.strings.get(value.lower(), value))
            if not self.consume("#"):
                return "".join(pieces)

    def fields(self, closer: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        while not self.consume(closer):
            key = self.match(_NAME).lower()
            if key in fields:
                raise self.error()
            self.require("=")
            fields[key] = self.value()
            if not self.consume(","):
                self.require(closer)
                break
        return fields

    def parse(self) -> dict[str, BibEntry]:
        entries: dict[str, BibEntry] = {}
        while True:
            self.skip()
            if self.pos == len(self.source):
                return entries
            self.require("@")
            entry_type = self.match(_NAME).lower()
            if self.consume("{"):
                closer = "}"
            else:
                self.require("(")
                closer = ")"
            if entry_type == "comment":
                self.grouped(closer)
            elif entry_type == "preamble":
                self.value()
                self.require(closer)
            elif entry_type == "string":
                self.strings.update(self.fields(closer))
            else:
                self.skip()
                line = self.source.count("\n", 0, self.pos) + 1
                citekey = self.match(_KEY)
                if citekey in entries:
                    raise OmmError("ERR_BIB_DUPLICATE_KEY", path=self.path, line=line, key=citekey)
                if self.consume(","):
                    fields = self.fields(closer)
                else:
                    self.require(closer)
                    fields = {}
                entries[citekey] = BibEntry(entry_type, citekey, fields)


def parse_bib(source: str, path: Path | None = None) -> dict[str, BibEntry]:
    return _BibParser(normalize_newlines(source), path).parse()


def load_bib(path: Path) -> dict[str, BibEntry]:
    return parse_bib(read_text(path), path)
