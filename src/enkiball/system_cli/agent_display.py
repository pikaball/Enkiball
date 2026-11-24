"""Compact terminal presentation for system CLI agent orchestration."""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from ..cli_renderer import TerminalMarkdown, _syntax_text, parse_tool_event


def _plain(value: Any, limit: int = 100) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _details(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    return text if len(text) <= 12000 else text[:12000] + "\n… (preview truncated)"


def _duration(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60)}s"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("s" if number != 1 else "")


def _badge(status: str) -> tuple[str, str]:
    if status in {"completed", "replied", "closed"}:
        return "✓", "green"
    if status in {"failed", "timed_out"}:
        return "!", "red"
    if status in {"cancelled", "stopped"}:
        return "×", "yellow"
    return "●", "cyan"


def _status_label(status: str) -> str:
    return {"closed": "done", "replied": "reply ready", "ready": "starting"}.get(status, status.replace("_", " "))


@dataclass
class AgentView:
    number: int
    name: str
    model: str = ""
    status: str = "ready"
    task: str = ""
    tools: int = 0
    last_tool: str = ""
    started: float = 0
    elapsed: float = 0
    messages_path: str = ""
    events_path: str = ""


@dataclass
class RunView:
    run_id: str
    title: str
    started: float
    code: str = ""
    status: str = "running"
    elapsed: float = 0
    agents: dict[int, AgentView] = field(default_factory=dict)
    responses: list[tuple[str, str]] = field(default_factory=list)
    response_count: int = 0
    error: str = ""
    delivery_tracking: bool = False
    delivered_sequences: set[int] = field(default_factory=set)
    completion_delivered: bool = False
    completion_local: bool = False
    delivery_route: str = ""
    delivery_stage: str = "awaiting results"
    waiting: bool = False
    wait_started: float = 0
    wait_seconds: float = 0
    wait_for_completion: bool = False
    trace_dir: str = ""
    manifest_path: str = ""


class _TaskTree:
    def __init__(self, display, run_id):
        self.display = display
        self.run_id = run_id

    def __rich_console__(self, console, options):
        yield self.display.render(self.run_id)


