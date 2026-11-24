"""System-only Python agent orchestration and delivery of background events."""

from __future__ import annotations

import copy
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager
from uuid import uuid4

from ..agent import Enkiball
from ..cli import CliTurnContext
from ..sandbox import TurnInterrupted
from ..persistence import atomic_write_json
from .orchestration_prompt import ORCHESTRATE_HELP, ORCHESTRATION_PROMPT
from .orchestration_trace import TraceJournal


@dataclass
class PythonRun:
    run_id: str
    session_id: str | None
    process: subprocess.Popen
    deadline: float
    trace_dir: Path
    wait_for_completion: bool
    title: str = "Agent task"
    provider: str = ""
    model: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: float = field(default_factory=time.monotonic)
    status: str = "running"
    events: deque = field(default_factory=deque)
    activity: list[dict] = field(default_factory=list)
    log: str = ""
    log_offset: int = 0
    delivered_terminal: bool = False
    response_sequence: int = 0
    display_lock: Any = field(default_factory=threading.RLock, repr=False)
    reader: threading.Thread | None = None
    log_reader: threading.Thread | None = None
    finished: threading.Event = field(default_factory=threading.Event, repr=False)
    agent_records: list[dict] = field(default_factory=list)
    trace_error: str | None = None
    stdout_truncated: bool = False
    journal: Any = field(default=None, repr=False)


