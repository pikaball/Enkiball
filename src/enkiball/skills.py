from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


DEFAULT_SKILL_DIR = Path.home() / ".config" / "enkiball" / "skills"
MAX_SKILL_BYTES = 64 * 1024
MAX_RESOURCE_BYTES = 64 * 1024
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_REFERENCE_RE = re.compile(r"(?<![\w$])\$([a-z0-9]+(?:-[a-z0-9]+)*)\b")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    root: Path
    body: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: str | None = None


class SkillRegistry:
    def __init__(self, directories: Iterable[str | Path]) -> None:
        self.directories = tuple(
            Path(path).expanduser().resolve(strict=False) for path in directories
        )
        self._skills: dict[str, Skill] = {}
        self._diagnostics: list[str] = []
        self.reload()

    @property
    def skills(self) -> tuple[Skill, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(skill.root for skill in self.skills)

    def reload(self) -> None:
        self._skills = {}
        self._diagnostics = []
        for directory in self.directories:
            if not directory.exists():
                continue
            if not directory.is_dir():
                self._diagnostics.append(f"skill directory is not a directory: {directory}")
                continue
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                if not child.is_dir():
                    continue
                try:
                    skill = self._load(child)
                except Exception as exc:  # noqa: BLE001
                    self._diagnostics.append(f"{child}: {exc}")
                    continue
                previous = self._skills.get(skill.name)
                if previous is not None:
                    self._diagnostics.append(
                        f"duplicate skill {skill.name!r}: {previous.root} and {skill.root}"
                    )
                    continue
                self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"unknown skill: {name}") from None

    def load(self, name: str, resource: str | None = None) -> str:
        skill = self.get(name)
        if resource is None:
            return self._format_content(skill, skill.body)
        if not isinstance(resource, str) or not resource.strip():
            raise ValueError("resource must be a non-empty relative path")
        relative = Path(resource)
        if relative.is_absolute():
            raise ValueError("resource must be a relative path")
        root = skill.root.resolve()
        target = (root / relative).resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError("resource escapes the skill directory")
        if not target.is_file():
            raise FileNotFoundError(f"skill resource not found: {resource}")
        content = self._read_text(target, MAX_RESOURCE_BYTES)
        return self._format_content(skill, content, resource=resource)

    def referenced(self, text: str) -> tuple[Skill, ...]:
        found: list[Skill] = []
        seen: set[str] = set()
        for name in SKILL_REFERENCE_RE.findall(text):
            if name in seen or name not in self._skills:
                continue
            seen.add(name)
            found.append(self._skills[name])
        return tuple(found)

    def catalog(self, max_chars: int = 12_000) -> str:
        lines = [skill.name for skill in self.skills]
        used = sum(len(line) + 1 for line in lines)
        for index, skill in enumerate(self.skills):
            detailed = f"{skill.name}: {skill.description}"
            added = len(detailed) - len(lines[index])
            if used + added <= max_chars:
                lines[index] = detailed
                used += added
        return "\n".join(lines)

    def tool_schema(self) -> dict[str, Any]:
        catalog = self.catalog()
        return {
            "type": "function",
            "function": {
                "name": "skill",
                "description": (
                    "Load reusable instructions or a referenced UTF-8 resource. "
                    "Call this when a task matches a skill description. If the user "
                    "names a skill, load it before proceeding. Available skills:\n"
                    f"{catalog}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact skill name from the catalog.",
                        },
                        "resource": {
                            "type": "string",
                            "description": (
                                "Optional resource path relative to the skill directory."
                            ),
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        }

    def handle_tool(self, args: dict[str, Any]) -> str:
        name = args.get("name")
        resource = args.get("resource")
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        if resource is not None and not isinstance(resource, str):
            raise TypeError("resource must be a string")
        return self.load(name, resource)

    def activation_context(self, text: str) -> str | None:
        selected = self.referenced(text)
        if not selected:
            return None
        return "\n\n".join(self._format_content(skill, skill.body) for skill in selected)

    @staticmethod
    def _format_content(skill: Skill, content: str, resource: str | None = None) -> str:
        resource_attr = f' resource="{resource}"' if resource else ""
        return f'<skill name="{skill.name}"{resource_attr}>\n{content}\n</skill>'

    @classmethod
    def _load(cls, root: Path) -> Skill:
        skill_file = root / "SKILL.md"
        if not skill_file.is_file():
            raise ValueError("missing SKILL.md")
        text = cls._read_text(skill_file, MAX_SKILL_BYTES)
        metadata, body = cls._parse_frontmatter(text)

        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
            raise ValueError("invalid skill name")
        if name != root.name:
            raise ValueError("skill name must match its directory name")
        if not isinstance(description, str) or not 1 <= len(description) <= 1024:
            raise ValueError("description must contain 1-1024 characters")

        license_value = cls._optional_string(metadata, "license")
        compatibility = cls._optional_string(metadata, "compatibility")
        if compatibility is not None and len(compatibility) > 500:
            raise ValueError("compatibility must not exceed 500 characters")
        allowed_tools = cls._optional_string(metadata, "allowed-tools")

        custom_metadata = metadata.get("metadata")
        if custom_metadata is None:
            parsed_metadata: dict[str, str] = {}
        elif isinstance(custom_metadata, dict) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in custom_metadata.items()
        ):
            parsed_metadata = dict(custom_metadata)
        else:
            raise ValueError("metadata must be a string-to-string mapping")

        return Skill(
            name=name,
            description=description,
            root=root.resolve(),
            body=body,
            license=license_value,
            compatibility=compatibility,
            metadata=parsed_metadata,
            allowed_tools=allowed_tools,
        )

    @staticmethod
    def _optional_string(metadata: dict[str, Any], key: str) -> str | None:
        value = metadata.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return value

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError("SKILL.md must start with YAML frontmatter")
        closing: int | None = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                closing = index
                break
        if closing is None:
            raise ValueError("unterminated YAML frontmatter")
        loaded = yaml.safe_load("".join(lines[1:closing]))
        if not isinstance(loaded, dict):
            raise ValueError("frontmatter must be a mapping")
        return loaded, "".join(lines[closing + 1 :]).lstrip("\r\n")

    @staticmethod
    def _read_text(path: Path, limit: int) -> str:
        if path.stat().st_size > limit:
            raise ValueError(f"file exceeds {limit} byte limit")
        return path.read_text(encoding="utf-8")