class AgentTaskDisplay:
    def __init__(self, console: Console, cwd: Path) -> None:
        self.console = console
        self.cwd = cwd
        self.runtime = None
        self._lock = threading.RLock()
        self._runs: dict[str, RunView] = {}
        self._session_id: str | None = None
        self.tool_renderers = {
            "orchestrate": self.render_start,
            "cancel_run": self.render_cancel,
            # Render old saved sessions without re-exposing their retired tools.
            "agent_python": self.render_start,
            "agent_python_wait": self.render_wait,
            "agent_python_cancel": self.render_cancel,
        }

    def set_session(self, session_id: str) -> None:
        with self._lock:
            if session_id != self._session_id:
                self._runs.clear()
                self._session_id = session_id

    def _short_id(self, run_id: str) -> str:
        with self._lock:
            prefix = run_id[:6]
            return run_id if sum(key.startswith(prefix) for key in self._runs) > 1 else prefix

    def _tool_summary(self, event: str) -> str:
        name, args = parse_tool_event(event)
        if name in {"read_file", "read_image", "write_file", "edit"}:
            path = Path(str(args.get("path", "")))
            try:
                path = path.relative_to(self.cwd)
            except ValueError:
                pass
            label = {"read_file": "Read", "read_image": "Image", "write_file": "Write", "edit": "Edit"}[name]
            return _plain(f"{label} {path}", 80)
        if name == "run_command":
            return _plain("Run " + str(args.get("command", "")), 80)
        return _plain(name, 80)

    def on_event(self, event: dict) -> None:
        lines: list[Text] = []
        with self._lock:
            if self._session_id is not None and event.get("session_id") != self._session_id:
                return
            run_id = event["run_id"]
            run = self._runs.setdefault(run_id, RunView(
                run_id, _plain(event["title"], 90), event["started_at"],
            ))
            kind = event["type"]
            when = event["started_at"] + event["elapsed"]
            short_id = self._short_id(run_id)
            if kind == "run_started":
                run.code = event.get("code", "")
                run.delivery_tracking = bool(event.get("delivery_tracking"))
                run.wait_for_completion = event.get("wait", False)
                run.trace_dir = event.get("trace_dir", "")
                run.manifest_path = event.get("manifest_path", "")
                lines.append(Text(f"  #{short_id}  started · /agents {short_id} for details", style="dim"))
            number = event.get("agent")
            agent = None
            if isinstance(number, int):
                agent = run.agents.setdefault(number, AgentView(number, f"Agent {number}"))
            if kind == "agent_started" and agent:
                agent.name = _plain(event.get("name", agent.name), 60)
                agent.model = _plain(event.get("model", ""), 48)
                agent.messages_path = event.get("messages_path", "")
                agent.events_path = event.get("events_path", "")
                agent.started = when
                lines.append(Text(f"  #{short_id}  + {agent.name} · {agent.model}", style="cyan"))
            elif kind == "agent_turn_queued" and agent:
                agent.status = "queued"
                agent.task = _plain(event.get("task", ""), 100)
            elif kind == "agent_turn_started" and agent:
                agent.status = "running"
                agent.model = _plain(event.get("model", agent.model), 48)
                agent.task = _plain(event.get("task", ""), 100)
                if not agent.started:
                    agent.started = when
            elif kind == "agent_tool" and agent:
                agent.tools += 1
                agent.last_tool = self._tool_summary(event.get("tool", ""))
            elif kind == "agent_turn_finished" and agent:
                agent.status = event.get("status", "replied")
                agent.elapsed = max(0, when - agent.started)
                if agent.status in {"failed", "cancelled"}:
                    symbol, style = _badge(agent.status)
                    lines.append(Text(f"  #{short_id}  {symbol} {agent.name} · {agent.status}", style=style))
                    if event.get("error"):
                        lines.append(Text("    " + _plain(event["error"], 140), style="dim red"))
            elif kind == "agent_finished" and agent:
                agent.elapsed = max(0, when - agent.started) if agent.started else 0
                if agent.status not in {"failed", "cancelled"}:
                    agent.status = "closed"
            elif kind == "response":
                source = agent.name if agent else "Python"
                run.response_count += 1
                text = _details(event.get("content", ""))
                run.responses.append((source, text))
                del run.responses[:-12]
                lines.append(Text(
                    f"  #{short_id}  ↳ {source} · update {run.response_count} ready · "
                    + ("buffered until workflow ends" if run.wait_for_completion else "pending delivery")
                    + f" · {_duration(event['elapsed'])}",
                    style="green",
                ))
                lines.append(Text("    " + _plain(text, 110), style="dim"))
            elif kind == "finished":
                run.status = event.get("status", "failed")
                run.elapsed = event["elapsed"]
                run.error = _details(event.get("error", ""))
                for child in run.agents.values():
                    if child.status in {"ready", "queued", "running"}:
                        child.status = "closed" if run.status == "completed" else "stopped"
                        child.elapsed = max(0, when - child.started) if child.started else 0
                symbol, style = _badge(run.status)
                lines.append(Text(
                    f"  #{short_id}  {symbol} {run.title} · {run.status.replace('_', ' ')} · "
                    f"{_count(len(run.agents), 'agent')} · {_count(run.response_count, 'update')} · {_duration(run.elapsed)}",
                    style=style,
                ))
                if run.error:
                    last = next((line for line in reversed(run.error.splitlines()) if line.strip()), run.error)
                    lines.append(Text("    " + _plain(last, 160), style="red"))
            elif kind == "wait_started":
                run.waiting = event.get("until_finished", False) or event.get("milliseconds", 0) > 0
                run.wait_started = when
                run.wait_seconds = event.get("milliseconds", 0) / 1000
                if run.waiting and not self.console.is_terminal:
                    lines.append(Text(
                        f"  #{short_id}  main agent waiting until workflow finishes",
                        style="cyan",
                    ))
            elif kind == "wait_ended":
                run.waiting = False
                if event["reason"] == "yielded":
                    if event.get("milliseconds") == 0:
                        text = "running in background · main agent can continue"
                    else:
                        text = f"wait ended after {_duration(event.get('waited', 0))} · no new result in this batch"
                        if run.status == "running":
                            text += " · task continues in background"
                    lines.append(Text(f"  #{short_id}  {text}", style="dim"))
                elif event["reason"] == "interrupted":
                    lines.append(Text(f"  #{short_id}  main agent's wait interrupted", style="yellow"))
                elif event["reason"] == "failed":
                    lines.append(Text(f"  #{short_id}  main agent's wait failed", style="red"))
            elif kind == "background_started":
                lines.append(Text(f"  #{short_id}  background workflow · responses arrive automatically", style="dim"))
            elif kind == "delivery":
                run.delivery_tracking = True
                run.delivery_route = event["route"]
                if event["stage"] == "queued":
                    run.delivery_stage = (
                        "queued for the next tool result" if event["route"] == "notice"
                        else "queued for an automatic turn"
                    )
                else:
                    run.delivered_sequences.update(event.get("sequences", []))
                    run.completion_delivered |= bool(event.get("terminal"))
                    run.delivery_stage = {
                        "wait": "returned to main agent",
                        "notice": "attached to current main turn",
                        "background": "background report in progress",
                    }[event["route"]]
                    if event["route"] != "background":
                        payload = _count(event["updates"], "update") if event["updates"] else "task status"
                        route = "via wait" if event["route"] == "wait" else "notice attached to current turn"
                        lines.append(Text(f"  #{short_id}  → main agent · {payload} · {route}", style="cyan"))
            elif kind == "background_turn_started":
                title = "Background report" if any(b["updates"] for b in event["batches"]) else "Background status"
                lines.append(Text(f"\n◉ {title} · main agent resumed", style="bold magenta"))
                for batch in event["batches"]:
                    source_run = self._runs.get(batch["run_id"])
                    names = []
                    if source_run:
                        names = [source_run.agents[n].name if n in source_run.agents else "Python"
                                 for n in batch.get("agents", [])]
                    source = ", ".join(names) or (source_run.title if source_run else "task status")
                    payload = _count(batch["updates"], "update") if batch["updates"] else _status_label(batch["status"])
                    lines.append(Text(
                        f"  #{self._short_id(batch['run_id'])} · {source} · {payload}",
                        style="dim magenta", no_wrap=True, overflow="ellipsis",
                    ))
            elif kind == "main_turn_ended":
                run.delivery_stage = f"main turn {event['stage']}"
                if event["stage"] != "finished":
                    lines.append(Text(f"  #{short_id}  main report {event['stage']}", style="yellow"))
            elif kind == "delivery_paused":
                run.waiting = False
                run.delivery_stage = "automatic reporting paused"
            elif kind == "completion_local":
                run.completion_local = True
                lines.append(Text(
                    f"  #{short_id}  completion recorded · no new result text, no extra report",
                    style="dim",
                ))
        for line in lines:
            self.console.print(line, soft_wrap=True)

    def render(self, run_id: str) -> Group:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return Group(Text("No live task details for this run.", style="dim"))
            elapsed = time.monotonic() - run.started if run.status == "running" else run.elapsed
            symbol, style = _badge(run.status)
            heading = Text(f"{symbol} {run.title}", style=f"bold {style}", no_wrap=True, overflow="ellipsis")
            lines: list[Any] = [heading, Text(
                f"  #{self._short_id(run_id)} · {run.status.replace('_', ' ')} · {_count(len(run.agents), 'agent')} · "
                f"{_count(run.response_count, 'update')} · {_duration(elapsed)}", style="dim", no_wrap=True, overflow="ellipsis",
            )]
            if run.waiting:
                waited = max(0, time.monotonic() - run.wait_started)
                lines.insert(0, Text(
                    f"◌ Main agent waiting · {_duration(waited)} · until workflow finishes · Esc interrupts",
                    style="bold cyan", no_wrap=True, overflow="ellipsis",
                ))
            if run.delivery_tracking:
                if not run.waiting:
                    route = " · via " + run.delivery_route if run.delivery_route else ""
                    stage = run.delivery_stage.removeprefix("main ")
                    lines.append(Text("  Main: " + stage + route,
                                      style="dim", no_wrap=True, overflow="ellipsis"))
                delivery = f"{len(run.delivered_sequences)}/{run.response_count} updates delivered"
                if run.status != "running":
                    delivery += (
                        " · completion delivered" if run.completion_delivered
                        else " · completion recorded locally" if run.completion_local
                        else " · completion pending"
                    )
                lines.append(Text("  Delivery: " + delivery, style="dim", no_wrap=True, overflow="ellipsis"))
            children = list(run.agents.values())
            for index, agent in enumerate(children):
                mark, color = _badge(agent.status)
                branch = "└─" if index == len(children) - 1 else "├─"
                elapsed = time.monotonic() - agent.started if agent.status == "running" else agent.elapsed
                row = Text(f"  {branch} {mark} ", style=color, no_wrap=True, overflow="ellipsis")
                row.append(agent.name, style="bold")
                row.append(f"  {_status_label(agent.status)} · {agent.model} · {_duration(max(0, elapsed))}", style="dim")
                lines.append(row)
                detail = ("queued for an execution slot" if agent.status == "queued" else
                          "last: " + agent.last_tool if agent.last_tool else agent.task or "Initializing")
                count = f"{agent.tools} call" + ("s" if agent.tools != 1 else "")
                lines.append(Text(f"       {count} · {detail}", style="dim", no_wrap=True, overflow="ellipsis"))
            if not children and run.status == "running":
                lines.append(Text("  Python is running; no child agent has started yet.", style="dim"))
            if run.error:
                last = next((line for line in reversed(run.error.splitlines()) if line.strip()), run.error)
                lines.append(Text("  " + _plain(last, 160), style="red", no_wrap=True, overflow="ellipsis"))
            return Group(*lines)

    @contextmanager
    def waiting(self, run_id: str):
        # The foreground renderer has flushed its Live before calling a tool.
        # Background runs never start another Live over an active input prompt.
        if self.console.is_terminal:
            with Live(_TaskTree(self, run_id), console=self.console,
                      refresh_per_second=4, transient=True, vertical_overflow="crop"):
                yield
        else:
            yield

    def render_start(self, args: dict, cwd: Path):
        mode = "wait" if args.get("wait") is True else "background" if args.get("wait") is False else ""
        title = _plain(args.get("title", "Agent workflow"), 90)
        return Text(f"└─ Workflow  {title}" + (f" · {mode}" if mode else ""), style="bold cyan")

    def render_wait(self, args: dict, cwd: Path):
        run_id = _plain(args.get("run_id", ""), 64)
        with self._lock:
            run = self._runs.get(run_id)
            title = run.title if run else run_id[:6]
        return Text(f"└─ Wait for agents  {title}  #{self._short_id(run_id)}", style="cyan")

    def render_cancel(self, args: dict, cwd: Path):
        return Text("└─ Cancel workflow  #" + self._short_id(_plain(args.get("run_id", ""), 64)), style="yellow")

    def _resolve(self, prefix: str) -> str:
        prefix = prefix.removeprefix("#")
        if not prefix:
            raise ValueError("Provide a task id from /agents")
        with self._lock:
            matches = [key for key in self._runs if key.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous task id; use /agents to list tasks")
        return matches[0]

    def command(self, argv, context) -> bool:
        console = context.console
        if len(argv) == 2 and argv[1] == "help":
            console.print(
                "/agents                  Current/recent task status\n"
                "/agents <id>             Task details and recent responses\n"
                "/agents code <id>        Inspect the submitted Python\n"
                "/agents logs <id>        Inspect stdout/stderr\n"
                "/agents trace <id>       Locate persistent Agent histories and event records\n"
                "/agents cancel <id>      Stop this Python run and its agents\n"
                "IDs may be unique prefixes. Live views are session-local; disk records survive exit.", markup=False,
            )
            return True
        if len(argv) == 1:
            with self._lock:
                active = [key for key, run in self._runs.items() if run.status == "running"]
                recent = [key for key, run in self._runs.items() if run.status != "running"][-4:]
                runs = active + recent
            if not runs:
                console.print("No agent tasks in this session.", style="dim")
            for run_id in runs:
                console.print(self.render(run_id), soft_wrap=True)
            return True
        if len(argv) == 2:
            run_id = self._resolve(argv[1])
            console.print(self.render(run_id), soft_wrap=True)
            with self._lock:
                responses = list(self._runs[run_id].responses)
                error = self._runs[run_id].error
                manifest_path = self._runs[run_id].manifest_path
            if manifest_path:
                console.print(Text("  Records: " + manifest_path, style="dim"), soft_wrap=True)
            for source, response in responses:
                console.print(Text(f"\n↳ {source}", style="green"))
                console.print(TerminalMarkdown(response), soft_wrap=True)
            if error:
                console.print(Text(error, style="red"), soft_wrap=True)
            return True
        if len(argv) == 3 and argv[1] in {"code", "logs", "trace", "cancel"}:
            run_id = self._resolve(argv[2])
            if argv[1] == "code":
                with self._lock:
                    code = self._runs[run_id].code
                console.print(_syntax_text(code, "python", line_numbers=True), soft_wrap=True)
            elif argv[1] == "logs":
                run = self.runtime._get_run(run_id)
                console.print(Text(_details(run.log or "(no stdout/stderr)")), soft_wrap=True)
            elif argv[1] == "trace":
                with self._lock:
                    run = self._runs[run_id]
                    paths = [(a.name, a.messages_path, a.events_path) for a in run.agents.values()]
                    manifest = run.manifest_path
                console.print(Text("Manifest: " + manifest), soft_wrap=True)
                for name, messages, events in paths:
                    console.print(Text(f"{name}\n  history: {messages}\n  events:  {events}"), soft_wrap=True)
            else:
                result = self.runtime.cancel({"run_id": run_id})
                if result["status"] != "cancelled":
                    console.print(f"Task #{self._short_id(run_id)} is already {result['status']}.", markup=False)
            return True
        console.print("usage: /agents [id | code id | logs id | trace id | cancel id | help]", style="yellow", markup=False)
        return True
