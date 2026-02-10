from __future__ import annotations

import json
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque
from queue import Empty, Queue
from typing import Any
from urllib.parse import urljoin

import httpx


class MCPClient(ABC):
    def __init__(self, name: str) -> None:
        self.name = name
        self._request_id = 0
        self._pending_requests: dict[int | str, Queue[dict[str, Any]]] = {}
        self.tools: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self.running = False

    def _get_next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _handle_message(self, msg: dict[str, Any]) -> None:
        if "id" not in msg:
            return
        rid = msg["id"]
        candidates: list[int | str] = [rid]
        if isinstance(rid, int):
            candidates.append(str(rid))
        elif isinstance(rid, str) and rid.isdigit():
            candidates.append(int(rid))

        for key in candidates:
            q = self._pending_requests.get(key)
            if q is None:
                continue
            q.put(msg)
            return

    @abstractmethod
    def _send_payload(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def initialize(self) -> None:
        self.send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                "clientInfo": {"name": "enkiball", "version": "0.1.0"},
            },
        )
        self.send_notification("notifications/initialized")

        try:
            tools_res = self.send_request("tools/list")
            if tools_res and "tools" in tools_res:
                self.tools = tools_res["tools"]
        except Exception:
            pass

        try:
            resources_res = self.send_request("resources/list")
            if resources_res and "resources" in resources_res:
                self.resources = resources_res["resources"]
        except Exception:
            pass

        try:
            prompts_res = self.send_request("prompts/list")
            if prompts_res and "prompts" in prompts_res:
                self.prompts = prompts_res["prompts"]
        except Exception:
            pass

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        rid = self._get_next_id()
        rid_str = str(rid)
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        }
        q: Queue[dict[str, Any]] = Queue()
        self._pending_requests[rid] = q
        self._pending_requests[rid_str] = q
        try:
            self._send_payload(payload)
            resp = q.get(timeout=30)
        except Empty as e:
            self._pending_requests.pop(rid, None)
            self._pending_requests.pop(rid_str, None)
            raise TimeoutError(f"MCP request timed out: {method}") from e
        except Exception:
            self._pending_requests.pop(rid, None)
            self._pending_requests.pop(rid_str, None)
            raise

        self._pending_requests.pop(rid, None)
        self._pending_requests.pop(rid_str, None)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"MCP error: {err.get('message', 'Unknown error')}")
        return resp.get("result")

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        self._send_payload(payload)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.send_request("tools/call", {"name": name, "arguments": arguments})


