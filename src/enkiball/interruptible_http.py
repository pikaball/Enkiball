"""Cancellable HTTP waits for both synchronous and asynchronous callers."""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, TypeVar

import httpx

from .sandbox import TurnInterrupted

T = TypeVar("T")


class InterruptibleAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, cancelled: threading.Event) -> None:
        self.cancelled = cancelled
        self.client = httpx.AsyncClient()

    async def wait(self, operation: Awaitable[T]) -> T:
        task = asyncio.ensure_future(operation)
        try:
            while not task.done():
                if self.cancelled.is_set():
                    raise TurnInterrupted
                await asyncio.wait({task}, timeout=0.05)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.wait(self.client.send(request, stream=True))
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_AsyncBody(response, self),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.client.aclose()


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response, transport: InterruptibleAsyncTransport):
        self.response = response
        self.transport = transport

    async def __aiter__(self):
        iterator = self.response.aiter_raw().__aiter__()
        while True:
            try:
                yield await self.transport.wait(iterator.__anext__())
            except StopAsyncIteration:
                return

    async def aclose(self) -> None:
        await self.response.aclose()


class InterruptibleTransport(httpx.BaseTransport):
    """Run only HTTP I/O on an event loop; callbacks and tools stay on the caller."""

    def __init__(self, cancelled: threading.Event) -> None:
        self.transport = InterruptibleAsyncTransport(cancelled)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="enkiball-http", daemon=True
        )
        self.thread.start()
        self.closed = False

    def run(self, operation: Awaitable[T]) -> T:
        async def invoke() -> T:
            return await operation

        future = asyncio.run_coroutine_threadsafe(invoke(), self.loop)
        try:
            return future.result()
        except BaseException:
            future.cancel()
            raise

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # The SDK uses buffered JSON bodies. Rebuild the stream for async I/O.
        async_request = httpx.Request(
            request.method, request.url, headers=request.headers,
            content=request.read(), extensions=request.extensions,
        )
        response = self.run(self.transport.handle_async_request(async_request))
        return httpx.Response(
            response.status_code, headers=response.headers,
            stream=_SyncBody(response, self), extensions=response.extensions,
        )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True

        async def shutdown() -> None:
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.transport.aclose()
            await self.loop.shutdown_asyncgens()

        try:
            self.run(shutdown())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            self.loop.close()


class _SyncBody(httpx.SyncByteStream):
    def __init__(self, response: httpx.Response, transport: InterruptibleTransport):
        self.response = response
        self.transport = transport

    def __iter__(self):
        iterator = self.response.stream.__aiter__()
        while True:
            try:
                yield self.transport.run(iterator.__anext__())
            except StopAsyncIteration:
                return

    def close(self) -> None:
        self.transport.run(self.response.aclose())
