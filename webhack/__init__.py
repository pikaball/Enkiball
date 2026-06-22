"""WebHack: specialized orchestration layer built on Enkiball.

This package intentionally keeps web-specific capabilities (browser control,
HTTP replay, matching rules, auth workflows) out of the base architecture.
The foundation here is task/state/executor orchestration that should remain
useful even as base model capability improves.
"""

from .executor import ExecutorController, ExecutorRecord
from .supervisor import WebHackSupervisor
from .workspace import Task, TaskStatus, WebHackWorkspace

__all__ = [
    "ExecutorController",
    "ExecutorRecord",
    "Task",
    "TaskStatus",
    "WebHackSupervisor",
    "WebHackWorkspace",
]
