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

    def __init__(self, workspace: WebHackWorkspace, *, max_running: int = 3) -> None:
        self.workspace = workspace
        self.max_running = max_running
        self._lock = threading.RLock()
        self._next_id = 0
        self.executors: dict[str, ExecutorRecord] = {}

    def _new_executor_id(self) -> str:
        with self._lock:
            self._next_id += 1
            return f"exec-{self._next_id}"

    def _make_agent(self) -> Enkiball:
        agent = Enkiball(system_prompt=EXECUTOR_PROMPT)
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
            return {"ok": False, "error": f"unknown task: {task_id}"}
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
            return {"ok": False, "error": f"unknown task: {task_id}"}
        if task.status != TaskStatus.TODO or not self.workspace.is_leaf(task_id):
            return {"ok": False, "error": "only todo leaf tasks can start executors"}
        exec_id = self._new_executor_id()
        agent = self._make_agent()
        target_id = browser_target_id(task_id=task_id)
        injected = self._inject(prompt, target_id=target_id)
        future = agent.chat_in_background(
            injected, system_context=self.workspace.floating_context()
        )
        rec = ExecutorRecord(
            id=exec_id,
            task_id=task_id,
            agent=agent,
            future=future,
            prompt=prompt,
            target_id=target_id,
            status="running",
        )
        with self._lock:
            self.executors[exec_id] = rec
        self.workspace.update_task(
            task_id, status=TaskStatus.RUNNING, executor_id=exec_id
        )
        self.workspace.log_event(
            "executor.started",
            {"executor_id": exec_id, "task_id": task_id, "target_id": target_id},
        )
        return {
            "ok": True,
            "executor_id": exec_id,
            "task_id": task_id,
            "target_id": target_id,
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
        self.workspace.log_event(
            "executor.continued", {"executor_id": exec_id, "task_id": rec.task_id}
        )
        return {"ok": True, "executor_id": exec_id}

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
            "Global context is available through read-only get_context. Follow human notes and state graph constraints."
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