class StdioMCPClient(MCPClient):
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        transport: str = "jsonl",
    ) -> None:
        super().__init__(name)
        self.command = command
        self.args = args
        self.env = env or {}
        self.transport = transport
        self.process: subprocess.Popen[bytes] | None = None
        self.thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=20)

    def start(self) -> None:
        run_env = os.environ.copy()
        run_env.update(self.env)

        self.process = subprocess.Popen(
            [self.command, *self.args],  # noqa: S603,S607
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            env=run_env,
        )
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        self.stderr_thread = threading.Thread(target=self._drain_stderr_loop, daemon=True)
        self.stderr_thread.start()

        try:
            self.initialize()
        except Exception as e:
            detail = self.get_stderr_tail()
            self.close()
            if detail:
                raise RuntimeError(f"{e}; stderr: {detail}") from e
            raise

    def _read_message(self) -> dict[str, Any] | None:
        if not self.process or not self.process.stdout:
            return None

        while True:
            line = self.process.stdout.readline()
            if not line:
                return None
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            # JSONL transport: one JSON object per line.
            if text.startswith("{"):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue

            # Content-Length framing.
            if ":" not in text:
                continue
            key, value = text.split(":", 1)
            if key.strip().lower() != "content-length":
                continue
            try:
                length = int(value.strip())
            except ValueError:
                continue
            if length <= 0:
                continue

            # Read remaining headers until blank line.
            while True:
                header_line = self.process.stdout.readline()
                if not header_line:
                    return None
                if header_line in (b"\r\n", b"\n"):
                    break

            body = self.process.stdout.read(length)
            if not body or len(body) < length:
                return None
            try:
                return json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                continue

    def _read_loop(self) -> None:
        while self.running:
            try:
                msg = self._read_message()
            except Exception:
                break
            if msg is None:
                break
            self._handle_message(msg)
        self.running = False

    def _drain_stderr_loop(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            while self.running:
                line = self.process.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    self._stderr_tail.append(decoded)
        except Exception:
            return

    def get_stderr_tail(self) -> str:
        return " | ".join(self._stderr_tail)

    def _send_payload(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("MCP process is not running")
        with self._write_lock:
            if self.transport == "content-length":
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                self.process.stdin.write(header)
                self.process.stdin.write(body)
            else:
                line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                self.process.stdin.write(line)
            self.process.stdin.flush()

    def close(self) -> None:
        self.running = False
        if not self.process:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()


class SSEMCPClient(MCPClient):
    def __init__(self, name: str, url: str) -> None:
        super().__init__(name)
        self.url = url
        self.post_url: str | None = None
        self.client = httpx.Client(timeout=60.0)
        self.thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._endpoint_ready = threading.Event()
        self._connection_error: Exception | None = None

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

        if not self._connected.wait(timeout=10):
            self.close()
            raise TimeoutError("SSE connection timeout")
        if self._connection_error:
            self.close()
            raise self._connection_error

        # Prefer server-provided endpoint when available.
        if not self.post_url:
            self._endpoint_ready.wait(timeout=1.5)
        if not self.post_url:
            self.post_url = self.url

        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    def _read_loop(self) -> None:
        buffer: list[str] = []
        try:
            with self.client.stream(
                "GET",
                self.url,
                headers={
                    "Accept": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            ) as response:
                response.raise_for_status()
                if not self.post_url:
                    self.post_url = self.url
                self._connected.set()

                for line in response.iter_lines():
                    if not self.running:
                        break
                    if line == "":
                        if buffer:
                            self._process_sse_event(buffer)
                            buffer = []
                        continue
                    buffer.append(line)
        except Exception as e:  # noqa: BLE001
            self._connection_error = e
            self._connected.set()
        finally:
            self.running = False

    def _process_sse_event(self, lines: list[str]) -> None:
        event_type = "message"
        data_parts: list[str] = []
        for line in lines:
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_parts.append(line.split(":", 1)[1].lstrip())

        if not data_parts:
            return
        data_str = "\n".join(data_parts)

        if event_type == "endpoint":
            if "://" in data_str:
                self.post_url = data_str
            else:
                self.post_url = urljoin(self.url, data_str)
            self._endpoint_ready.set()
            return

        if event_type != "message":
            return
        try:
            msg = json.loads(data_str)
        except json.JSONDecodeError:
            return
        self._handle_message(msg)

    def _send_payload(self, payload: dict[str, Any]) -> None:
        if not self.post_url:
            raise RuntimeError("MCP post endpoint unavailable")
        resp = self.client.post(self.post_url, json=payload)
        try:
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            body = resp.text[:500] if resp.text else ""
            raise RuntimeError(f"POST {self.post_url} failed: {resp.status_code} {body}") from e

    def close(self) -> None:
        self.running = False
        try:
            self.client.close()
        except Exception:
            pass


class StreamableHTTPMCPClient(MCPClient):
    def __init__(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name)
        self.url = url
        self.session_id: str | None = None
        self.client = httpx.Client(timeout=60.0, headers=headers or {})

    @staticmethod
    def _parse_sse_message_lines(lines: list[str]) -> tuple[str, str] | None:
        if not lines:
            return None
        event_type = "message"
        data_parts: list[str] = []
        for line in lines:
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_parts.append(line.split(":", 1)[1].lstrip())
        if not data_parts:
            return None
        return event_type, "\n".join(data_parts)

    def _send_payload(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError("StreamableHTTPMCPClient uses HTTP request-response directly")

    def _request_once(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        expected_id = payload.get("id")
        req_headers = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            req_headers["Mcp-Session-Id"] = self.session_id
        with self.client.stream(
            "POST",
            self.url,
            json=payload,
            headers=req_headers,
        ) as resp:
            # Capture/refresh session id when server provides it.
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid

            try:
                resp.raise_for_status()
            except Exception as e:  # noqa: BLE001
                body = resp.read().decode("utf-8", errors="replace")
                if len(body) > 800:
                    body = body[:800] + "...[truncated]"
                raise RuntimeError(
                    f"POST {self.url} failed: {resp.status_code}; "
                    f"session_id={self.session_id!r}; body={body}"
                ) from e
            content_type = resp.headers.get("content-type", "").lower()

            if "text/event-stream" not in content_type:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))

            block: list[str] = []
            for line in resp.iter_lines():
                if line == "":
                    parsed = self._parse_sse_message_lines(block)
                    block = []
                    if not parsed:
                        continue
                    event_type, data = parsed
                    if event_type != "message":
                        continue
                    try:
                        msg = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if expected_id is None or msg.get("id") == expected_id:
                        return msg
                else:
                    block.append(line)

            if block:
                parsed = self._parse_sse_message_lines(block)
                if parsed and parsed[0] == "message":
                    try:
                        msg = json.loads(parsed[1])
                    except json.JSONDecodeError:
                        msg = None
                    if msg and (expected_id is None or msg.get("id") == expected_id):
                        return msg

        return None

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        rid = self._get_next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        }
        msg = self._request_once(payload)
        if not msg:
            raise RuntimeError(f"Empty response for MCP request: {method}")
        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(f"MCP error: {err.get('message', 'Unknown error')}")
        if msg.get("id") != rid:
            raise RuntimeError(
                f"MCP response id mismatch for {method}: expected {rid}, got {msg.get('id')}"
            )
        return msg.get("result")

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        # Notification response is optional; ignore body.
        self._request_once(payload)

    def start(self) -> None:
        self.running = True
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.running = False
        try:
            self.client.close()
        except Exception:
            pass
