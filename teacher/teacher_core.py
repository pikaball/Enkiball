"""TeacherMaster — the core teacher agent orchestrator with multi-project support."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from enkiball import Enkiball


def _load_prompt(name: str) -> str:
    path = Path(__file__).parent / "prompts" / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


ORCHESTRATOR_PROMPT = _load_prompt("orchestrator.txt")

TEACHER_CONFIG_DIR = Path(__file__).parent / "config"
DEFAULT_CONFIG_PATH = TEACHER_CONFIG_DIR / "enki.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "topic": None,
        "level": 1,
        "modules": [],
        "assessments": [],
        "yields": [],
        "capstone_completed": False,
        "capstone_details": "",
    }


def _default_registry() -> dict[str, Any]:
    return {"projects": {}, "active": None}


class TeacherMaster:
    """Orchestrates the strict teacher agent backed by Enkiball.

    Supports multiple learning projects, each with independent
    conversation history and learning state.  Persists everything
    under ``data/projects/<project_id>/``.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        if data_dir is None:
            data_dir = Path(__file__).parent / "data"
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._registry_file = self.data_dir / "projects.json"
        self._projects_dir = self.data_dir / "projects"

        if config_path is None:
            config_path = DEFAULT_CONFIG_PATH
        config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(
                f"Teacher config not found: {config_path}\n"
                f"Copy the base config and adjust: cp ~/.config/enkiball/enki.json {config_path}"
            )

        self.agent = Enkiball(
            config_path=config_path,
            system_prompt=ORCHESTRATOR_PROMPT,
        )
        self._register_tools()

        self._ensure_registry()

    # ------------------------------------------------------------------
    # Project registry
    # ------------------------------------------------------------------

    def _load_registry(self) -> dict[str, Any]:
        if self._registry_file.exists():
            try:
                return json.loads(self._registry_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return _default_registry()

    def _save_registry(self, reg: dict[str, Any]) -> None:
        self._registry_file.parent.mkdir(parents=True, exist_ok=True)
        self._registry_file.write_text(
            json.dumps(reg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _ensure_registry(self) -> None:
        reg = self._load_registry()
        changed = False

        if not reg.get("projects"):
            self._projects_dir.mkdir(parents=True, exist_ok=True)
            # Migrate legacy data if it exists alongside registry
            legacy_state = self.data_dir / "state.json"
            legacy_sessions = self.data_dir / "sessions"
            if legacy_state.exists() or legacy_sessions.exists():
                reg["projects"]["virtualization"] = {
                    "name": "虚拟化安全基础",
                    "created_at": _now_iso(),
                    "topic": None,
                }
                proj_dir = self._projects_dir / "virtualization"
                proj_dir.mkdir(parents=True, exist_ok=True)
                if legacy_state.exists():
                    legacy_state.rename(proj_dir / "state.json")
                if legacy_sessions.exists():
                    # Migrate latest session messages
                    sessions = sorted(legacy_sessions.glob("*.json"))
                    if sessions:
                        try:
                            data = json.loads(sessions[-1].read_text(encoding="utf-8"))
                            msgs = data.get("messages", [])
                            (proj_dir / "messages.json").write_text(
                                json.dumps(msgs, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                        except (json.JSONDecodeError, OSError):
                            pass
                    import shutil
                    shutil.rmtree(legacy_sessions, ignore_errors=True)
                reg["active"] = "virtualization"
                changed = True
            else:
                reg["projects"]["default"] = {
                    "name": "default",
                    "created_at": _now_iso(),
                    "topic": None,
                }
                reg["active"] = "default"
                changed = True

        if changed:
            self._save_registry(reg)

        active = reg.get("active")
        if active:
            self._load_project(active)

    # ------------------------------------------------------------------
    # Project-level paths
    # ------------------------------------------------------------------

    def _project_dir(self, project_id: str) -> Path:
        return self._projects_dir / project_id

    def _project_state_file(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "state.json"

    def _project_msgs_file(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "messages.json"

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        reg = self._load_registry()
        out = []
        for pid, info in reg.get("projects", {}).items():
            state = self._load_state_for(pid)
            out.append({
                "id": pid,
                "name": info.get("name", pid),
                "topic": state.get("topic"),
                "level": state.get("level", 1),
                "created_at": info.get("created_at", ""),
            })
        return out

    def create_project(self, name: str) -> dict[str, Any]:
        pid = _safe_id(name)
        reg = self._load_registry()
        counter = 1
        base = pid
        while pid in reg.get("projects", {}):
            pid = f"{base}-{counter}"
            counter += 1

        reg["projects"][pid] = {
            "name": name,
            "created_at": _now_iso(),
            "topic": None,
        }
        proj_dir = self._project_dir(pid)
        proj_dir.mkdir(parents=True, exist_ok=True)

        # Init empty state
        self._save_state_for(pid, _default_state())
        # Init empty messages
        self._save_messages_for(pid, [])

        self._save_registry(reg)
        return {"id": pid, "name": name}

    def switch_project(self, project_id: str) -> dict[str, Any]:
        reg = self._load_registry()
        if project_id not in reg.get("projects", {}):
            raise ValueError(f"Project not found: {project_id}")

        active = reg.get("active")
        if active and active != project_id:
            self._save_messages_for(active, self.agent.messages)

        reg["active"] = project_id
        self._save_registry(reg)
        self._load_project(project_id)

        return {
            "id": project_id,
            "name": reg["projects"][project_id].get("name", project_id),
        }

    def delete_project(self, project_id: str) -> bool:
        reg = self._load_registry()
        if project_id not in reg.get("projects", {}):
            return False
        if len(reg["projects"]) <= 1:
            return False

        import shutil
        shutil.rmtree(self._project_dir(project_id), ignore_errors=True)
        del reg["projects"][project_id]
        if reg["active"] == project_id:
            reg["active"] = next(iter(reg["projects"]))
            self._load_project(reg["active"])
        self._save_registry(reg)
        return True

    # ------------------------------------------------------------------
    # Per-project state
    # ------------------------------------------------------------------

    def _load_state_for(self, project_id: str) -> dict[str, Any]:
        sf = self._project_state_file(project_id)
        if sf.exists():
            try:
                return json.loads(sf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return _default_state()

    def _save_state_for(self, project_id: str, state: dict[str, Any]) -> None:
        self._project_dir(project_id).mkdir(parents=True, exist_ok=True)
        self._project_state_file(project_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_messages_for(self, project_id: str) -> list[dict[str, Any]]:
        mf = self._project_msgs_file(project_id)
        if mf.exists():
            try:
                return json.loads(mf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save_messages_for(self, project_id: str, msgs: list[dict[str, Any]]) -> None:
        self._project_dir(project_id).mkdir(parents=True, exist_ok=True)
        self._project_msgs_file(project_id).write_text(
            json.dumps(msgs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_project(self, project_id: str) -> None:
        msgs = self._load_messages_for(project_id)
        if msgs and len(msgs) > 1 and msgs[0].get("role") == "system":
            self.agent.load_messages(msgs)
        elif msgs and len(msgs) >= 1:
            self.agent.load_messages(msgs)

    def save_current_project(self) -> None:
        reg = self._load_registry()
        active = reg.get("active")
        if active:
            self._save_messages_for(active, self.agent.messages)

    # ------------------------------------------------------------------
    # Current project state (delegates to active project)
    # ------------------------------------------------------------------

    @property
    def state(self) -> dict[str, Any]:
        reg = self._load_registry()
        active = reg.get("active")
        if active:
            return self._load_state_for(active)
        return _default_state()

    def _load_state(self) -> dict[str, Any]:
        return self.state

    def _save_state(self, state: dict[str, Any]) -> None:
        reg = self._load_registry()
        active = reg.get("active")
        if active:
            topic = state.get("topic")
            if topic and topic != reg["projects"][active].get("topic"):
                reg["projects"][active]["topic"] = topic
                self._save_registry(reg)
            self._save_state_for(active, state)

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        self.agent.register_tool(
            self._get_learning_state,
            name="get_learning_state",
            description="获取当前学习状态，包含当前主题、等级(1-4)、模块完成情况、评估历史、认输记录、毕业状态",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )

        self.agent.register_tool(
            self._set_curriculum,
            name="set_curriculum",
            description=(
                "保存或更新课程计划。在对话中向学生展示并确认课程后调用此工具。"
                "modules 数组每个元素含 id(唯一标识)、name(模块名)、level(所属等级 1-4)、"
                "description(简述)。等级 4 的模块即毕业课题。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "学习主题"},
                    "modules": {
                        "type": "array",
                        "description": "课程模块列表，按学习顺序排列",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "level": {"type": "integer", "minimum": 1, "maximum": 4},
                                "description": {"type": "string"},
                            },
                            "required": ["id", "name", "level"],
                        },
                    },
                },
                "required": ["topic", "modules"],
            },
        )

        self.agent.register_tool(
            self._record_assessment,
            name="record_assessment",
            description=(
                "记录一次等级评估结果。passed=true 且 level<4 时自动升级；"
                "passed=true 且 level=4 表示毕业。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "description": "被评估的等级 (1-4)", "minimum": 1, "maximum": 4},
                    "passed": {"type": "boolean", "description": "是否通过评估"},
                    "questions_total": {"type": "integer", "description": "评估问题总数"},
                    "questions_passed": {"type": "integer", "description": "通过的问题数"},
                    "notes": {"type": "string", "description": "评估备注或教师评语"},
                },
                "required": ["level", "passed"],
            },
        )

        self.agent.register_tool(
            self._record_yield,
            name="record_yield",
            description="记录学生认输。学生在某主题/问题上认输后，教师在给出完整解答时调用此工具标记该主题需重新学习。",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "认输的主题或问题描述"},
                    "details": {"type": "string", "description": "认输详情和你给出的解答概要"},
                },
                "required": ["topic"],
            },
        )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _get_learning_state(self, args: dict[str, Any]) -> str:
        state = self._load_state()
        return json.dumps({"ok": True, "result": state}, ensure_ascii=False)

    def _set_curriculum(self, args: dict[str, Any]) -> str:
        state = self._load_state()
        state["topic"] = args["topic"]
        modules = args.get("modules", [])
        for m in modules:
            m.setdefault("completed", False)
        state["modules"] = modules
        state["level"] = 1
        state["capstone_completed"] = False
        self._save_state(state)
        return json.dumps(
            {"ok": True, "result": f"课程已保存: {args['topic']}，共 {len(modules)} 个模块"},
            ensure_ascii=False,
        )

    def _record_assessment(self, args: dict[str, Any]) -> str:
        state = self._load_state()
        record = {
            "level": args["level"],
            "passed": args["passed"],
            "questions_total": args.get("questions_total", 0),
            "questions_passed": args.get("questions_passed", 0),
            "notes": args.get("notes", ""),
            "date": datetime.now().isoformat(),
        }
        state.setdefault("assessments", []).append(record)

        if args["passed"]:
            for m in state.get("modules", []):
                if m.get("level") == args["level"]:
                    m["completed"] = True
            if args["level"] < 4:
                state["level"] = args["level"] + 1
            elif args["level"] == 4:
                state["capstone_completed"] = True

        self._save_state(state)
        return json.dumps({"ok": True, "result": "评估结果已记录"}, ensure_ascii=False)

    def _record_yield(self, args: dict[str, Any]) -> str:
        state = self._load_state()
        record = {
            "topic": args["topic"],
            "details": args.get("details", ""),
            "date": datetime.now().isoformat(),
        }
        state.setdefault("yields", []).append(record)
        self._save_state(state)
        return json.dumps({"ok": True, "result": "认输已记录，该主题需要重新学习"}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self.save_current_project()
        self.agent.shutdown()


def _safe_id(name: str) -> str:
    import re
    safe = re.sub(r"[^a-zA-Z0-9\-_]", "-", name.strip().lower())
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe or "project"
