"""Enkiball Teacher — web server entry point.

Start:  ./start.sh
Stop:   ./stop.sh
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from teacher_core import TeacherMaster

WEB_DIR = Path(__file__).parent / "web" / "static"
TEACHER_DATA_DIR = Path(__file__).parent / "data"
TEACHER_CONFIG_PATH = Path(__file__).parent / "config" / "enki.json"

app = FastAPI(title="Enkiball Teacher", docs_url=None, redoc_url=None)

teacher = TeacherMaster(
    data_dir=TEACHER_DATA_DIR,
    config_path=TEACHER_CONFIG_PATH,
)
_lock = asyncio.Lock()


@app.on_event("shutdown")
async def _shutdown() -> None:
    teacher.shutdown()


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def _index() -> HTMLResponse:
    html_path = WEB_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def _chat(request: Request) -> StreamingResponse:
    body = await request.json()
    message = str(body.get("message", "")).strip()
    if not message:
        return StreamingResponse(
            _iter_json({"type": "done", "data": {"content": "", "error": "empty message"}}),
            media_type="text/event-stream",
        )

    async with _lock:

        async def _event_stream():
            try:
                async for event in teacher.agent.astream_events(message):
                    yield _sse_line(_serialize_event(event))
            except Exception as exc:
                yield _sse_line(
                    json.dumps({"type": "error", "data": str(exc)}, ensure_ascii=False)
                )
            finally:
                teacher.save_current_project()

        return StreamingResponse(_event_stream(), media_type="text/event-stream")


@app.get("/api/state")
async def _get_state() -> dict:
    return teacher.state


@app.get("/api/history")
async def _get_history() -> dict:
    msgs = teacher.agent.history()
    return {"messages": msgs}


@app.get("/api/messages")
async def _get_messages() -> dict:
    msgs = teacher.agent.history()
    return {"messages": msgs}


@app.post("/api/reset")
async def _reset() -> dict:
    teacher.agent.reset()
    teacher.save_current_project()
    return {"ok": True}


@app.post("/api/revert")
async def _revert() -> dict:
    n = teacher.agent.revert(steps=1)
    teacher.save_current_project()
    return {"ok": True, "reverted": n}


# ---------------------------------------------------------------------------
# Project API
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def _list_projects() -> dict:
    return {"projects": teacher.list_projects()}


@app.post("/api/projects")
async def _create_project(request: Request) -> dict:
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "name is required"}
    proj = teacher.create_project(name)
    return {"ok": True, "project": proj}


@app.post("/api/projects/{project_id}/switch")
async def _switch_project(project_id: str) -> dict:
    try:
        result = teacher.switch_project(project_id)
        return {"ok": True, "project": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.delete("/api/projects/{project_id}")
async def _delete_project(project_id: str) -> dict:
    ok = teacher.delete_project(project_id)
    return {"ok": ok}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_event(event: dict) -> str:
    from enkiball import ChatResult

    data = dict(event)
    val = data.get("data")
    if isinstance(val, ChatResult):
        data["data"] = {
            "content": val.content,
            "tool_calls": val.tool_calls,
            "thinking": val.thinking,
            "interrupted": val.interrupted,
        }
    return json.dumps(data, ensure_ascii=False)


def _sse_line(payload: str) -> str:
    return f"data: {payload}\n\n"


async def _iter_json(data: dict) -> ...:
    yield _sse_line(json.dumps(data, ensure_ascii=False))
