from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .persistence import atomic_write_json

_CONFIG_BASE = Path.home() / ".config" / "enkiball"
SESSION_DIR = _CONFIG_BASE / "sessions"
EXPORT_DIR = _CONFIG_BASE / "exports"
_SESSION_ID_PATTERN = re.compile(r"^[\w-]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_name(name: str) -> str:
    cleaned = "".join(ch for ch in name.strip() if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "session"


@dataclass
class SessionRecord:
    session_id: str
    name: str
    created_at: str
    updated_at: str
    messages: list[dict[str, Any]]
    cwd: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cwd": self.cwd,
            "messages": self.messages,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SessionRecord:
        return SessionRecord(
            session_id=str(data["session_id"]),
            name=str(data.get("name", data["session_id"])),
            created_at=str(data.get("created_at", _now_iso())),
            updated_at=str(data.get("updated_at", _now_iso())),
            messages=list(data.get("messages", [])),
            cwd=str(data["cwd"]) if data.get("cwd") is not None else None,
        )


class SessionStore:
    def __init__(self, cwd: str | Path | None = None) -> None:
        self._init_paths(SESSION_DIR, EXPORT_DIR, cwd)

    @classmethod
    def _from_paths(
        cls, base_dir: Path, export_dir: Path, cwd: str | Path | None = None
    ) -> SessionStore:
        store = cls.__new__(cls)
        store._init_paths(base_dir, export_dir, cwd)
        return store

    def _init_paths(
        self, base_dir: Path, export_dir: Path, cwd: str | Path | None
    ) -> None:
        self.base_dir = base_dir
        self.export_dir = export_dir
        self.cwd = Path.cwd().resolve() if cwd is None else Path(cwd).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.current_session_id: str | None = None
        self.current_session_name: str | None = None
        self.current_session_persisted: bool = False

    @staticmethod
    def has_user_turn(messages: list[dict[str, Any]]) -> bool:
        for msg in messages:
            if msg.get("role") == "user":
                return True
        return False

    def _generate_session_id(self, display_name: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{ts}-{_safe_name(display_name)}-{uuid4().hex[:8]}"

    def begin_new(
        self,
        name: str | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
        persisted: bool = False,
    ) -> SessionRecord:
        display_name = name.strip() if name else "untitled"
        record = SessionRecord(
            session_id=self._generate_session_id(display_name),
            name=display_name,
            created_at=_now_iso(),
            updated_at=_now_iso(),
            messages=initial_messages or [],
            cwd=str(self.cwd),
        )
        self.current_session_id = record.session_id
        self.current_session_name = record.name
        self.current_session_persisted = False
        if persisted:
            self.save(record)
            self.current_session_persisted = True
        return record

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(
            session_id
        ):
            raise ValueError(f"invalid session id: {session_id!r}")
        base_dir = self.base_dir.resolve()
        path = (base_dir / f"{session_id}.json").resolve()
        if path.parent != base_dir:
            raise ValueError(f"session path escapes session directory: {session_id!r}")
        return path

    def list_sessions(self) -> list[SessionRecord]:
        out: list[SessionRecord] = []
        for fp in sorted(self.base_dir.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                record = SessionRecord.from_dict(data)
                if self._belongs_to_cwd_scope(record):
                    out.append(record)
            except Exception:
                continue
        out.sort(key=lambda s: s.updated_at, reverse=True)
        return out

    def create(
        self,
        name: str | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> SessionRecord:
        display_name = (
            name.strip()
            if name
            else f"session-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        )
        return self.begin_new(
            name=display_name, initial_messages=initial_messages, persisted=True
        )

    def load(self, session_id: str) -> SessionRecord:
        fp = self._path(session_id)
        if not fp.exists():
            raise FileNotFoundError(f"session not found: {session_id}")
        data = json.loads(fp.read_text(encoding="utf-8"))
        record = SessionRecord.from_dict(data)
        if not self._belongs_to_cwd_scope(record):
            raise PermissionError(
                f"session does not belong to cwd or an ancestor: {session_id}"
            )
        return record

    def save(self, record: SessionRecord) -> None:
        record.updated_at = _now_iso()
        if record.cwd is None:
            record.cwd = str(self.cwd)
        fp = self._path(record.session_id)
        atomic_write_json(fp, record.to_dict())

    def rename_current(self, name: str) -> bool:
        cleaned = name.strip()
        if not cleaned:
            return False
        sid = self.current_session_id
        if not sid:
            return False
        self.current_session_name = cleaned
        if not self.current_session_persisted:
            return True

        fp = self._path(sid)
        if not fp.exists():
            return True
        record = self.load(sid)
        record.name = cleaned
        self.save(record)
        return True

    def save_messages(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> SessionRecord:
        if self._path(session_id).exists():
            record = self.load(session_id)
        else:
            record = SessionRecord(
                session_id=session_id,
                name=self.current_session_name or session_id,
                created_at=_now_iso(),
                updated_at=_now_iso(),
                messages=[],
                cwd=str(self.cwd),
            )
        record.messages = messages
        self.save(record)
        if self.current_session_id == session_id:
            self.current_session_persisted = True
            self.current_session_name = record.name
        return record

    def save_current_messages(
        self, messages: list[dict[str, Any]], force: bool = False
    ) -> SessionRecord | None:
        sid = self.current_session_id
        if not sid:
            return None
        if (
            not force
            and not self.current_session_persisted
            and not self.has_user_turn(messages)
        ):
            return None
        return self.save_messages(sid, messages)

    def switch_to(self, record: SessionRecord) -> None:
        self.current_session_id = record.session_id
        self.current_session_name = record.name
        self.current_session_persisted = True

    def ensure_default(self, initial_messages: list[dict[str, Any]]) -> SessionRecord:
        sessions = self.list_sessions()
        if sessions:
            self.switch_to(sessions[0])
            return sessions[0]
        return self.create(name="default", initial_messages=initial_messages)

    def resolve(self, raw: str) -> str | None:
        sessions = self.list_sessions()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx].session_id
            return None
        for s in sessions:
            if s.session_id == raw:
                return raw
        return None

    def _belongs_to_cwd_scope(self, record: SessionRecord) -> bool:
        if record.cwd is None:
            return False
        try:
            session_cwd = Path(record.cwd).expanduser().resolve()
            return self.cwd == session_cwd or self.cwd.is_relative_to(session_cwd)
        except (OSError, RuntimeError):
            return False

    def export_session(self, session_id: str, out_path: str | None = None) -> Path:
        record = self.load(session_id)
        target = Path(out_path) if out_path else self.export_dir / f"{session_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# Session {record.session_id}",
            "",
            f"- Name: {record.name}",
            f"- Created: {record.created_at}",
            f"- Updated: {record.updated_at}",
            f"- CWD: {record.cwd}",
            "",
            "## Messages",
            "",
        ]
        for idx, msg in enumerate(record.messages, start=1):
            role = str(msg.get("role", "unknown"))
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            lines.extend(
                [
                    f"### {idx}. {role}",
                    "",
                    "```text",
                    content,
                    "```",
                    "",
                ]
            )
        target.write_text("\n".join(lines), encoding="utf-8")
        return target
