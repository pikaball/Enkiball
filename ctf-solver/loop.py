from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from enkiball import Enkiball

from .prompts import ACTIVE_AUDIT_PROMPT, REVIEW_PROMPT, STATE_MAINTENANCE_PROMPT
from .workspace import TaskStatus


class SupervisorLoopMixin:
    """Stateless background review, audit, and state-maintenance jobs."""

    def _new_loop_agent(self, prompt: str) -> Enkiball:
        session_dir = self.workspace.root / "loop-sessions" / uuid4().hex
        agent_kwargs = dict(self._agent_kwargs)
        agent_kwargs["session_dir"] = session_dir
        agent = Enkiball(system_prompt=prompt, **agent_kwargs)
        agent.auto_compact_enabled = True
        return agent

    @staticmethod
    def _save_loop_session(self, agent: Enkiball) -> None:
        try:
            record = agent.save_session()
            self.workspace.log_event(
                "loop.session_saved",
                {"session_id": record.session_id if record else ""},
            )
        except Exception as exc:
            self.workspace.log_event("loop.session_save_error", {"error": str(exc)})

    def _review_finished_tasks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for task in self.workspace.list_tasks():
            if task.status != TaskStatus.REVIEW:
                continue
            agent = self._new_loop_agent(REVIEW_PROMPT)
            try:
                attempt = task.attempts[-1] if task.attempts else {}
                prompt = (
                    f"<task>\n{json.dumps(task.to_dict(), ensure_ascii=False)}\n</task>\n\n"
                    f"<executor_output>\n{attempt.get('content', task.result)}\n</executor_output>"
                )
                res = agent.chat(
                    prompt,
                    system_context=self.workspace.floating_context(max_chars=60000),
                ).content
                decision = self._parse_json(res)
                applied = self._apply_review_decision(task.id, decision)
                out.append(
                    {
                        "task_id": task.id,
                        "decision": decision.get("decision"),
                        "applied": applied,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.workspace.log_event("review.error", {"task_id": task.id, "error": str(exc)})
                out.append({"task_id": task.id, "error": str(exc)})
            finally:
                self._save_loop_session(agent)
                agent.shutdown()
        return out

    def _apply_review_decision(self, task_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        kind = str(decision.get("decision", "")).lower()
        result = str(decision.get("result", decision.get("rationale", "")))
        facts = decision.get("state_facts", [])
        if isinstance(facts, list):
            for fact in facts:
                if isinstance(fact, dict):
                    self.workspace.add_state_fact(fact)
        if kind == "done":
            self.workspace.update_task(task_id, status=TaskStatus.DONE, result=result)
            return {"status": "done"}
        if kind == "failed":
            self.workspace.update_task(task_id, status=TaskStatus.FAILED, result=result)
            return {"status": "failed"}
        if kind == "blocked":
            self.workspace.update_task(
                task_id,
                status=TaskStatus.BLOCKED,
                blocked_reason=str(decision.get("blocked_reason", result)),
                result=result,
            )
            return {"status": "blocked"}
        if kind == "decompose":
            children = decision.get("children", [])
            created: list[str] = []
            if isinstance(children, list):
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    new_task = self.workspace.create_task(
                        str(child.get("title", "subtask")),
                        str(child.get("goal", "")),
                        parent_id=task_id,
                        context=str(child.get("context", "")),
                        completion_criteria=str(child.get("completion_criteria", "")),
                    )
                    created.append(new_task.id)
            self.workspace.update_task(task_id, status=TaskStatus.DECOMPOSED, result=result)
            return {"status": "decomposed", "children": created}
        if kind == "retry":
            task = self.workspace.get_task(task_id)
            extra = str(decision.get("retry_context", ""))
            context = ((task.context + "\n\n") if task and task.context else "") + extra
            self.workspace.update_task(
                task_id, status=TaskStatus.TODO, context=context, result=result
            )
            return {"status": "todo"}
        self.workspace.update_task(
            task_id,
            status=TaskStatus.BLOCKED,
            blocked_reason="review decision was invalid",
            result=result,
        )
        return {"status": "blocked", "reason": "invalid decision"}

    def _audit_running_tasks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for task in self.workspace.list_tasks():
            if task.status != TaskStatus.RUNNING or not task.executor_id:
                continue
            cap = self.executors.capture(task.executor_id)
            agent = self._new_loop_agent(ACTIVE_AUDIT_PROMPT)
            try:
                prompt = (
                    f"<task>\n{json.dumps(task.to_dict(), ensure_ascii=False)}\n</task>\n\n"
                    f"<executor_capture>\n{json.dumps(cap, ensure_ascii=False)}\n"
                    "</executor_capture>"
                )
                res = agent.chat(
                    prompt,
                    system_context=self.workspace.floating_context(max_chars=60000),
                ).content
                decision = self._parse_json(res)
                action = self._apply_active_audit(task.id, task.executor_id, decision)
                out.append(
                    {
                        "task_id": task.id,
                        "decision": decision.get("decision"),
                        "action": action,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.workspace.log_event(
                    "active_audit.error", {"task_id": task.id, "error": str(exc)}
                )
                out.append({"task_id": task.id, "error": str(exc)})
            finally:
                self._save_loop_session(agent)
                agent.shutdown()
        return out

    def _apply_active_audit(
        self, task_id: str, executor_id: str, decision: dict[str, Any]
    ) -> dict[str, Any]:
        kind = str(decision.get("decision", "continue")).lower()
        reason = str(decision.get("reason", ""))
        if kind == "continue":
            return {"status": "running"}
        self.executors.interrupt(executor_id, reason=reason or kind)
        if kind == "restart":
            task = self.workspace.get_task(task_id)
            extra = str(decision.get("replacement_context", ""))
            context = ((task.context + "\n\n") if task and task.context else "") + extra
            self.workspace.update_task(
                task_id, status=TaskStatus.TODO, context=context, executor_id=""
            )
            return {"status": "requeued"}
        if kind == "blocked":
            self.workspace.update_task(
                task_id,
                status=TaskStatus.BLOCKED,
                blocked_reason=reason,
                executor_id="",
            )
            return {"status": "blocked"}
        if kind == "failed":
            self.workspace.update_task(
                task_id, status=TaskStatus.FAILED, result=reason, executor_id=""
            )
            return {"status": "failed"}
        self.workspace.update_task(
            task_id, status=TaskStatus.REVIEW, blocked_reason=reason, executor_id=""
        )
        return {"status": "review"}

    def _maintain_state_graph(self) -> dict[str, Any]:
        agent = self._new_loop_agent(STATE_MAINTENANCE_PROMPT)
        try:
            res = agent.chat(
                "Normalize the current state graph. Return the complete normalized graph JSON.",
                system_context=self.workspace.floating_context(max_chars=60000),
            ).content
            graph = self._parse_json(res)
            if "facts" in graph:
                self.workspace.write_state_graph(graph)
                return {"ok": True, "facts": len(graph.get("facts", []))}
            return {"ok": False, "error": "missing facts"}
        except Exception as exc:  # noqa: BLE001
            self.workspace.log_event("state_maintenance.error", {"error": str(exc)})
            return {"ok": False, "error": str(exc)}
        finally:
            self._save_loop_session(agent)
            agent.shutdown()

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Models occasionally wrap an otherwise valid response in one
            # sentence. Decode the first JSON object without accepting a
            # partial or malformed decision.
            start = text.find("{")
            if start < 0:
                raise
            data, end = json.JSONDecoder().raw_decode(text[start:])
            if text[start + end :].strip():
                raise
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
        return data
