# WebHack Architecture

WebHack is a specialized agent layer on top of Enkiball. It implements the orchestration foundation only: durable task/state storage, foreground supervisor conversation, a stateless background loop, executor lifecycle, capture, interrupt, and CLI extension. Browser control, request replay, approval policies, resource leasing, and vulnerability-specific scanners are intentionally outside this layer for now.

## Design Principles

- Durable state beats chat history. The task tree, state graph, and human notes are external sources of truth injected into turns.
- The foreground supervisor talks with the human, captures scope/constraints, creates the root task through normal task tools, and starts the loop when ready.
- The background loop is the real operational supervisor. Each loop job uses a fresh model session with complete durable context, not accumulated chat history.
- Executors are disposable leaf-task workers. They can read durable context but cannot write tasks, write state, or control other executors.
- Only leaf tasks may bind executors. Parent tasks are containers for decomposition and roll-up.
- Executor completion means `review`, not `done`. The loop decides whether a task is done, failed, blocked, retried, or decomposed.
- The state graph is not a todo list. Tasks track work; the state graph tracks stable target/application/business facts, constraints, decisions, findings, and open questions.
- Keep the foundation small and replaceable. File-backed JSON/Markdown can later become a server without changing the conceptual model.

## Files

- `workspace.py` - durable task tree, JSON state graph, human notes, event log, floating context construction
- `executor.py` - executor lifecycle, read-only executor context, background execution, capture, interrupt, task assignment
- `supervisor.py` - foreground supervisor tools, stateless watchdog tick, CLI command handlers
- `prompts.py` - foreground supervisor, executor, review, active-audit, and state-maintenance prompts
- `main.py` - runnable CLI entry for WebHack

## Usage

```bash
python -m webhack.main
```

Inside CLI:

```text
/task list
/task start task-xxxx
/exec list
/exec cap exec-1
/exec stop exec-1 drifting
/state show
/state append "Human confirmed test account user@example.com is in scope."
/btw "What is the current high-level progress?"
/debug supervisor
/debug executor("exec-1")
/debug executor_agent("exec-1").capture()
/tick
/loop start
/loop stop
```

There is no `/brief` command and no `/task new` command. Start by talking normally with the foreground supervisor. It should use tools such as `append_human_note`, `append_state_change`, `create_task`, and `start_loop` when scope is clear enough.

`/debug` evaluates Python inside the WebHack CLI process. It is intentionally unsafe and intended only for local development. Available locals include `supervisor`, `workspace`, `executors`, `agent`, `executor(id)`, and `executor_agent(id)`.

## Python Embedding

```python
from webhack import WebHackSupervisor
from enkiball import EnkiballCLI

wh = WebHackSupervisor()
cli = EnkiballCLI(
    wh.agent,
    input_transform=wh.inject_user_prompt,
)
wh.install_cli_commands(cli)
cli.run()
```

## Task Lifecycle

Task statuses:

- `todo` - leaf task ready to run.
- `running` - leaf task bound to an active executor.
- `review` - executor finished; loop must audit the result.
- `decomposed` - task was not directly solved and has child tasks.
- `blocked` - needs human input or external condition.
- `done` - loop judged task complete.
- `failed` - loop judged task cannot proceed or failed.
- `cancelled` - no longer relevant.

Executors append attempts to the bound task. The loop reviews the latest attempt and applies a structured decision.

## Loop Tick

Each tick performs these phases:

1. Collect finished executors and move their bound tasks to `review`.
2. Review every `review` task with a fresh review agent and complete durable context.
3. Audit every active `running` leaf with a fresh active-audit agent and current capture.
4. Normalize the JSON state graph with a fresh state-maintenance agent.
5. Start ready `todo` leaf tasks up to executor capacity.

Loop agents are disposable per job. They do not share history across ticks.

## Durable Files

Default root: `~/.config/enkiball/webhack/sessions/<session_id>/`

Each Enkiball CLI session gets an isolated WebHack workspace. Creating or switching sessions with `/session new`, `/session use`, or `/session fork` switches WebHack durable files too. Running executors from the previous workspace are interrupted during the switch because their context belongs to the previous session.

- `tasks.json`
- `state.json`
- `human_notes.md`
- `events.jsonl`

## What Is Not Implemented Yet

- Browser automation / Playwright control
- HTTP capture/replay
- Human approval matching rules and allowlists
- Resource/account/browser-session leasing
- Vulnerability-specific scanners

These should be added as tools and commands on top of this foundation, not mixed into the orchestration core.
