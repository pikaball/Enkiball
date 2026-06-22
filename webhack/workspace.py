from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4


WEBHACK_BASE = Path.home() / ".config" / "enkiball" / "webhack"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskStatus(StrEnum):
    TODO = "todo"
    RUNNING = "running"
    REVIEW = "review"
    DECOMPOSED = "decomposed"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """A durable unit of work.

    The task tree is the source of truth for work allocation and completion.
    Executor conversation history is deliberately not the source of truth.
    """

    id: str
    title: str
    goal: str
    status: TaskStatus = TaskStatus.TODO
    parent_id: str | None = None
    executor_id: str | None = None
    context: str = ""
    completion_criteria: str = ""
    result: str = ""
    evidence: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        title: str,
        goal: str,
        *,
        parent_id: str | None = None,
        context: str = "",
        completion_criteria: str = "",
    ) -> Task:
        return cls(
            id=f"task-{uuid4().hex[:10]}",
            title=title,
            goal=goal,
            parent_id=parent_id,
            context=context,
            completion_criteria=completion_criteria,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        raw = dict(raw)
        raw.pop("priority", None)
        raw["status"] = TaskStatus(raw.get("status", TaskStatus.TODO))
        raw.setdefault("completion_criteria", "")
        raw.setdefault("attempts", [])
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["status"] = str(self.status)
        return out


class WebHackWorkspace:
    """Durable shared state for supervisor and executors.

    Stores only stable facts: task tree, state graph, human notes, and event log.
    This is deliberately file-backed JSONL/JSON so it is inspectable, robust,
    and easy to replace with a server later.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root) if root else WEBHACK_BASE / "sessions" / "default"
        self.root = base.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks_path = self.root / "tasks.json"
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"
        self.notes_path = self.root / "human_notes.md"
        self._lock = threading.RLock()
        self._ensure_files()

    @classmethod
    def for_session(cls, session_id: str, base: str | Path | None = None) -> WebHackWorkspace:
        root_base = Path(base) if base else WEBHACK_BASE / "sessions"
        return cls(root_base / safe_session_id(session_id))

    def _ensure_files(self) -> None:
        if not self.tasks_path.exists():
            self.tasks_path.write_text("[]\n", encoding="utf-8")
        if not self.state_path.exists():
            self.state_path.write_text(
                json.dumps(self._initial_state_graph(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if not self.notes_path.exists():
            self.notes_path.write_text("# Human Notes\n\n", encoding="utf-8")
        if not self.events_path.exists():
            self.events_path.write_text("", encoding="utf-8")

    @staticmethod
    def _initial_state_graph() -> dict[str, Any]:
        return {"facts": [], "open_questions": []}

    # -- Task tree ----------------------------------------------------------

    def list_tasks(self) -> list[Task]:
        with self._lock:
            raw = json.loads(self.tasks_path.read_text(encoding="utf-8") or "[]")
            return [Task.from_dict(item) for item in raw]

    def save_tasks(self, tasks: list[Task]) -> None:
        with self._lock:
            data = [t.to_dict() for t in tasks]
            self.tasks_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def create_task(
        self,
        title: str,
        goal: str,
        *,
        parent_id: str | None = None,
        context: str = "",
        completion_criteria: str = "",
    ) -> Task:
        with self._lock:
            tasks = self.list_tasks()
            task = Task.create(
                title,
                goal,
                parent_id=parent_id,
                context=context,
                completion_criteria=completion_criteria,
            )
            tasks.append(task)
            self.save_tasks(tasks)
            self.log_event("task.created", {"task_id": task.id, "title": task.title})
            return task

    def get_task(self, task_id: str) -> Task | None:
        for task in self.list_tasks():
            if task.id == task_id:
                return task
        return None

    def update_task(self, task_id: str, **changes: Any) -> Task | None:
        with self._lock:
            tasks = self.list_tasks()
            updated: Task | None = None
            for task in tasks:
                if task.id != task_id:
                    continue
                for key, value in changes.items():
                    if key == "status" and value is not None:
                        value = TaskStatus(value)
                    if hasattr(task, key):
                        setattr(task, key, value)
                task.updated_at = utc_now()
                updated = task
                break
            if updated:
                self.save_tasks(tasks)
                self.log_event("task.updated", {"task_id": task_id, "changes": changes})
            return updated

    def child_ids(self, task_id: str) -> list[str]:
        return [task.id for task in self.list_tasks() if task.parent_id == task_id]

    def is_leaf(self, task_id: str) -> bool:
        return not self.child_ids(task_id)

    def mark_executor_finished(
        self,
        task_id: str,
        *,
        executor_id: str,
        content: str,
        status: str,
        error: str = "",
    ) -> Task | None:
        task = self.get_task(task_id)
        if not task:
            return None
        attempt = {
            "executor_id": executor_id,
            "status": status,
            "finished_at": utc_now(),
            "content": content[-20000:],
        }
        if error:
            attempt["error"] = error
        attempts = [*task.attempts, attempt]
        return self.update_task(
            task_id,
            status=TaskStatus.REVIEW,
            executor_id="",
            attempts=attempts,
            result=content[-4000:] if content else task.result,
        )

    def task_tree_text(self) -> str:
        tasks = self.list_tasks()
        by_parent: dict[str | None, list[Task]] = {}
        for task in tasks:
            by_parent.setdefault(task.parent_id, []).append(task)

        def walk(parent: str | None, depth: int) -> list[str]:
            lines: list[str] = []
            for task in by_parent.get(parent, []):
                indent = "  " * depth
                running = task.status == TaskStatus.RUNNING and bool(task.executor_id)
                assignee = f" exec={task.executor_id}" if task.executor_id else ""
                active = " active=yes" if running else " active=no"
                lines.append(
                    f"{indent}- [{task.status}] {task.id}: {task.title}{assignee}{active}\n"
                    f"{indent}  goal: {task.goal}"
                )
                if task.completion_criteria:
                    lines.append(f"{indent}  done when: {task.completion_criteria[:300]}")
                if task.blocked_reason:
                    lines.append(f"{indent}  blocked: {task.blocked_reason[:300]}")
                if task.result:
                    lines.append(f"{indent}  result: {task.result[:300]}")
                if task.attempts:
                    last = task.attempts[-1]
                    lines.append(
                        f"{indent}  last_attempt: {last.get('status')} {last.get('executor_id')}"
                    )
                lines.extend(walk(task.id, depth + 1))
            return lines

        return "\n".join(walk(None, 0)) or "(no tasks)"

    # -- State graph and human notes ---------------------------------------

    def read_state(self) -> str:
        with self._lock:
            return self.state_path.read_text(encoding="utf-8")

    def read_state_graph(self) -> dict[str, Any]:
        with self._lock:
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                data = self._initial_state_graph()
            if not isinstance(data, dict):
                data = self._initial_state_graph()
            data.setdefault("facts", [])
            data.setdefault("open_questions", [])
            return data

    def write_state(self, content: str) -> None:
        with self._lock:
            data = json.loads(content)
            self.write_state_graph(data)

    def write_state_graph(self, data: dict[str, Any]) -> None:
        with self._lock:
            data = self._validate_state_graph(data)
            content = json.dumps(data, ensure_ascii=False, indent=2)
            self.state_path.write_text(content + "\n", encoding="utf-8")
            self.log_event("state.updated", {"chars": len(content)})

    def append_state_change(self, note: str) -> None:
        self.add_state_fact({"type": "note", "subject": "operator", "fact": note})

    def add_state_fact(self, fact: dict[str, Any]) -> None:
        with self._lock:
            state = self.read_state_graph()
            clean = self._validate_fact(fact)
            state["facts"].append(clean)
            self.write_state_graph(state)
            self.log_event("state.fact_added", {"fact": clean})

    @staticmethod
    def _validate_fact(fact: dict[str, Any]) -> dict[str, Any]:
        allowed = {"type", "subject", "fact", "evidence", "confidence"}
        clean = {}
        for key in allowed:
            value = fact.get(key)
            if value is None or value == "":
                continue
            clean[key] = value
        clean["type"] = str(clean.get("type", "fact"))
        clean["subject"] = str(clean.get("subject", "unknown"))
        clean["fact"] = str(clean.get("fact", ""))
        evidence = clean.get("evidence", [])
        clean["evidence"] = evidence if isinstance(evidence, list) else [str(evidence)]
        clean["confidence"] = str(clean.get("confidence", "observed"))
        clean["updated_at"] = utc_now()
        return clean

    def _validate_state_graph(self, data: dict[str, Any]) -> dict[str, Any]:
        facts = data.get("facts", [])
        questions = data.get("open_questions", [])
        return {
            "facts": [self._validate_fact(f) for f in facts if isinstance(f, dict)],
            "open_questions": [q for q in questions if isinstance(q, dict)],
        }

    def append_human_note(self, note: str) -> None:
        with self._lock:
            with self.notes_path.open("a", encoding="utf-8") as f:
                f.write(f"\n## {utc_now()}\n\n{note.strip()}\n")
            self.log_event("human.note", {"note": note[:300]})

    def read_human_notes(self) -> str:
        with self._lock:
            return self.notes_path.read_text(encoding="utf-8")

    # -- Events -------------------------------------------------------------

    def log_event(self, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            row = {"ts": utc_now(), "kind": kind, "data": data}
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def recent_events(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # -- Injection ----------------------------------------------------------

    def floating_context(self, *, max_chars: int = 8000) -> str:
        """Return compact context injected into supervisor/executor turns."""
        task_tree = self.task_tree_text()
        state = self.read_state()
        notes = self.read_human_notes()
        events = "\n".join(
            f"- {e.get('ts')} {e.get('kind')}: {e.get('data')}"
            for e in self.recent_events(12)
        )
        text = (
            "<floating_context>\n"
            "This block is external durable state. Treat it as more authoritative "
            "than stale conversation history. Do not rewrite it unless asked; "
            "use provided tools to update tasks/state.\n\n"
            "## Task Tree\n"
            f"{task_tree}\n\n"
            "## State Graph JSON\n"
            f"{state}\n\n"
            "## Human Notes\n"
            f"{notes}\n\n"
            "## Recent Events\n"
            f"{events or '(none)'}\n"
            "</floating_context>"
        )
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 80] + "\n...[floating_context truncated]\n</floating_context>"

    def ready_tasks(self, limit: int = 5) -> list[Task]:
        tasks = [
            t
            for t in self.list_tasks()
            if t.status == TaskStatus.TODO and t.parent_id is not None and self.is_leaf(t.id)
        ]
        tasks.sort(key=lambda t: t.created_at)
        return tasks[:limit]

    def summary(self) -> dict[str, Any]:
        tasks = self.list_tasks()
        counts: dict[str, int] = {}
        for task in tasks:
            counts[str(task.status)] = counts.get(str(task.status), 0) + 1
        return {
            "root": str(self.root),
            "task_count": len(tasks),
            "status_counts": counts,
            "tasks_path": str(self.tasks_path),
            "state_path": str(self.state_path),
            "events_path": str(self.events_path),
        }


def safe_session_id(session_id: str) -> str:
    cleaned = "".join(
        ch for ch in session_id.strip() if ch.isalnum() or ch in {"-", "_"}
    )
    return cleaned or "default"
