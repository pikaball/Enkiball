from __future__ import annotations

import json
import threading
from typing import Any, Callable

from enkiball import Enkiball

from .commands import SupervisorCommandsMixin
from .executor import ExecutorController
from .loop import SupervisorLoopMixin
from .prompts import SUPERVISOR_PROMPT
from .tools import SupervisorToolsMixin
from .workspace import TaskStatus, WebHackWorkspace


class CtfSolverSupervisor(
    SupervisorLoopMixin,
    SupervisorToolsMixin,
    SupervisorCommandsMixin,
):
    """Autonomous CTF supervisor facade."""

    def __init__(
        self,
        *,
        workspace: WebHackWorkspace | None = None,
        agent: Enkiball | None = None,
        tick_interval: float = 20.0,
        session_id: str | None = None,
        config_path: str | None = None,
        cwd: str | None = None,
        sandbox_network: str = "host",
        sandbox: bool = True,
        dir_permissions: list[str] | None = None,
        max_running: int = 10,
        supervisor_model: str | None = None,
        executor_model: str = "moonshot/kimi-k3",
    ) -> None:
        self.session_id = session_id or "default"
        self.workspace = workspace or WebHackWorkspace.for_session(self.session_id)
        agent_kwargs = {
            "cwd": cwd,
            "dir_permissions": dir_permissions or ([cwd] if cwd else None),
            "approval": False,
            "sandbox": sandbox,
            "sandbox_network": sandbox_network,
            "session_dir": self.workspace.root / "supervisor-session",
            "model": supervisor_model,
        }
        if config_path:
            agent_kwargs["config_path"] = config_path
        self._agent_kwargs = agent_kwargs
        executor_kwargs = dict(agent_kwargs)
        executor_kwargs["model"] = executor_model
        self.executors = ExecutorController(
            self.workspace, max_running=max_running, agent_kwargs=executor_kwargs
        )
        self.executors.wake_callback = self.wake_loop
        self.agent = agent or Enkiball(system_prompt=SUPERVISOR_PROMPT, **agent_kwargs)
        self.agent.auto_compact_enabled = True
        self.tick_interval = tick_interval
        self._loop_thread: threading.Thread | None = None
        self._loop_stop = threading.Event()
        self._loop_wake = threading.Event()
        self._tick_lock = threading.Lock()
        self._tick_running = False
        self._tick_count = 0
        self._register_tools(self.agent)

    def switch_session(self, session_id: str) -> None:
        """Switch WebHack durable workspace to match the active CLI session.

        Running executors from the old workspace are interrupted because their
        prompts and captured state belong to that session's workspace.
        """
        if session_id == self.session_id:
            return
        for item in self.executors.list_executors():
            if item.get("status") == "running":
                self.executors.interrupt(
                    str(item["id"]), reason="switching webhack session workspace"
                )
        self.session_id = session_id
        self.workspace = WebHackWorkspace.for_session(session_id)
        self.executors = ExecutorController(
            self.workspace,
            max_running=self.executors.max_running,
            agent_kwargs=self.executors.agent_kwargs,
        )
        self.executors.wake_callback = self.wake_loop
        self.workspace.log_event("workspace.session_attached", {"session_id": session_id})

    # -- Background supervisor loop ----------------------------------------

    def start_loop(self) -> None:
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._loop_stop.clear()
        self._loop_wake.set()
        self._loop_thread = threading.Thread(
            target=self._loop, name="ctf-solver-supervisor", daemon=True
        )
        self._loop_thread.start()
        self.workspace.log_event("supervisor.loop_started", {})

    def stop_loop(self) -> None:
        self._loop_stop.set()
        if self._loop_thread:
            self._loop_thread.join(timeout=1.0)
        self.workspace.log_event("supervisor.loop_stopped", {})

    def _loop(self) -> None:
        while not self._loop_stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.workspace.log_event("supervisor.tick_error", {"error": str(exc)})
            self._loop_wake.wait(self.tick_interval)
            self._loop_wake.clear()

    def wake_loop(self) -> None:
        """Wake the scheduler after an executor or task state transition."""
        self._loop_wake.set()

    def tick(self) -> str:
        """Run one watchdog iteration."""
        if not self._tick_lock.acquire(blocking=False):
            self.workspace.log_event("supervisor.tick_skipped", {"reason": "already_running"})
            return "tick skipped: previous tick is still running"
        self._tick_running = True
        try:
            self._tick_count += 1
            self.executors.collect_finished()
            # Reconcile and refill before any model-backed policy work. A
            # slow review must never leave executor capacity idle.
            started = self._auto_start_ready_tasks()
            reviewed = self._review_finished_tasks()
            started.extend(self._auto_start_ready_tasks())
            # Active audits are deliberately periodic: a model call on every
            # ten-second tick steals time from solving and can interrupt a
            # worker before it has made a useful attempt.
            audited = self._audit_running_tasks() if self._tick_count % 6 == 0 else []
            maintained = (
                self._maintain_state_graph()
                if reviewed
                else {"ok": True, "skipped": "no completed tasks"}
            )
            result = json.dumps(
                {
                    "reviewed": reviewed,
                    "audited": audited,
                    "state": maintained,
                    "started": started,
                },
                ensure_ascii=False,
                indent=2,
            )
            self.workspace.log_event(
                "supervisor.tick", {"response_chars": len(result)}
            )
            return result
        finally:
            self._tick_running = False
            self._tick_lock.release()

    def btw(
        self,
        prompt: str,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """Single human-supervisor interaction outside foreground history.

        /btw should see the same prior foreground conversation, but its own
        user/assistant turn must not be appended back to the foreground agent.
        """
        return self.agent.btw(
            prompt,
            system_context=self.workspace.floating_context(),
            should_stop=should_stop,
        ).content

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace.summary(),
            "executors": self.executors.list_executors(),
            "loop_running": bool(self._loop_thread and self._loop_thread.is_alive()),
            "tick_running": self._tick_running,
        }

    def stop_task(self, task_id: str, reason: str = "manual task stop") -> dict[str, Any]:
        task = self.workspace.get_task(task_id)
        if not task:
            return {"ok": False, "error": f"unknown task: {task_id}"}
        if task.executor_id:
            res = self.executors.interrupt(task.executor_id, reason=reason)
            self.workspace.update_task(task_id, status=TaskStatus.REVIEW, executor_id="")
            return {"ok": True, "task_id": task_id, "executor": res}
        if task.status == TaskStatus.RUNNING:
            self.workspace.update_task(task_id, status=TaskStatus.REVIEW, blocked_reason=reason)
            return {"ok": True, "task_id": task_id, "stopped": "running_without_executor"}
        return {"ok": False, "error": f"task is not running: {task_id}", "status": str(task.status)}

    def stop_all_tasks(self, reason: str = "manual stop all tasks") -> dict[str, Any]:
        stopped: list[dict[str, Any]] = []
        for task in self.workspace.list_tasks():
            if task.status == TaskStatus.RUNNING or task.executor_id:
                stopped.append(self.stop_task(task.id, reason=reason))
        return {"ok": True, "stopped": stopped, "count": len(stopped)}

    def inject_user_prompt(
        self, prompt: str, image_paths: list[str] | None = None
    ) -> tuple[str, str]:
        """Attach floating context as a temporary system message.

        This makes every regular user turn state-aware without requiring the
        model to remember to call get_floating_context(), without persisting
        floating context as user-visible chat history.
        """
        return prompt, self.workspace.floating_context()

    def _auto_start_ready_tasks(self) -> list[dict[str, Any]]:
        capacity = self.executors.max_running - len(self.executors.running_executors())
        started: list[dict[str, Any]] = []
        if capacity <= 0:
            return started
        for task in self.workspace.ready_tasks(limit=capacity):
            started.append(self.executors.start_for_task(task.id))
        return started

    def shutdown(self) -> None:
        self.stop_loop()
        for item in self.executors.list_executors():
            if item.get("status") == "running":
                self.executors.interrupt(str(item["id"]), reason="supervisor shutdown")
        self.agent.shutdown()


# Compatibility name inside the copied package; the original webhack package is untouched.
WebHackSupervisor = CtfSolverSupervisor
