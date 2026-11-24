"""One-shot Python workflow with recorded, independently stateful Agents."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import copy
import functools
import inspect
import json
import os
import signal
import sys
import threading
import traceback
from pathlib import Path
from uuid import uuid4

from ..agent import Enkiball
from ..persistence import atomic_write_json
from ..sandbox import TurnInterrupted
from .orchestration_trace import RecordedClient, RecordedAsyncClient, TraceJournal, json_value


async def execute(payload, emit):
    agents = []
    agents_lock = threading.Lock()
    agent_counter = 0
    slots = threading.BoundedSemaphore(8)
    tasks = []
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    cancelled = False
    current_agent = contextvars.ContextVar("respond_agent", default=None)

    def cancel(signum, frame):
        nonlocal cancelled
        cancelled = True
        for agent in agents:
            agent.interrupt()
        if owner is not None:
            loop.call_soon_threadsafe(owner.cancel)
        raise TurnInterrupted("Workflow cancelled")

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)

    class Agent(Enkiball):
        """Enkiball with inherited defaults and a durable execution record."""

        def __init__(self, *, name=None, **kwargs):
            nonlocal agent_counter
            with agents_lock:
                agent_counter += 1
                number = agent_counter
            self._display_id = number
            self._display_name = str(name or f"Agent {number}")[:80]
            self.trace_dir = str(Path(payload["trace_dir"]) / "agents" / f"{number:04d}")
            self.messages_path = str(Path(self.trace_dir) / "messages.json")
            self.events_path = str(Path(self.trace_dir) / "events.jsonl")
            self._trace = TraceJournal(Path(self.trace_dir))
            self._trace.event("agent_initializing", name=self._display_name)
            defaults = copy.deepcopy(payload["agent_defaults"])
            defaults.update(kwargs)
            defaults["persist"] = False
            defaults.pop("state_path", None)
            callback = defaults.get("on_tool_event")

            def tool_event(event):
                emit({"type": "agent_tool", "agent": number, "tool": event})
                if callback:
                    callback(event)

            defaults["on_tool_event"] = tool_event
            try:
                super().__init__(**defaults)
            except BaseException as error:
                self._trace.error("agent_initialization_error", error)
                raise
            with agents_lock:
                agents.append(self)
            self._metadata = {
                "agent": number, "name": self._display_name,
                "provider": self.provider, "model": self.model,
                "status": "ready", "messages_path": self.messages_path,
                "events_path": self.events_path,
            }
            self._save_metadata()
            self._trace.snapshot(self)
            self._install_recording()
            emit({"type": "agent_started", **self._metadata})

        def _save_metadata(self, **changes):
            self._metadata.update(provider=self.provider, model=self.model)
            self._metadata.update(changes)
            atomic_write_json(Path(self.trace_dir) / "agent.json", self._metadata)

        def _install_recording(self):
            engine = self._engine
            sync_client, async_client = engine._client, engine._async_client
            engine._client = lambda: RecordedClient(sync_client(), self._trace, self)
            engine._async_client = lambda: RecordedAsyncClient(async_client(), self._trace, self)

            def before_tool(context):
                self._trace.snapshot(self)
                self._trace.event("tool_started", name=context.name, call_id=context.call_id, args=context.args)

            def after_tool(context):
                result = context.result
                if getattr(result, "image_url", None):
                    result = {"text": str(result), "image_url": result.image_url}
                self._trace.event("tool_finished", name=context.name, call_id=context.call_id,
                                  args=context.args, result=result, error=context.error,
                                  duration_sec=context.duration_sec)
                self._trace.snapshot(self)

            self.add_tool_hook(before=before_tool, after=after_tool)
            for method_name in ("stream_reply", "astream_reply", "compact", "acompact"):
                self._wrap_operation(method_name)

        def _wrap_operation(self, method_name):
            method = getattr(self._engine, method_name)
            signature = inspect.signature(method)

            def submitted(args, kwargs):
                current_agent.set(self._display_id)
                bound = signature.bind(*args, **kwargs).arguments
                inputs = {k: v for k, v in bound.items()
                          if k in {"user_input", "image_paths", "prompt", "system_context", "temporary"}}
                operation = uuid4().hex[:12]
                self._trace.operation.set(operation)
                self._trace.event("operation_queued", operation=operation, method=method_name, inputs=inputs)
                emit({"type": "agent_turn_queued", "agent": self._display_id,
                      "name": self._display_name,
                      "task": str(inputs.get("user_input") or inputs.get("prompt") or "Compact history")[:240]})
                return operation, inputs

            def started(operation, inputs):
                if cancelled:
                    raise TurnInterrupted
                self._save_metadata(status="running", operation=operation)
                self._trace.event("operation_started", operation=operation, method=method_name, inputs=inputs)
                emit({"type": "agent_turn_started", "agent": self._display_id,
                      "name": self._display_name, "provider": self.provider, "model": self.model,
                      "task": str(inputs.get("user_input") or inputs.get("prompt") or "Compact history")[:240]})

            def finished(operation, error):
                status = "cancelled" if isinstance(error, (TurnInterrupted, KeyboardInterrupt, asyncio.CancelledError)) else (
                    "failed" if error else "cancelled" if self.interrupted else "replied"
                )
                if error is not None:
                    self._trace.error("operation_error", error, operation=operation)
                self._trace.snapshot(self)
                self._save_metadata(status=status, operation=operation, usage=self.token_usage.to_dict())
                self._trace.event("operation_finished", operation=operation, status=status, usage=self.token_usage.to_dict())
                emit({"type": "agent_turn_finished", "agent": self._display_id,
                      "status": status, "error": str(error) if error else None})

            if inspect.iscoroutinefunction(method):
                @functools.wraps(method)
                async def wrapped(*args, **kwargs):
                    operation, inputs = submitted(args, kwargs)
                    acquired = False
                    error = None
                    try:
                        while not slots.acquire(blocking=False):
                            if cancelled:
                                raise TurnInterrupted
                            await asyncio.sleep(0.02)
                        acquired = True
                        started(operation, inputs)
                        return await method(*args, **kwargs)
                    except BaseException as exc:
                        error = exc
                        raise
                    finally:
                        try:
                            finished(operation, error)
                        finally:
                            if acquired:
                                slots.release()
            else:
                @functools.wraps(method)
                def wrapped(*args, **kwargs):
                    operation, inputs = submitted(args, kwargs)
                    acquired = False
                    error = None
                    try:
                        try:
                            asyncio.get_running_loop()
                            in_async = True
                        except RuntimeError:
                            in_async = False
                        while not slots.acquire(blocking=False):
                            if cancelled:
                                raise TurnInterrupted
                            if in_async:
                                raise RuntimeError("All 8 slots are busy; use await Agent.achat/acompact in async code")
                            threading.Event().wait(0.02)
                        acquired = True
                        started(operation, inputs)
                        return method(*args, **kwargs)
                    except BaseException as exc:
                        error = exc
                        raise
                    finally:
                        try:
                            finished(operation, error)
                        finally:
                            if acquired:
                                slots.release()
            setattr(self._engine, method_name, wrapped)

    def respond(value):
        if cancelled:
            raise TurnInterrupted
        emit({"type": "response", "agent": current_agent.get(), "content": json_value(value)})

    def spawn(awaitable):
        task = asyncio.create_task(awaitable)
        tasks.append(task)
        return task

    scope = {"__name__": "__orchestrate__", "Agent": Agent, "respond": respond,
             "spawn": spawn, "asyncio": asyncio}
    try:
        code = compile(payload["code"], "<orchestrate>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        result = eval(code, scope, scope)
        if inspect.isawaitable(result):
            await result
        index = 0
        while index < len(tasks):
            batch = tasks[index:]
            index = len(tasks)
            await asyncio.gather(*batch)
        for agent in agents:
            await asyncio.to_thread(agent.wait)
            for future in agent._bg_futures:
                future.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for agent in agents:
            agent.interrupt()
        await asyncio.gather(*tasks, return_exceptions=True)
        for agent in agents:
            try:
                await asyncio.to_thread(agent.shutdown)
            finally:
                agent._trace.snapshot(agent)
                agent._trace.event("agent_closed", operation=None, usage=agent.token_usage.to_dict())
                agent._save_metadata(closed=True, usage=agent.token_usage.to_dict())
                emit({"type": "agent_finished", "agent": agent._display_id,
                      "usage": agent.token_usage.to_dict()})


def main():
    channel = os.fdopen(int(os.environ.pop("ENKIBALL_AGENT_EVENT_FD")), "w", buffering=1)
    lock = threading.RLock()
    response_sequence = 0
    journal = None
    payload = None

    def emit(event):
        nonlocal response_sequence
        with lock:
            if event["type"] == "response":
                response_sequence += 1
                event = {**event, "sequence": response_sequence}
                journal.append("responses.jsonl", event)
            line = json.dumps(event, ensure_ascii=False, default=str)
            if len(line) > 32000:
                event = {**event, "truncated": True}
                for key in ("content", "tool", "error"):
                    if key in event:
                        event[key] = str(event[key])[:24000]
                line = json.dumps(event, ensure_ascii=False, default=str)
            channel.write(line + "\n")
            channel.flush()

    outcome = {"type": "finished", "status": "completed"}
    try:
        payload = json.load(sys.stdin)
        journal = TraceJournal(Path(payload["trace_dir"]))
        os.chdir(payload["agent_defaults"]["cwd"])
        asyncio.run(execute(payload, emit))
    except (TurnInterrupted, KeyboardInterrupt, asyncio.CancelledError):
        outcome["status"] = "cancelled"
    except BaseException:
        outcome.update(status="failed", error=traceback.format_exc(limit=12))
    finally:
        if journal is not None:
            journal.append("worker.jsonl", outcome)
        emit(outcome)
        channel.close()


if __name__ == "__main__":
    main()
