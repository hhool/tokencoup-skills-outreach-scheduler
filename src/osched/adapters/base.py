"""SourceAdapter abstract interface (DESIGN.md §4b).

Discipline (不变式):
1. Adapters are read-only and side-effect free: no probing, no DB writes,
   no file mutation.
2. One-way dependency: adapters may import models only — never downstream
   modules.
3. New import source = one adapter + one YAML block; downstream unchanged.
4. parse() must yield lazily (large files: tens of thousands of rows).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path

from ..models import SourceRecord


@dataclass
class SourceFile:
    """One discoverable data file of a source."""

    path: Path
    mtime: float = 0.0
    size: int = 0


@dataclass
class Issue:
    """A validation problem attached to a record."""

    severity: str   # "error" | "warning"
    message: str


class SourceAdapter(abc.ABC):
    """Abstract import-source interface.

    中文：抽象导入源接口——discover 枚举数据文件，parse 惰性产出统一模型，
    validate 做记录级校验。适配器只读、无副作用、单向依赖。
    """

    kind: str = "base"

    @abc.abstractmethod
    def discover(self) -> list[SourceFile]:
        """Enumerate currently available data files for this source."""

    @abc.abstractmethod
    def parse(self, file: SourceFile):
        """Yield SourceRecord objects lazily from one file."""

    def validate(self, rec: SourceRecord) -> list[Issue]:
        """Record-level validation hook. Default: structural checks only."""
        from ..models import validate as model_validate

        issues: list[Issue] = []
        for msg in model_validate(rec):
            issues.append(Issue("error", msg))
        return issues
