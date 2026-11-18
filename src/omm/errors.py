"""Structured diagnostics shared by source readers and the CLI."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    path: Path | None = None
    line: int | None = None
    block_id: str | None = None
    severity: Literal["error", "warning"] = "error"
    params: Mapping[str, object] = field(default_factory=dict)


class OmmError(Exception):
    """A blocking error with a stable code and localized message arguments."""

    def __init__(
        self,
        code: str,
        *,
        path: Path | None = None,
        line: int | None = None,
        block_id: str | None = None,
        **params: object,
    ) -> None:
        super().__init__(code)
        self.diagnostic = Diagnostic(code, path, line, block_id, params=params)

    @property
    def code(self) -> str:
        return self.diagnostic.code
