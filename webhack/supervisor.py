from __future__ import annotations

import ast
import json
import threading
import time
from typing import Any

from enkiball import Enkiball

from .executor import ExecutorController
from .prompts import ACTIVE_AUDIT_PROMPT, REVIEW_PROMPT, STATE_MAINTENANCE_PROMPT, SUPERVISOR_PROMPT
from .workspace import TaskStatus, WebHackWorkspace


class WebHackSupervisor:
    """Supervisor facade: agent + workspace + executors + watchdog loop."""

    def __init__(
        self,
        *,
        workspace: WebHackWorkspace | None = None,
        agent: Enkiball | None = None,
        tick_interval: float = 20.0,
        session_id: str | None = None,
    ) -> None:
        self.session_id = session_id or "default"
        self.workspace = workspace or WebHackWorkspace.for_session(self.session_id)
        self.executors = ExecutorController(self.workspace)
        self.agent = agent or Enkiball(system_prompt=SUPERVISOR_PROMPT)
        self.agent.auto_compact_enabled = True
        self.tick_interval = tick_interval
        self._loop_thread: threading.Thread | None = None
        self._loop_stop = threading.Event()
        self._tick_lock = threading.Lock()
        self._tick_running = False
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
        self.executors = ExecutorController(self.workspace)
        self.workspace.log_event("workspace.session_attached", {"session_id": session_id})

    # -- Background supervisor loop ----------------------------------------

    def start_loop(self) -> None:
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._loop_stop.clear()
        self._loop_thread = threading.Thread(
            target=self._loop, name="webhack-supervisor", daemon=True
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
            self._loop_stop.wait(self.tick_interval)

    def tick(self) -> str:
        """Run one watchdog iteration."""
        if not self._tick_lock.acquire(blocking=False):
            self.workspace.log_event("supervisor.tick_skipped", {"reason": "already_running"})
            return "tick skipped: previous tick is still running"
        self._tick_running = True
        try:
            self.executors.collect_finished()
            reviewed = self._review_finished_tasks()
            audited = self._audit_running_tasks()
            maintained = self._maintain_state_graph()
            started = self._auto_start_ready_tasks()
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

    def btw(self, prompt: str) -> str:
        """Single human-supervisor interaction outside foreground history.

        /btw should see the same prior foreground conversation, but its own
        user/assistant turn must not be appended back to the foreground agent.
        """
        agent = Enkiball(system_prompt=SUPERVISOR_PROMPT)
        agent.auto_compact_enabled = True
        agent.load_messages(self.agent.messages)
        self._register_read_only_tools(agent)
        try:
            return agent.chat(
                prompt,
                system_context=self.workspace.floating_context(),
            ).content
        finally:
            agent.shutdown()

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

    def _new_loop_agent(self, prompt: str) -> Enkiball:
        agent = Enkiball(system_prompt=prompt)
        agent.auto_compact_enabled = True
        return agent

    def _review_finished_tasks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for task in self.workspace.list_tasks():
            if task.status != TaskStatus.REVIEW:
                continue
            agent = self._new_loop_agent(REVIEW_PROMPT)
            try:
                attempt = task.attempts[-1] if task.attempts else {}
                prompt = (
                    f"<task>\n{json.dumps(task.to_dict(), ensure_ascii=False)}\n</task>\n\n"
                    f"<executor_output>\n{attempt.get('content', task.result)}\n</executor_output>"
                )
                res = agent.chat(prompt, system_context=self.workspace.floating_context(max_chars=60000)).content
                decision = self._parse_json(res)
                applied = self._apply_review_decision(task.id, decision)
                out.append({"task_id": task.id, "decision": decision.get("decision"), "applied": applied})
            except Exception as exc:  # noqa: BLE001
                self.workspace.log_event("review.error", {"task_id": task.id, "error": str(exc)})
                out.append({"task_id": task.id, "error": str(exc)})
            finally:
                agent.shutdown()
        return out

    def _apply_review_decision(self, task_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        kind = str(decision.get("decision", "")).lower()
        result = str(decision.get("result", decision.get("rationale", "")))
        facts = decision.get("state_facts", [])
        if isinstance(facts, list):
            for fact in facts:
                if isinstance(fact, dict):
                    self.workspace.add_state_fact(fact)
        if kind == "done":
            self.workspace.update_task(task_id, status=TaskStatus.DONE, result=result)
            return {"status": "done"}
        if kind == "failed":
            self.workspace.update_task(task_id, status=TaskStatus.FAILED, result=result)
            return {"status": "failed"}
        if kind == "blocked":
            self.workspace.update_task(
                task_id,
                status=TaskStatus.BLOCKED,
                blocked_reason=str(decision.get("blocked_reason", result)),
                result=result,
            )
            return {"status": "blocked"}
        if kind == "decompose":
            children = decision.get("children", [])
            created: list[str] = []
            if isinstance(children, list):
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    new_task = self.workspace.create_task(
                        str(child.get("title", "subtask")),
                        str(child.get("goal", "")),
                        parent_id=task_id,
                        context=str(child.get("context", "")),
                        completion_criteria=str(child.get("completion_criteria", "")),
                    )
                    created.append(new_task.id)
            self.workspace.update_task(task_id, status=TaskStatus.DECOMPOSED, result=result)
            return {"status": "decomposed", "children": created}
        if kind == "retry":
            task = self.workspace.get_task(task_id)
            extra = str(decision.get("retry_context", ""))
            context = ((task.context + "\n\n") if task and task.context else "") + extra
            self.workspace.update_task(task_id, status=TaskStatus.TODO, context=context, result=result)
            return {"status": "todo"}
        self.workspace.update_task(task_id, status=TaskStatus.BLOCKED, blocked_reason="review decision was invalid", result=result)
        return {"status": "blocked", "reason": "invalid decision"}

    def _audit_running_tasks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for task in self.workspace.list_tasks():
            if task.status != TaskStatus.RUNNING or not task.executor_id:
                continue
            cap = self.executors.capture(task.executor_id)
            agent = self._new_loop_agent(ACTIVE_AUDIT_PROMPT)
            try:
                prompt = (
                    f"<task>\n{json.dumps(task.to_dict(), ensure_ascii=False)}\n</task>\n\n"
                    f"<executor_capture>\n{json.dumps(cap, ensure_ascii=False)}\n</executor_capture>"
                )
                res = agent.chat(prompt, system_context=self.workspace.floating_context(max_chars=60000)).content
                decision = self._parse_json(res)
                action = self._apply_active_audit(task.id, task.executor_id, decision)
                out.append({"task_id": task.id, "decision": decision.get("decision"), "action": action})
            except Exception as exc:  # noqa: BLE001
                self.workspace.log_event("active_audit.error", {"task_id": task.id, "error": str(exc)})
                out.append({"task_id": task.id, "error": str(exc)})
            finally:
                agent.shutdown()
        return out

    def _apply_active_audit(self, task_id: str, executor_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        kind = str(decision.get("decision", "continue")).lower()
        reason = str(decision.get("reason", ""))
        if kind == "continue":
            return {"status": "running"}
        self.executors.interrupt(executor_id, reason=reason or kind)
        if kind == "restart":
            task = self.workspace.get_task(task_id)
            extra = str(decision.get("replacement_context", ""))
            context = ((task.context + "\n\n") if task and task.context else "") + extra
            self.workspace.update_task(task_id, status=TaskStatus.TODO, context=context, executor_id="")
            return {"status": "requeued"}
        if kind == "blocked":
            self.workspace.update_task(task_id, status=TaskStatus.BLOCKED, blocked_reason=reason, executor_id="")
            return {"status": "blocked"}
        if kind == "failed":
            self.workspace.update_task(task_id, status=TaskStatus.FAILED, result=reason, executor_id="")
            return {"status": "failed"}
        self.workspace.update_task(task_id, status=TaskStatus.REVIEW, blocked_reason=reason, executor_id="")
        return {"status": "review"}

    def _maintain_state_graph(self) -> dict[str, Any]:
        agent = self._new_loop_agent(STATE_MAINTENANCE_PROMPT)
        try:
            res = agent.chat(
                "Normalize the current state graph. Return the complete normalized graph JSON.",
                system_context=self.workspace.floating_context(max_chars=60000),
            ).content
            graph = self._parse_json(res)
            if "facts" in graph:
                self.workspace.write_state_graph(graph)
                return {"ok": True, "facts": len(graph.get("facts", []))}
            return {"ok": False, "error": "missing facts"}
        except Exception as exc:  # noqa: BLE001
            self.workspace.log_event("state_maintenance.error", {"error": str(exc)})
            return {"ok": False, "error": str(exc)}
        finally:
            agent.shutdown()

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
        return data

    def shutdown(self) -> None:
        self.stop_loop()
        for item in self.executors.list_executors():
            if item.get("status") == "running":
                self.executors.interrupt(str(item["id"]), reason="supervisor shutdown")
        self.agent.shutdown()

    # -- Tool registration --------------------------------------------------

    def _register_read_only_tools(self, agent: Enkiball) -> None:
        agent.register_tool(
            self.tool_get_context,
            name="get_floating_context",
            description="Return current task tree, state graph, human notes, and recent events.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        agent.register_tool(
            self.tool_list_tasks,
            name="list_tasks",
            description="List current tasks as a compact tree.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        agent.register_tool(
            self.tool_list_executors,
            name="list_executors",
            description="List executors and their latest captured progress.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        agent.register_tool(
            self.tool_capture_executor,
            name="capture_executor",
            description="Capture an executor's current output.",
            parameters={
                "type": "object",
                "properties": {"executor_id": {"type": "string"}},
                "required": ["executor_id"],
                "additionalProperties": False,
            },
        )

    def _register_tools(self, agent: Enkiball) -> None:
        agent.register_tool(
            self.tool_get_context,
            name="get_floating_context",
            description="Return current task tree, state graph, human notes, and recent events.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        agent.register_tool(
            self.tool_create_task,
            name="create_task",
            description="Create a durable task in the task tree. Use parent_id for subtasks; a root task is just a task with no parent_id.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "context": {"type": "string"},
                    "completion_criteria": {"type": "string"},
                },
                "required": ["title", "goal"],
                "additionalProperties": False,
            },
        )
        agent.register_tool(
            self.tool_update_task,
            name="update_task",
            description="Update durable task fields such as status, result, executor_id, context.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "status": {"type": "string"},
                    "result": {"type": "string"},
                    "context": {"type": "string"},
                    "completion_criteria": {"type": "string"},
                    "executor_id": {"type": "string"},
                    "blocked_reason": {"type": "string"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        )
        agent.register_tool(
            self.tool_list_tasks,
            name="list_tasks",
            description="List current tasks as a compact tree.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        agent.register_tool(
            self.tool_append_state,
            name="append_state_change",
            description="Append one stable factual target/application/state fact. Do not write task progress or plans.",
            parameters={
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "subject": {"type": "string"},
                    "fact": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string"},
                },
                "required": ["fact"],
                "additionalProperties": False,
            },
        )
        agent.register_tool(
            self.tool_append_human_note,
            name="append_human_note",
            description="Append durable human-provided scope, credentials, constraints, environment facts, or decisions.",
            parameters={
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
                "additionalProperties": False,
            },
        )
        agent.register_tool(
            self.tool_start_loop,
            name="start_loop",
            description="Start the background WebHack supervision loop after root scope and constraints are captured.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        agent.register_tool(
            self.tool_start_task_executor,
            name="start_task_executor",
            description="Start an executor for an existing todo leaf task by id.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        )
        agent.register_tool(
            self.tool_list_executors,
            name="list_executors",
            description="List executors and their latest captured progress.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        agent.register_tool(
            self.tool_capture_executor,
            name="capture_executor",
            description="Capture an executor's current output.",
            parameters={
                "type": "object",
                "properties": {"executor_id": {"type": "string"}},
                "required": ["executor_id"],
                "additionalProperties": False,
            },
        )
        agent.register_tool(
            self.tool_interrupt_executor,
            name="interrupt_executor",
            description="Interrupt an executor that is drifting, done enough, or unsafe.",
            parameters={
                "type": "object",
                "properties": {
                    "executor_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["executor_id"],
                "additionalProperties": False,
            },
        )

    # -- Tool handlers ------------------------------------------------------

    def tool_get_context(self, args: dict[str, Any]) -> str:
        return self.workspace.floating_context()

    def tool_create_task(self, args: dict[str, Any]) -> str:
        task = self.workspace.create_task(
            str(args["title"]),
            str(args["goal"]),
            parent_id=args.get("parent_id") or None,
            context=str(args.get("context", "")),
            completion_criteria=str(args.get("completion_criteria", "")),
        )
        return json.dumps({"ok": True, "task": task.to_dict()}, ensure_ascii=False)

    def tool_update_task(self, args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", ""))
        changes = {k: v for k, v in args.items() if k != "task_id" and v is not None}
        task = self.workspace.update_task(task_id, **changes)
        if not task:
            return json.dumps({"ok": False, "error": f"unknown task: {task_id}"}, ensure_ascii=False)
        return json.dumps({"ok": True, "task": task.to_dict()}, ensure_ascii=False)

    def tool_list_tasks(self, args: dict[str, Any]) -> str:
        return json.dumps(
            {"ok": True, "tree": self.workspace.task_tree_text()}, ensure_ascii=False
        )

    def tool_append_state(self, args: dict[str, Any]) -> str:
        if "note" in args and "fact" not in args:
            self.workspace.append_state_change(str(args.get("note", "")))
        else:
            self.workspace.add_state_fact(args)
        return json.dumps({"ok": True}, ensure_ascii=False)

    def tool_append_human_note(self, args: dict[str, Any]) -> str:
        self.workspace.append_human_note(str(args.get("note", "")))
        return json.dumps({"ok": True}, ensure_ascii=False)

    def tool_start_loop(self, args: dict[str, Any]) -> str:
        self.start_loop()
        return json.dumps({"ok": True}, ensure_ascii=False)

    def tool_start_task_executor(self, args: dict[str, Any]) -> str:
        return self.executors.tool_start_task(args)

    def tool_list_executors(self, args: dict[str, Any]) -> str:
        return self.executors.tool_list_executors(args)

    def tool_capture_executor(self, args: dict[str, Any]) -> str:
        return self.executors.tool_capture_executor(args)

    def tool_interrupt_executor(self, args: dict[str, Any]) -> str:
        return self.executors.tool_interrupt_executor(args)

    # -- CLI command handlers ----------------------------------------------

    def cmd_status(self, argv, handler, engine, session) -> bool:
        handler.console.print(json.dumps(self.status(), indent=2, ensure_ascii=False))
        return True

    def cmd_tick(self, argv, handler, engine, session) -> bool:
        handler.console.print("running supervisor tick...", style="cyan")
        handler.console.print(self.tick())
        return True

    def cmd_loop(self, argv, handler, engine, session) -> bool:
        if len(argv) < 2 or argv[1] not in {"start", "stop"}:
            handler.console.print("usage: /loop [start|stop]", style="yellow")
            return True
        if argv[1] == "start":
            self.start_loop()
            handler.console.print("supervisor loop started", style="green")
        else:
            self.stop_loop()
            handler.console.print("supervisor loop stopped", style="green")
        return True

    def cmd_task(self, argv, handler, engine, session) -> bool:
        if len(argv) == 1 or argv[1] == "list":
            handler.console.print(self.workspace.task_tree_text())
            return True
        if argv[1] == "start" and len(argv) == 3:
            handler.console.print(self.executors.start_for_task(argv[2]))
            return True
        if argv[1] == "stop" and len(argv) >= 3:
            reason = " ".join(argv[3:]) if len(argv) > 3 else "manual task stop"
            handler.console.print(self.stop_task(argv[2], reason=reason))
            return True
        if argv[1] == "stop-all":
            reason = " ".join(argv[2:]) if len(argv) > 2 else "manual stop all tasks"
            handler.console.print(self.stop_all_tasks(reason=reason))
            return True
        handler.console.print("usage: /task [list|start <task_id>|stop <task_id> [reason...]|stop-all [reason...]]", style="yellow")
        return True

    def cmd_exec(self, argv, handler, engine, session) -> bool:
        if len(argv) == 1 or argv[1] == "list":
            handler.console.print(json.dumps(self.executors.list_executors(), indent=2, ensure_ascii=False))
            return True
        if argv[1] == "cap" and len(argv) == 3:
            handler.console.print(json.dumps(self.executors.capture(argv[2]), indent=2, ensure_ascii=False))
            return True
        if argv[1] == "stop" and len(argv) >= 3:
            reason = " ".join(argv[3:]) if len(argv) > 3 else "manual"
            handler.console.print(self.executors.interrupt(argv[2], reason=reason))
            return True
        handler.console.print("usage: /exec [list|cap <exec_id>|stop <exec_id> [reason...]]", style="yellow")
        return True

    def cmd_state(self, argv, handler, engine, session) -> bool:
        if len(argv) == 1 or argv[1] == "show":
            handler.console.print(self.workspace.read_state())
            return True
        if argv[1] == "append" and len(argv) >= 3:
            self.workspace.append_state_change(" ".join(argv[2:]))
            handler.console.print("state updated", style="green")
            return True
        handler.console.print("usage: /state [show|append <note...>]", style="yellow")
        return True

    def cmd_btw(self, argv, handler, engine, session) -> bool:
        if len(argv) < 2:
            handler.console.print("usage: /btw <prompt...>", style="yellow")
            return True
        handler.console.print(self.btw(" ".join(argv[1:])))
        return True

    def cmd_debug(self, argv, handler, engine, session) -> bool:
        code = handler.last_raw_line.removeprefix("/debug").strip()
        if not code:
            handler.console.print(
                "usage: /debug <python expression or statement>", style="yellow"
            )
            handler.console.print(
                "locals: supervisor, workspace, executors, agent, "
                "executor(id), executor_agent(id)",
                style="dim",
            )
            return True

        def executor(exec_id: str) -> Any:
            return self.executors.executors.get(exec_id)

        def executor_agent(exec_id: str) -> Any:
            rec = executor(exec_id)
            return rec.agent if rec else None

        scope: dict[str, Any] = {
            "supervisor": self,
            "workspace": self.workspace,
            "executors": self.executors,
            "agent": self.agent,
            "executor": executor,
            "executor_agent": executor_agent,
        }

        try:
            parsed = ast.parse(code, mode="eval")
            result = eval(compile(parsed, "<webhack-debug>", "eval"), scope, scope)  # noqa: S307
            handler.console.print(repr(result))
        except SyntaxError:
            try:
                exec(compile(code, "<webhack-debug>", "exec"), scope, scope)  # noqa: S102
            except Exception as exc:  # noqa: BLE001
                handler.console.print(f"debug error: {exc!r}", style="bold red")
        except Exception as exc:  # noqa: BLE001
            handler.console.print(f"debug error: {exc!r}", style="bold red")
        return True

    def install_cli_commands(self, cli: Any) -> None:
        cli.register_command("status", self.cmd_status, "Show webhack workspace/executor status")
        cli.register_command("tick", self.cmd_tick, "Run one supervisor watchdog tick")
        cli.register_command("loop", self.cmd_loop, "Start/stop supervisor watchdog loop", completions=["start", "stop"])
        cli.register_command(
            "task",
            self.cmd_task,
            "Task tree: list/start/stop/stop-all",
            completions=["list", "start", "stop", "stop-all"],
        )
        cli.register_command(
            "exec",
            self.cmd_exec,
            "Executors: list/cap/stop",
            completions=["list", "cap", "stop"],
        )
        cli.register_command(
            "state",
            self.cmd_state,
            "State graph: show/append",
            completions=["show", "append"],
        )
        cli.register_command("btw", self.cmd_btw, "One-off supervisor chat without intended durable mutation")
        cli.register_command("debug", self.cmd_debug, "Eval Python in WebHack process")
