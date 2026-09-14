from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from enkiball import Enkiball

from .prompts import EXECUTOR_PROMPT, browser_target_id, executor_task_prompt
from .workspace import TaskStatus, WebHackWorkspace, utc_now


@dataclass
class ExecutorRecord:
    id: str
    task_id: str | None
    agent: Enkiball
    future: Any | None = None
    prompt: str = ""
    target_id: str = ""
    session_dir: str = ""
    status: str = "idle"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_error: str = ""

    def to_summary(self) -> dict[str, Any]:
        snap = self.agent.capture()
        done = bool(self.future.done()) if self.future else self.status != "running"
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": "done" if done and self.status == "running" else self.status,
            "target_id": self.target_id,
            "session_dir": self.session_dir,
            "running": bool(snap.in_progress),
            "content_chars": len(snap.content),
            "last_output": snap.content[-1200:],
            "tool_events": snap.tool_events[-5:],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }


class ExecutorController:
    """Manages disposable focused Enkiball executors."""

    def __init__(
        self,
        workspace: WebHackWorkspace,
        *,
        max_running: int = 10,
        agent_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.max_running = max_running
        self.agent_kwargs = dict(agent_kwargs or {})
        self.wake_callback: Any | None = None
        self._lock = threading.RLock()
        self._next_id = 0
        self.executors: dict[str, ExecutorRecord] = {}

    def _new_executor_id(self) -> str:
        with self._lock:
            self._next_id += 1
            return f"exec-{self._next_id}"

    def _make_agent(self, agent_kwargs: dict[str, Any] | None = None) -> Enkiball:
        agent = Enkiball(
            system_prompt=EXECUTOR_PROMPT,
            **(dict(self.agent_kwargs) if agent_kwargs is None else agent_kwargs),
        )
        agent.auto_compact_enabled = True
        agent.register_tool(
            lambda args: self.workspace.floating_context(max_chars=20000),
            name="get_context",
            description="Read current durable task tree, state graph, human notes, and recent events. Read-only.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        return agent

    def start_for_task(self, task_id: str) -> dict[str, Any]:
        task = self.workspace.get_task(task_id)
        if not task:
            valid_ids = [item.id for item in self.workspace.list_tasks()]
            self.workspace.log_event(
                "executor.invalid_start", {"task_id": task_id, "valid_task_ids": valid_ids}
            )
            return {
                "ok": False,
                "error": f"unknown task: {task_id}; use an id returned by create_task/list_tasks",
                "valid_task_ids": valid_ids,
            }
        if task.status != TaskStatus.TODO:
            return {"ok": False, "error": f"task is not todo: {task_id}", "status": str(task.status)}
        if not self.workspace.is_leaf(task.id):
            return {"ok": False, "error": f"task is not a leaf: {task_id}"}
        prompt = executor_task_prompt(
            task.id,
            task.title,
            task.goal,
            self._task_context(task.id),
        )
        return self.start(prompt, task_id=task.id)

    def start(self, prompt: str, *, task_id: str) -> dict[str, Any]:
        if len(self.running_executors()) >= self.max_running:
            return {"ok": False, "error": "executor capacity reached", "max_running": self.max_running}
        task = self.workspace.get_task(task_id)
        if not task:
            valid_ids = [item.id for item in self.workspace.list_tasks()]
            self.workspace.log_event(
                "executor.invalid_start", {"task_id": task_id, "valid_task_ids": valid_ids}
            )
            return {
                "ok": False,
                "error": f"unknown task: {task_id}; use an id returned by create_task/list_tasks",
                "valid_task_ids": valid_ids,
            }
        if task.status != TaskStatus.TODO or not self.workspace.is_leaf(task_id):
            return {"ok": False, "error": "only todo leaf tasks can start executors"}
        exec_id = self._new_executor_id()
        session_dir = self.workspace.root / "executor-sessions" / exec_id
        agent_kwargs = dict(self.agent_kwargs)
        agent_kwargs["session_dir"] = session_dir
        agent = self._make_agent(agent_kwargs)
        target_id = browser_target_id(task_id=task_id)
        injected = self._inject(prompt, target_id=target_id)
        future = agent.chat_in_background(
            injected, system_context=self.workspace.floating_context()
        )
        future.add_done_callback(
            lambda _future: self._save_session(agent, session_dir=str(session_dir), exec_id=exec_id)
        )
        future.add_done_callback(lambda _future: self._wake_supervisor())
        rec = ExecutorRecord(
            id=exec_id,
            task_id=task_id,
            agent=agent,
            future=future,
            prompt=prompt,
            target_id=target_id,
            session_dir=str(session_dir),
            status="running",
        )
        with self._lock:
            self.executors[exec_id] = rec
        self.workspace.update_task(
            task_id, status=TaskStatus.RUNNING, executor_id=exec_id
        )
        self.workspace.log_event(
            "executor.started",
            {
                "executor_id": exec_id,
                "task_id": task_id,
                "target_id": target_id,
                "session_dir": str(session_dir),
            },
        )
        return {
            "ok": True,
            "executor_id": exec_id,
            "task_id": task_id,
            "target_id": target_id,
            "session_dir": str(session_dir),
        }

    def continue_executor(self, exec_id: str, prompt: str) -> dict[str, Any]:
        rec = self.executors.get(exec_id)
        if not rec:
            return {"ok": False, "error": f"unknown executor: {exec_id}"}
        if rec.future and not rec.future.done():
            return {"ok": False, "error": f"executor still running: {exec_id}"}
        rec.status = "running"
        rec.updated_at = utc_now()
        rec.prompt = prompt
        rec.future = rec.agent.chat_in_background(
            self._inject(prompt, target_id=rec.target_id),
            system_context=self.workspace.floating_context(),
        )
        rec.future.add_done_callback(
            lambda _future: self._save_session(
                rec.agent, session_dir=rec.session_dir, exec_id=rec.id
            )
        )
        rec.future.add_done_callback(lambda _future: self._wake_supervisor())
        self.workspace.log_event(
            "executor.continued", {"executor_id": exec_id, "task_id": rec.task_id}
        )
        return {"ok": True, "executor_id": exec_id}

    def _save_session(self, agent: Enkiball, *, session_dir: str, exec_id: str) -> None:
        """Persist the complete Enkiball transcript, including tool messages."""
        try:
            record = agent.save_session()
            self.workspace.log_event(
                "executor.session_saved",
                {"executor_id": exec_id, "session_dir": session_dir, "session_id": record.session_id if record else ""},
            )
        except Exception as exc:
            self.workspace.log_event(
                "executor.session_save_error",
                {"executor_id": exec_id, "session_dir": session_dir, "error": str(exc)},
            )

    def _wake_supervisor(self) -> None:
        callback = getattr(self, "wake_callback", None)
        if callback:
            callback()

    def interrupt(self, exec_id: str, reason: str = "") -> dict[str, Any]:
        rec = self.executors.get(exec_id)
        if not rec:
            return {"ok": False, "error": f"unknown executor: {exec_id}"}
        rec.agent.interrupt()
        rec.status = "interrupted"
        rec.updated_at = utc_now()
        if rec.task_id:
            task = self.workspace.get_task(rec.task_id)
            if task and task.status == TaskStatus.RUNNING:
                snap = rec.agent.capture()
                self.workspace.mark_executor_finished(
                    rec.task_id,
                    executor_id=rec.id,
                    content=snap.content,
                    status="interrupted",
                    error=reason,
                )
                self.workspace.update_task(
                    rec.task_id,
                    status=TaskStatus.REVIEW,
                    executor_id="",
                    blocked_reason=reason,
                )
        self._save_session(rec.agent, session_dir=rec.session_dir, exec_id=rec.id)
        self.workspace.log_event(
            "executor.interrupted", {"executor_id": exec_id, "reason": reason}
        )
        return {"ok": True, "executor_id": exec_id, "reason": reason}

    def capture(self, exec_id: str) -> dict[str, Any]:
        rec = self.executors.get(exec_id)
        if not rec:
            return {"ok": False, "error": f"unknown executor: {exec_id}"}
        snap = rec.agent.capture()
        return {
            "ok": True,
            "executor_id": exec_id,
            "task_id": rec.task_id,
            "target_id": rec.target_id,
            "status": rec.status,
            "in_progress": snap.in_progress,
            "content": snap.content,
            "thinking": snap.thinking,
            "tool_events": snap.tool_events,
            "elapsed": snap.elapsed,
        }

    def collect_finished(self) -> list[dict[str, Any]]:
        """Collect completed executor results and mark records stable."""
        out: list[dict[str, Any]] = []
        with self._lock:
            records = list(self.executors.values())
        for rec in records:
            if rec.status != "running" or not rec.future or not rec.future.done():
                continue
            try:
                result = rec.future.result()
                rec.status = "done"
                rec.updated_at = utc_now()
                payload = {
                    "executor_id": rec.id,
                    "task_id": rec.task_id,
                    "content": result.content,
                    "interrupted": result.interrupted,
                }
                out.append(payload)
                self.workspace.log_event("executor.finished", payload)
                if rec.task_id:
                    self.workspace.mark_executor_finished(
                        rec.task_id,
                        executor_id=rec.id,
                        content=result.content,
                        status="interrupted" if result.interrupted else "finished",
                    )
            except Exception as exc:  # noqa: BLE001
                rec.status = "failed"
                rec.last_error = str(exc)
                rec.updated_at = utc_now()
                payload = {
                    "executor_id": rec.id,
                    "task_id": rec.task_id,
                    "error": str(exc),
                }
                out.append(payload)
                self.workspace.log_event("executor.failed", payload)
                if rec.task_id:
                    self.workspace.mark_executor_finished(
                        rec.task_id,
                        executor_id=rec.id,
                        content="",
                        status="failed",
                        error=str(exc),
                    )
        return out

    def list_executors(self) -> list[dict[str, Any]]:
        self.collect_finished()
        with self._lock:
            return [rec.to_summary() for rec in self.executors.values()]

    def running_executors(self) -> list[dict[str, Any]]:
        return [item for item in self.list_executors() if item.get("status") == "running"]

    def _inject(self, prompt: str, *, target_id: str) -> str:
        return (
            "<browser_target>\n"
            f"targetId: {target_id}\n"
            "If Chrome MCP tools are used, call chrome_target_create with this targetId "
            "and background=true before other Chrome MCP calls. Pass this targetId to "
            "every Chrome MCP call. Never rely on the active tab.\n"
            "</browser_target>\n\n"
            f"<assigned_prompt>\n{prompt}\n</assigned_prompt>"
        )

    def _task_context(self, task_id: str) -> str:
        task = self.workspace.get_task(task_id)
        if not task:
            return ""
        parts: list[str] = []
        if task.completion_criteria:
            parts.append(f"Completion criteria:\n{task.completion_criteria}")
        if task.context:
            parts.append(f"Task context:\n{task.context}")
        ancestors: list[str] = []
        parent = task.parent_id
        while parent:
            item = self.workspace.get_task(parent)
            if not item:
                break
            ancestors.append(f"{item.id} {item.title}: {item.goal}")
            parent = item.parent_id
        if ancestors:
            parts.append("Task ancestry:\n" + "\n".join(reversed(ancestors)))
        parts.append(
            "Global contest facts are available through read-only get_context. "
            "Follow the exercise context and fetched contest rules."
        )
        return "\n\n".join(parts)

    # -- Tool wrappers ------------------------------------------------------

    def tool_start_task(self, args: dict[str, Any]) -> str:
        res = self.start_for_task(str(args.get("task_id", "")))
        return json.dumps(res, ensure_ascii=False)

    def tool_capture_executor(self, args: dict[str, Any]) -> str:
        res = self.capture(str(args.get("executor_id", "")))
        return json.dumps(res, ensure_ascii=False)

    def tool_interrupt_executor(self, args: dict[str, Any]) -> str:
        res = self.interrupt(
            str(args.get("executor_id", "")), reason=str(args.get("reason", ""))
        )
        return json.dumps(res, ensure_ascii=False)

    def tool_list_executors(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": True, "executors": self.list_executors()}, ensure_ascii=False)
