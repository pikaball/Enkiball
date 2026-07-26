from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CONFIG_BASE = Path.home() / ".config" / "enkiball"
SESSION_DIR = _CONFIG_BASE / "sessions"
EXPORT_DIR = _CONFIG_BASE / "exports"


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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
        )


class SessionStore:
    def __init__(self) -> None:
        self._init_paths(SESSION_DIR, EXPORT_DIR)

    @classmethod
    def _from_paths(cls, base_dir: Path, export_dir: Path) -> SessionStore:
        store = cls.__new__(cls)
        store._init_paths(base_dir, export_dir)
        return store

    def _init_paths(self, base_dir: Path, export_dir: Path) -> None:
        self.base_dir = base_dir
        self.export_dir = export_dir
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
        base_id = f"{ts}-{_safe_name(display_name)}"
        session_id = base_id
        suffix = 1
        while self._path(session_id).exists():
            suffix += 1
            session_id = f"{base_id}-{suffix}"
        return session_id

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
        )
        self.current_session_id = record.session_id
        self.current_session_name = record.name
        self.current_session_persisted = False
        if persisted:
            self.save(record)
            self.current_session_persisted = True
        return record

    def _path(self, session_id: str) -> Path:
        return self.base_dir / f"{session_id}.json"

    def list_sessions(self) -> list[SessionRecord]:
        out: list[SessionRecord] = []
        for fp in sorted(self.base_dir.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                out.append(SessionRecord.from_dict(data))
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
        return SessionRecord.from_dict(data)

    def save(self, record: SessionRecord) -> None:
        record.updated_at = _now_iso()
        fp = self._path(record.session_id)
        fp.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

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
