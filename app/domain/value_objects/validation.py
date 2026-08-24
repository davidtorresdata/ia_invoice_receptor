"""Validation report value objects shared across validation levels."""

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "ERROR"      # blocks: invoice marked INVALID
    WARNING = "WARNING"  # suspicious but tolerated
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A single, traceable validation finding."""

    code: str
    severity: Severity
    message: str
    field: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "field": self.field,
        }


@dataclass(slots=True)
class ValidationReport:
    """Aggregate result of a validation pass over one invoice."""

    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "issues": [issue.to_dict() for issue in self.issues],
        }