class AgentPythonRuntime:
    def __init__(
        self, agent: Enkiball, *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        wait_context: Callable[[str], ContextManager] | None = None,
        trace_root: Path | None = None,
    ) -> None:
        self.agent = agent
        self.on_event = on_event
        self.wait_context = wait_context
        self.trace_root = (trace_root or Path.home() / ".config" / "enkiball" / "runs").expanduser().resolve()
        if self.trace_root.is_dir():
            agent._sandbox.set_managed_read_roots("orchestration_traces", [self.trace_root])
        self.base_prompt = agent.system_prompt
        self._condition = threading.Condition(threading.RLock())
        self._runs: dict[str, PythonRun] = {}
        self._closed = False
        self._session_id: str | None = None
        self._paused = False
        self._notices: dict[str, dict] = {}
        self._background_batches: dict[str, list[dict]] = {}
        self._turn_deliveries: dict[str, dict] = {}
        agent.register_tool(self.execute, name="orchestrate", description=ORCHESTRATE_HELP,
                            parameters={"type": "object", "properties": {
                                "code": {"type": "string"},
                                "wait": {"type": "boolean", "description": "Wait for the entire workflow (true), or return immediately (false)"},
                                "title": {"type": "string", "description": "Short user-facing workflow title"},
                                "timeout_sec": {"type": "integer", "default": 600,
                                                "minimum": 1, "maximum": 3600},
                            }, "required": ["code", "wait"], "additionalProperties": False})
        agent.register_tool(self.cancel, name="cancel_run",
                            description="Cancel an orchestrate invocation and its agents. Its recorded traces remain available.",
                            parameters={"type": "object", "properties": {
                                "run_id": {"type": "string"},
                            }, "required": ["run_id"], "additionalProperties": False})
        agent.add_tool_hook(before=self._before_tool, after=self._after_tool)
        agent.system_prompt = self.base_prompt + "\n\n" + ORCHESTRATION_PROMPT + f"\nExecution records root: {self.trace_root}\n"

    def _snapshot(self) -> dict[str, Any]:
        config = self.agent.config
        config["active_provider"] = self.agent.provider
        config["active_model"] = self.agent.model
        sandbox = self.agent._sandbox
        builtin_tools = self.agent._engine.builtin_tools
        if isinstance(builtin_tools, (set, frozenset, tuple)):
            builtin_tools = list(builtin_tools)
        return {
            "config": config, "model": self.agent.model,
            "system_prompt": self.base_prompt, "persist": False,
            "cwd": str(self.agent.cwd), "approval": False,
            "sandbox": sandbox.enabled,
            "sandbox_policy": copy.deepcopy(sandbox._policy),
            "sandbox_network": sandbox.network,
            "sandbox_env": dict(sandbox._env),
            "sandbox_inherit_env": list(sandbox._inherit_env),
            "dir_permissions": [str(p) for p in sandbox.writable_directories],
            "ro_permissions": [str(p) for p in sandbox.read_only_directories],
            "builtin_tools": builtin_tools,
            "skill_dirs": [str(p) for p in self.agent._skill_registry.roots],
            "auto_start_mcp": True,
        }

    @staticmethod
    def _duration(value, default, maximum):
        value = default if value is None else value
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"duration must be an integer between 0 and {maximum}")
        return value

    def execute(self, args: dict) -> dict:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty Python string")
        title = args.get("title", "Agent task")
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        wait = args.get("wait")
        if type(wait) is not bool:
            raise ValueError("wait must be true or false")
        timeout = self._duration(args.get("timeout_sec"), 600, 3600)
        if timeout == 0:
            raise ValueError("timeout_sec must be positive")
        with self._condition:
            if self._closed:
                raise RuntimeError("Python runtime is closed")
            if sum(r.status == "running" for r in self._runs.values()) >= 4:
                raise RuntimeError("4 workflows are already active; wait for their notifications or cancel one")
            self._paused = False
            run_id = uuid4().hex[:12]
            trace_dir = self.trace_root / run_id
            trace_dir.mkdir(parents=True, mode=0o700)
            (trace_dir / "code.py").write_text(code, encoding="utf-8")
            (trace_dir / "responses.jsonl").touch(mode=0o600)
            self.agent._sandbox.set_managed_read_roots("orchestration_traces", [self.trace_root])
            payload = json.dumps({"code": code, "agent_defaults": self._snapshot(),
                                  "run_id": run_id, "trace_dir": str(trace_dir)})
            read_fd, write_fd = os.pipe()
            env = dict(os.environ, ENKIBALL_AGENT_EVENT_FD=str(write_fd))
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "enkiball.system_cli.orchestration_worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    pass_fds=(write_fd,), env=env, start_new_session=True,
                )
            except BaseException:
                os.close(read_fd)
                raise
            finally:
                os.close(write_fd)
            run = PythonRun(run_id, self.agent.session_store.current_session_id,
                            process, time.monotonic() + timeout, trace_dir, wait,
                            title=title.strip()[:120] or "Agent workflow",
                            provider=self.agent.provider, model=self.agent.model)
            run.journal = TraceJournal(trace_dir)
            self._runs[run.run_id] = run
            run.reader = threading.Thread(target=self._read_events, args=(run, read_fd), daemon=True)
            run.log_reader = threading.Thread(target=self._read_log, args=(run,), daemon=True)
            run.log_reader.start()
            run.reader.start()
            threading.Thread(target=self._watch_timeout, args=(run,), daemon=True).start()
        self._notify(run, {"type": "run_started", "code": code, "delivery_tracking": True,
                           "wait": wait, **self._trace_paths(run)})
        try:
            process.stdin.write(payload.encode())
            process.stdin.close()
        except BaseException:
            self._stop(run)
            raise
        if wait:
            return self._wait_until_finished(run)
        self._notify(run, {"type": "background_started"})
        return {"run_id": run.run_id, "status": run.status, "wait": False,
                "events": [], **self._trace_paths(run)}

    @staticmethod
    def _trace_paths(run: PythonRun) -> dict:
        return {"trace_dir": str(run.trace_dir),
                "manifest_path": str(run.trace_dir / "manifest.json"),
                "responses_path": str(run.trace_dir / "responses.jsonl"),
                "trace_error": run.trace_error}

    def _manifest(self, run: PythonRun) -> None:
        atomic_write_json(run.trace_dir / "manifest.json", {
            "run_id": run.run_id, "session_id": run.session_id, "title": run.title,
            "status": run.status, "wait": run.wait_for_completion,
            "provider": run.provider, "model": run.model, "created_at": run.created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cwd": str(self.agent.cwd), "code_path": str(run.trace_dir / "code.py"),
            "events_path": str(run.trace_dir / "events.jsonl"),
            "stdout_path": str(run.trace_dir / "stdout.log"),
            "agents": run.agent_records, **self._trace_paths(run),
        })

    def _notify(self, run: PythonRun, event: dict) -> None:
        with run.display_lock:
            envelope = {"run_id": run.run_id, "session_id": run.session_id,
                        "title": run.title, "started_at": run.started_at,
                        "elapsed": time.monotonic() - run.started_at, **event}
            try:
                run.journal.event("runtime_event", event=envelope)
                if event["type"] == "agent_started":
                    descriptor = {key: event[key] for key in (
                        "agent", "name", "messages_path", "events_path",
                    ) if key in event}
                    descriptor.update(initial_provider=event.get("provider"), initial_model=event.get("model"))
                    if event.get("messages_path"):
                        descriptor["metadata_path"] = str(Path(event["messages_path"]).with_name("agent.json"))
                    run.agent_records.append(descriptor)
                if event["type"] in {"run_started", "agent_started", "finished"}:
                    self._manifest(run)
            except OSError as error:
                run.trace_error = str(error)
                logging.getLogger(__name__).warning("Workflow trace write failed: %s", error)
            if self.on_event is not None:
                try:
                    self.on_event(envelope)
                except Exception:
                    logging.getLogger(__name__).exception("Agent task display failed")

    def _read_events(self, run: PythonRun, fd: int) -> None:
        terminal = None
        try:
            with os.fdopen(fd, encoding="utf-8") as channel:
                for line in channel:
                    event = json.loads(line)
                    if event.get("type") not in {
                        "finished", "agent_tool", "agent_finished", "agent_turn_started", "agent_turn_finished", "agent_turn_queued",
                    }:
                        # Never hold the display lock while waiting for a
                        # consumer: that consumer also publishes delivery UI.
                        with self._condition:
                            while len(run.events) >= 256 and run.status == "running" and not self._closed:
                                self._condition.wait(0.1)
                    # Publish the arrival before any UI delivery acknowledgement,
                    # even when the waiting foreground thread wakes immediately.
                    with run.display_lock:
                        with self._condition:
                            if event.get("type") == "response":
                                run.response_sequence = event["sequence"]
                            if event.get("type") == "finished":
                                terminal = event
                            elif event.get("type") in {
                                "agent_tool", "agent_finished", "agent_turn_started", "agent_turn_finished", "agent_turn_queued",
                            }:
                                if len(run.activity) < 256:
                                    run.activity.append(event)
                            else:
                                run.events.append(event)
                            self._condition.notify_all()
                        if event.get("type") != "finished":
                            self._notify(run, event)
                    if event.get("type") == "finished":
                        break
        except Exception as exc:
            terminal = {"type": "finished", "status": "failed", "error": str(exc)}
        finally:
            code = run.process.wait()
            # Reap commands left in this invocation's process group as well.
            try:
                os.killpg(run.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if run.log_reader is not None:
                run.log_reader.join(timeout=1)
            with run.display_lock:
                with self._condition:
                    if run.status == "running":
                        run.status = (terminal or {}).get("status", "failed")
                    event = terminal or {"type": "finished", "status": run.status,
                                         "exit_code": code}
                    event["status"] = run.status
                    run.events.append(event)
                self._notify(run, event)
                with self._condition:
                    run.finished.set()
                    self._condition.notify_all()

    def _read_log(self, run: PythonRun) -> None:
        try:
            with (run.trace_dir / "stdout.log").open("ab", buffering=0) as logfile:
                while data := run.process.stdout.read1(4096):
                    logfile.write(data)
                    with self._condition:
                        combined = run.log + data.decode("utf-8", errors="replace")
                        run.stdout_truncated |= len(combined) > 64000
                        run.log = combined[:64000]
        finally:
            run.process.stdout.close()

    def _watch_timeout(self, run: PythonRun) -> None:
        with self._condition:
            while run.status == "running":
                remaining = run.deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 1))
            else:
                return
        self._stop(run, "timed_out")

    def _get_run(self, run_id: str) -> PythonRun:
        with self._condition:
            run = self._runs.get(run_id)
            if run is None or run.session_id != self.agent.session_store.current_session_id:
                raise ValueError("unknown run_id in this session")
            return run

    def _take(self, run: PythonRun) -> dict:
        events = []
        size = 0
        while run.events:
            next_size = len(json.dumps(run.events[0], ensure_ascii=False))
            if events and size + next_size > 48000:
                break
            events.append(run.events.popleft())
            size += next_size
        self._condition.notify_all()
        log = run.log[run.log_offset:]
        run.log_offset = len(run.log)
        if run.status != "running":
            run.delivered_terminal = True
        return {"run_id": run.run_id, "status": run.status, "events": events,
                "more_events": bool(run.events), "stdout": log,
                "stdout_truncated": run.stdout_truncated, **self._trace_paths(run)}

    def _wait_until_finished(self, run: PythonRun) -> dict:
        self._notify(run, {"type": "wait_started", "until_finished": True})
        display = self.wait_context(run.run_id) if self.wait_context else nullcontext()
        collected = deque()
        collected_size = 0
        omitted = 0
        output = []
        try:
            with display:
                while True:
                    if self.agent.interrupted:
                        self._stop(run)
                        raise TurnInterrupted
                    with self._condition:
                        packet = self._take(run)
                        for event in packet["events"]:
                            size = len(json.dumps(event, ensure_ascii=False))
                            collected.append((event, size))
                            collected_size += size
                            while len(collected) > 1 and collected_size > 48000:
                                _, removed = collected.popleft()
                                collected_size -= removed
                                omitted += 1
                        if packet["stdout"]:
                            output.append(packet["stdout"])
                        if run.finished.is_set():
                            break
                        self._condition.wait(0.1)
        except BaseException as exc:
            reason = "interrupted" if isinstance(exc, (TurnInterrupted, KeyboardInterrupt)) else "failed"
            self._notify(run, {"type": "wait_ended", "reason": reason})
            raise
        result = {**packet, "wait": True, "events": [event for event, _ in collected],
                  "stdout": "".join(output), "more_events": False,
                  "events_truncated": omitted > 0, "responses_total": run.response_sequence}
        self._notify(run, {"type": "wait_ended", "reason": "received"})
        self._delivery(result, route="wait", stage="delivered")
        return result

    def _delivery(self, packet: dict, *, route: str, stage: str) -> dict:
        responses = [e for e in packet["events"] if e.get("type") == "response"]
        info = {
            "type": "delivery", "route": route, "stage": stage,
            "sequences": [e["sequence"] for e in responses if "sequence" in e],
            "updates": len(responses),
            "agents": list(dict.fromkeys(e.get("agent") for e in responses)),
            "terminal": packet["status"] != "running",
            "status": packet["status"],
        }
        with self._condition:
            run = self._runs.get(packet["run_id"])
        if run is not None:
            self._notify(run, info)
            if stage == "delivered":
                self._turn_deliveries[run.run_id] = info
        return info

    def _stop(self, run: PythonRun, status: str = "cancelled") -> None:
        with self._condition:
            if run.status != "running":
                return
            try:
                os.killpg(run.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            run.status = status
            self._condition.notify_all()
        try:
            run.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(run.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            run.process.wait()
        if run.reader is not None:
            run.reader.join(timeout=1)

    def cancel(self, args: dict) -> dict:
        run = self._get_run(args["run_id"])
        self._stop(run)
        with self._condition:
            return self._take(run)

    def _drain(self) -> list[dict]:
        with self._condition:
            packets = [(run, self._take(run)) for run in self._runs.values()
                       if not run.wait_for_completion and run.session_id == self.agent.session_store.current_session_id
                       and (any(e.get("type") != "agent_started" for e in run.events)
                            or (run.status != "running" and not run.delivered_terminal))]
        deliver = []
        for run, packet in packets:
            if (packet["status"] == "completed" and run.response_sequence > 0
                    and not packet["stdout"]
                    and not any(e.get("type") == "response" for e in packet["events"])):
                # respond() already handed off all result text. A later normal
                # process cleanup is UI bookkeeping, not another model report.
                self._notify(run, {"type": "completion_local"})
            else:
                deliver.append(packet)
        return deliver

    def _before_tool(self, context) -> None:
        if context.name not in {"orchestrate", "cancel_run"} and not self._paused:
            for event in self._drain():
                notice = "Workflow update: " + json.dumps(event, ensure_ascii=False)
                self._notices[notice] = event
                self.agent.notice_queue.append(notice)
                self._delivery(event, route="notice", stage="queued")

    def _after_tool(self, context) -> None:
        if context.error is not None:
            return
        # The engine drains notices into the tool result before invoking this
        # hook. Queued notices alone are not yet a delivery acknowledgement.
        pending = list(self.agent.notice_queue)
        for notice, packet in list(self._notices.items()):
            if notice not in pending:
                del self._notices[notice]
                self._delivery(packet, route="notice", stage="delivered")

    def poll_background(self) -> str | None:
        if self._paused or self._closed:
            return None
        events = self._drain()
        if not events:
            return None
        context = ("Background workflow updates for the current task. Process the findings, "
                "continue useful work or wait for running jobs, and report only verified results.\n"
                + json.dumps(events, ensure_ascii=False))
        self._background_batches[context] = events
        while len(self._background_batches) > 8:
            self._background_batches.pop(next(iter(self._background_batches)))
        for packet in events:
            self._delivery(packet, route="background", stage="queued")
        return context

    def on_background_turn(self, context: str) -> None:
        packets = self._background_batches.pop(context, [])
        batches = []
        for packet in packets:
            info = self._delivery(packet, route="background", stage="delivered")
            batches.append({"run_id": packet["run_id"], **info})
        if batches:
            with self._condition:
                run = self._runs.get(batches[0]["run_id"])
            if run is not None:
                self._notify(run, {"type": "background_turn_started", "batches": batches})

    def on_session_change(self, session_id: str) -> None:
        if session_id != self._session_id:
            for notice in self._notices:
                try:
                    self.agent.notice_queue.remove(notice)
                except ValueError:
                    pass
            self._notices.clear()
            self._background_batches.clear()
            self._turn_deliveries.clear()
            for run in list(self._runs.values()):
                if run.session_id != session_id:
                    self._stop(run)
                    with self._condition:
                        self._runs.pop(run.run_id, None)
            self._session_id = session_id
            self._paused = False

    def on_turn_end(self, context: CliTurnContext) -> None:
        stage = "interrupted" if context.interrupted else "failed" if context.error else "finished"
        for run_id, delivery in self._turn_deliveries.items():
            run = self._runs.get(run_id)
            if run is not None:
                self._notify(run, {**delivery, "type": "main_turn_ended", "stage": stage,
                                   "background": context.background})
        self._turn_deliveries.clear()
        if context.interrupted:
            self._paused = True
            for run in list(self._runs.values()):
                if run.session_id == context.session_id:
                    pending = run.status == "running" or bool(run.events) or any(
                        packet["run_id"] == run.run_id for packet in self._notices.values()
                    )
                    self._stop(run)
                    if pending:
                        self._notify(run, {"type": "delivery_paused"})

    def close(self) -> None:
        self._closed = True
        for run in list(self._runs.values()):
            self._stop(run)
            if run.reader is not None and run.reader is not threading.current_thread():
                run.reader.join(timeout=2)
