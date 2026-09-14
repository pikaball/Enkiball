from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enkiball import Enkiball
from enkiball.config import save_config
from enkiball.skills import DEFAULT_SKILL_DIR, SkillRegistry


def write_skill(
    directory: Path,
    name: str,
    *,
    description: str = "Review changes when the user asks for a review.",
    body: str = "Follow the review workflow.",
) -> Path:
    root = directory / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return root


class SkillRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_discovers_valid_skills_and_reports_invalid_ones(self) -> None:
        write_skill(self.root, "code-review")
        invalid = self.root / "Bad_Name"
        invalid.mkdir()
        (invalid / "SKILL.md").write_text(
            "---\nname: Bad_Name\ndescription: invalid\n---\nbody\n",
            encoding="utf-8",
        )

        registry = SkillRegistry([self.root])

        self.assertEqual([skill.name for skill in registry.skills], ["code-review"])
        self.assertEqual(registry.skills[0].body, "Follow the review workflow.\n")
        self.assertTrue(any("invalid skill name" in item for item in registry.diagnostics))

    def test_first_duplicate_wins_and_conflict_is_reported(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        write_skill(first, "review", body="first")
        write_skill(second, "review", body="second")

        registry = SkillRegistry([first, second])

        self.assertIn("first", registry.load("review"))
        self.assertTrue(any("duplicate skill" in item for item in registry.diagnostics))

    def test_resource_loading_is_bounded_to_skill_root(self) -> None:
        skill = write_skill(self.root, "review")
        references = skill / "references"
        references.mkdir()
        (references / "guide.md").write_text("resource text", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        registry = SkillRegistry([self.root])

        loaded = registry.load("review", "references/guide.md")

        self.assertIn("resource text", loaded)
        with self.assertRaisesRegex(ValueError, "escapes"):
            registry.load("review", "../outside.md")

    def test_reference_detection_only_matches_known_exact_names(self) -> None:
        write_skill(self.root, "review")
        registry = SkillRegistry([self.root])

        selected = registry.referenced("Use $review, not $review-more or $UNKNOWN")

        self.assertEqual([skill.name for skill in selected], ["review"])


class EnkiballSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.skill_dir = self.root / "skills"
        self.config_path = self.root / "enki.json"
        save_config({}, self.config_path)

    def build_agent(self, skill_dirs: list[Path] | None = None) -> Enkiball:
        return Enkiball(
            config_path=self.config_path,
            state_path=self.root / "state.json",
            session_dir=self.root / "sessions",
            cwd=self.root,
            skill_dirs=skill_dirs if skill_dirs is not None else [],
            auto_start_mcp=False,
        )

    def test_default_skill_directory_is_user_config_directory(self) -> None:
        self.assertEqual(
            DEFAULT_SKILL_DIR,
            Path.home() / ".config" / "enkiball" / "skills",
        )

    def test_skill_tool_is_internal_and_skill_roots_are_read_only(self) -> None:
        skill_root = write_skill(self.skill_dir, "review")
        agent = self.build_agent([self.skill_dir])

        tool_names = {
            schema["function"]["name"] for schema in agent._engine._get_tools()
        }
        self.assertIn("skill", tool_names)
        self.assertNotIn("skill", agent.list_custom_tools())
        self.assertIn(skill_root.resolve(), agent._sandbox.read_only_directories)
        self.assertNotIn(skill_root.resolve(), agent._sandbox.writable_directories)
        with self.assertRaisesRegex(ValueError, "reserved"):
            agent.register_tool(
                lambda args: "bad",
                name="skill",
                description="bad",
                parameters={"type": "object", "properties": {}},
            )

    def test_explicit_skill_is_merged_with_existing_system_context(self) -> None:
        write_skill(self.skill_dir, "review", body="Inspect every changed line.")
        agent = self.build_agent([self.skill_dir])

        with patch.object(agent._engine, "stream_reply", return_value="done") as reply:
            result = agent.chat("Please use $review", system_context="Harness context")

        self.assertEqual(result.content, "done")
        context = reply.call_args.kwargs["system_context"]
        self.assertIn("Harness context", context)
        self.assertIn('<skill name="review">', context)
        self.assertIn("Inspect every changed line.", context)

    def test_reload_updates_registry_tool_and_read_roots(self) -> None:
        agent = self.build_agent([self.skill_dir])
        self.assertEqual(agent.skills, ())
        self.assertNotIn(
            "skill",
            {schema["function"]["name"] for schema in agent._engine._get_tools()},
        )

        skill_root = write_skill(self.skill_dir, "review")
        loaded = agent.reload_skills()

        self.assertEqual([skill.name for skill in loaded], ["review"])
        self.assertIn(skill_root.resolve(), agent._sandbox.read_only_directories)
        self.assertIn(
            "skill",
            {schema["function"]["name"] for schema in agent._engine._get_tools()},
        )


if __name__ == "__main__":
    unittest.main()
