"""Data models for the teacher agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Module:
    id: str
    name: str
    level: int
    description: str = ""
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "level": self.level,
            "description": self.description,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Module:
        return cls(
            id=d["id"],
            name=d["name"],
            level=d["level"],
            description=d.get("description", ""),
            completed=d.get("completed", False),
        )


@dataclass
class AssessmentRecord:
    level: int
    passed: bool
    questions_total: int = 0
    questions_passed: int = 0
    notes: str = ""
    date: str = ""

    def __post_init__(self):
        if not self.date:
            self.date = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "passed": self.passed,
            "questions_total": self.questions_total,
            "questions_passed": self.questions_passed,
            "notes": self.notes,
            "date": self.date,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AssessmentRecord:
        return cls(
            level=d["level"],
            passed=d["passed"],
            questions_total=d.get("questions_total", 0),
            questions_passed=d.get("questions_passed", 0),
            notes=d.get("notes", ""),
            date=d.get("date", ""),
        )


@dataclass
class LearningState:
    topic: str | None = None
    level: int = 1
    modules: list[dict[str, Any]] = field(default_factory=list)
    assessments: list[dict[str, Any]] = field(default_factory=list)
    yields: list[dict[str, Any]] = field(default_factory=list)
    capstone_completed: bool = False
    capstone_details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "level": self.level,
            "modules": self.modules,
            "assessments": self.assessments,
            "yields": self.yields,
            "capstone_completed": self.capstone_completed,
            "capstone_details": self.capstone_details,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LearningState:
        return cls(
            topic=d.get("topic"),
            level=d.get("level", 1),
            modules=d.get("modules", []),
            assessments=d.get("assessments", []),
            yields=d.get("yields", []),
            capstone_completed=d.get("capstone_completed", False),
            capstone_details=d.get("capstone_details", ""),
        )


def default_state() -> LearningState:
    return LearningState()
