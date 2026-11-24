"""Durable run records and transparent recording of an Agent's model clients."""

from __future__ import annotations

import json
import contextvars
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ..persistence import atomic_write_json


def _json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, SimpleNamespace):
        return vars(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    return str(value)


def json_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


class TraceJournal:
    """Append and flush each event; never keep the only copy in worker memory."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._sequence = 0
        self.operation = contextvars.ContextVar("trace_operation", default=None)

    def event(self, kind: str, **data) -> None:
        with self._lock:
            self._sequence += 1
            self.append("events.jsonl", {
                "time": datetime.now(timezone.utc).isoformat(),
                "sequence": self._sequence, "type": kind,
                "operation": self.operation.get(), **data,
            })

    def append(self, filename: str, value) -> None:
        line = json.dumps(value, ensure_ascii=False, default=_json_default) + "\n"
        with self._lock:
            fd = os.open(self.directory / filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(line)
                file.flush()

    def snapshot(self, agent) -> None:
        with self._lock:
            atomic_write_json(self.directory / "messages.json", agent.messages)

    def error(self, kind: str, error: BaseException, **data) -> None:
        self.event(kind, error=error, traceback=traceback.format_exc(), **data)


class RecordedStream:
    def __init__(self, stream, journal: TraceJournal, request_id: str):
        self.stream, self.journal, self.request_id = stream, journal, request_id

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __iter__(self):
        try:
            for chunk in self.stream:
                self.journal.event("llm_chunk", request_id=self.request_id, chunk=chunk)
                yield chunk
        except BaseException as error:
            self.journal.error("llm_stream_error", error, request_id=self.request_id)
            raise

    def close(self):
        try:
            result = self.stream.close()
        except BaseException as error:
            self.journal.error("llm_stream_close_error", error, request_id=self.request_id)
            raise
        else:
            self.journal.event("llm_stream_closed", request_id=self.request_id)
            return result


class RecordedAsyncStream(RecordedStream):
    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                self.journal.event("llm_chunk", request_id=self.request_id, chunk=chunk)
                yield chunk
        except BaseException as error:
            self.journal.error("llm_stream_error", error, request_id=self.request_id)
            raise

    async def close(self):
        try:
            result = await self.stream.close()
        except BaseException as error:
            self.journal.error("llm_stream_close_error", error, request_id=self.request_id)
            raise
        else:
            self.journal.event("llm_stream_closed", request_id=self.request_id)
            return result


class RecordedClient:
    def __init__(self, client, journal: TraceJournal, agent):
        self.client, self.journal, self.agent = client, journal, agent

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _request(self, request):
        request_id = uuid4().hex[:12]
        self.journal.snapshot(self.agent)
        # Logical model input only. Client credentials/HTTP headers are not recorded.
        self.journal.event("llm_request", request_id=request_id, request=request,
                           provider=self.agent.provider, protocol=self.agent.protocol)
        return request_id

    def create(self, **request):
        request_id = self._request(request)
        try:
            return RecordedStream(self.client.create(**request), self.journal, request_id)
        except BaseException as error:
            self.journal.error("llm_request_error", error, request_id=request_id)
            raise

    def close(self):
        self.journal.event("client_closing")
        try:
            result = self.client.close()
        except BaseException as error:
            self.journal.error("client_close_error", error)
            raise
        else:
            self.journal.event("client_closed")
            return result


class RecordedAsyncClient(RecordedClient):
    async def create(self, **request):
        request_id = self._request(request)
        try:
            return RecordedAsyncStream(await self.client.create(**request), self.journal, request_id)
        except BaseException as error:
            self.journal.error("llm_request_error", error, request_id=request_id)
            raise

    async def close(self):
        self.journal.event("client_closing")
        try:
            result = await self.client.close()
        except BaseException as error:
            self.journal.error("client_close_error", error)
            raise
        else:
            self.journal.event("client_closed")
            return result
