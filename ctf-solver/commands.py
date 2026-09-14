from __future__ import annotations

import ast
import json
from types import SimpleNamespace
from collections import Counter
from typing import Any


class SupervisorCommandsMixin:
    """Interactive CLI command handlers for the CTF solver."""

    def _compact_status_text(self) -> str:
        summary = self.workspace.summary()
        executors = self.executors.list_executors()
        running_executors = [item for item in executors if item.get("status") == "running"]
        counts = summary.get("status_counts", {})
        status_counts = ", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())
        ) or "none"
        loop_state = (
            "running"
            if self._loop_thread and self._loop_thread.is_alive()
            else "stopped"
        )
        tick_state = "busy" if self._tick_running else "idle"
        executor_summary = (
            f"Executors: {len(running_executors)}/{self.executors.max_running} running, "
            f"{len(executors)} total"
        )
        lines = [
            f"CTF solver session: {self.session_id}",
            f"Loop: {loop_state} | tick: {tick_state}",
            f"Tasks: {summary.get('task_count', 0)} ({status_counts})",
            executor_summary,
        ]
        if running_executors:
            lines.append("Running now:")
            task_titles = {task.id: task.title for task in self.workspace.list_tasks()}
            for item in running_executors:
                task_id = str(item.get("task_id") or "-")
                title = task_titles.get(task_id, "")
                task_label = f"{task_id} {title}".strip()
                lines.append(f"- {item.get('id')} -> {task_label}")
        return "\n".join(lines)

    def _compact_task_list_text(self) -> str:
        tasks = self.workspace.list_tasks()
        if not tasks:
            return "Tasks: 0\n(no tasks)"

        counts = Counter(str(task.status) for task in tasks)
        summary = ", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())
        )
        by_parent: dict[str | None, list[Any]] = {}
        for task in tasks:
            by_parent.setdefault(task.parent_id, []).append(task)

        lines = [f"Tasks: {len(tasks)} ({summary})"]

        def walk(parent_id: str | None, depth: int) -> None:
            for task in by_parent.get(parent_id, []):
                indent = "  " * depth
                exec_part = f" exec={task.executor_id}" if task.executor_id else ""
                child_count = len(by_parent.get(task.id, []))
                child_part = f" children={child_count}" if child_count else ""
                lines.append(
                    f"{indent}- {task.id} [{task.status}]{exec_part}{child_part} {task.title}"
                )
                detail = self._task_detail_line(task)
                if detail:
                    lines.append(f"{indent}  {detail}")
                walk(task.id, depth + 1)

        walk(None, 0)
        return "\n".join(lines)

    def _task_detail_line(self, task: Any) -> str:
        if task.status == "blocked" and task.blocked_reason:
            return f"blocked: {self._one_line(task.blocked_reason, 120)}"
        if task.status in {"done", "failed"} and task.result:
            return f"result: {self._one_line(task.result, 120)}"
        if task.goal:
            return f"goal: {self._one_line(task.goal, 120)}"
        return ""

    def _compact_exec_list_text(self) -> str:
        executors = self.executors.list_executors()
        if not executors:
            return "Executors: 0\n(no executors)"

        counts = Counter(str(item.get("status", "unknown")) for item in executors)
        summary = ", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())
        )
        tasks = {task.id: task for task in self.workspace.list_tasks()}
        lines = [f"Executors: {len(executors)} ({summary})"]
        for item in executors:
            task_id = str(item.get("task_id") or "-")
            task = tasks.get(task_id)
            task_title = f" {task.title}" if task else ""
            running = " active" if item.get("running") else ""
            chars = item.get("content_chars", 0)
            lines.append(
                f"- {item.get('id')} [{item.get('status')}{running}] "
                f"task={task_id}{task_title} output={chars} chars"
            )
            last_output = self._one_line(str(item.get("last_output") or ""), 140)
            if last_output:
                lines.append(f"  last: {last_output}")
            if item.get("last_error"):
                lines.append(f"  error: {self._one_line(str(item['last_error']), 140)}")
        return "\n".join(lines)

    @staticmethod
    def _one_line(text: str, limit: int) -> str:
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def cmd_status(self, argv, handler, engine, session) -> bool:
        handler.console.print(self._compact_status_text())
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
            handler.console.print(self._compact_task_list_text())
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
        handler.console.print(
            "usage: /task [list|start <task_id>|stop <task_id> [reason...]|"
            "stop-all [reason...]]",
            style="yellow",
        )
        return True

    def cmd_exec(self, argv, handler, engine, session) -> bool:
        if len(argv) == 1 or argv[1] == "list":
            handler.console.print(self._compact_exec_list_text())
            return True
        if argv[1] == "cap" and len(argv) == 3:
            handler.console.print(
                json.dumps(self.executors.capture(argv[2]), indent=2, ensure_ascii=False)
            )
            return True
        if argv[1] == "stop" and len(argv) >= 3:
            reason = " ".join(argv[3:]) if len(argv) > 3 else "manual"
            handler.console.print(self.executors.interrupt(argv[2], reason=reason))
            return True
        handler.console.print(
            "usage: /exec [list|cap <exec_id>|stop <exec_id> [reason...]]",
            style="yellow",
        )
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
        handler.console.print(
            self.btw(" ".join(argv[1:]), should_stop=handler.should_stop)
        )
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
        def adapt(method):
            def command(argv, context):
                handler = SimpleNamespace(
                    console=context.console,
                    last_raw_line=" ".join(argv),
                    should_stop=lambda: bool(
                        context.should_stop and context.should_stop()
                    ),
                )
                return method(argv, handler, context.agent._engine, context.prompt_session)

            return command

        cli.register_command("status", adapt(self.cmd_status), "Show CTF workspace/executor status")
        cli.register_command("tick", adapt(self.cmd_tick), "Run one supervisor watchdog tick")
        cli.register_command(
            "loop",
            adapt(self.cmd_loop),
            "Start/stop supervisor watchdog loop",
            completions=["start", "stop"],
        )
        cli.register_command(
            "task",
            adapt(self.cmd_task),
            "Task tree: list/start/stop/stop-all",
            completions=["list", "start", "stop", "stop-all"],
        )
        cli.register_command(
            "exec",
            adapt(self.cmd_exec),
            "Executors: list/cap/stop",
            completions=["list", "cap", "stop"],
        )
        cli.register_command(
            "state",
            adapt(self.cmd_state),
            "State graph: show/append",
            completions=["show", "append"],
        )
        # `/btw` is provided by the EnkiballCLI base and keeps the full agent
        # tool set. Do not register a second command here.
        # `/debug` is reserved by EnkiballCLI. The base command already
        # provides the same process-local debugging facility, so registering
        # the copied WebHack handler would make startup fail.
