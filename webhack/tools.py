from __future__ import annotations

import json
from typing import Any

from enkiball import Enkiball


class SupervisorToolsMixin:
    """Enkiball tool registration and tool handler methods."""

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
            description=(
                "Create a durable task in the task tree. Use parent_id for subtasks; "
                "a root task is just a task with no parent_id."
            ),
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
            description=(
                "Append one stable factual target/application/state fact. "
                "Do not write task progress or plans."
            ),
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
            description=(
                "Append durable human-provided scope, credentials, constraints, "
                "environment facts, or decisions."
            ),
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
            description=(
                "Start the background WebHack supervision loop after root scope "
                "and constraints are captured."
            ),
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
            return json.dumps(
                {"ok": False, "error": f"unknown task: {task_id}"},
                ensure_ascii=False,
            )
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
