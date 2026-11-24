"""The orchestration contract, independent of a particular workflow shape."""

ORCHESTRATION_PROMPT = """
Use orchestrate to express an agent workflow as an ordinary Python program.
The program is the control plane: you decide dependencies, transformations,
conditions, iteration and concurrency. Agent supplies a stateful model-backed
operation; it does not prescribe a workflow or a fixed team of roles.

Choose the structure from the work. Serial calls carry an earlier result into a
later step; parallel calls handle independent work; conditions choose a path
from observed values; loops refine or repeat work until a concrete condition
holds. These are equally valid and can be combined using normal Python. Keep
simple work in the main conversation when a separate program adds no value.

Each Agent owns its conversation. Reuse one sequentially when continuity helps;
use separate instances when work needs independent context or concurrency.
Pass context and results explicitly through Python variables. A name labels the
terminal view; the prompt defines the agent's work. Request a parseable answer
when the program needs to branch on it, and judge completion using actual
evidence. The parent provider/model is inherited unless explicitly overridden.

Choose wait=true or wait=false when calling orchestrate. With true, this single
tool call waits until the whole program and its managed work finish. With false,
the main conversation continues and respond() updates arrive automatically.
Do not manage waiting by repeated tool calls. Python await controls dependencies
inside the program; it is separate from the tool's wait parameter.

respond(value) deliberately selects what the main conversation receives. It can
report intermediate findings and final results; it does not terminate execution.
The full per-Agent record is saved separately. Read the returned manifest and
agent trace paths when a result needs checking or an execution fails, rather
than repeating the whole investigation or flooding the main context with logs.
Integrate new findings into the current user objective without duplicating work.

The Python environment is fresh for every invocation. Agent, respond and spawn
are injected names: use them directly, do not import them or the tool name.
asyncio is preloaded, and regular Python imports are allowed. Variables and Agent objects disappear
after execution; saved records and artifacts remain available.
"""

ORCHESTRATE_HELP = """Run a one-shot Python program that composes Enkiball agents.
Required: code (Python source), wait (boolean). Optional: title, timeout_sec.
wait=true keeps this tool call open until the entire program finishes; false
returns immediately and respond() messages arrive by notice/background turns.
There is no polling/wait tool. cancel_run(run_id) stops an invocation.

Globals are provided directly; DO NOT import Agent or the tool name:
  Agent(name=..., system_prompt=..., model=...) inherits the parent's config/cwd.
  a.chat(prompt) or await a.achat(prompt) returns ChatResult; use result.content.
  respond(value) sends a selected JSON-serializable finding to the main agent.
  asyncio is available; spawn(coroutine) schedules work joined before exit.
Top-level await is supported; do not call asyncio.run. Use ordinary Python for
serial dependencies, parallel tasks, conditional branches, loops, aggregation
and error handling. Reuse an Agent for successive turns or create new ones.
Use separate Agents concurrently: one Agent runs one operation at a time.
Up to 8 Agent operations run concurrently; excess async work queues. There is
no lifetime limit on creating agents or running sequential iterations.

Agent retains the Enkiball API, including history(), messages, compact/acompact,
streaming and background execution. Children receive your specified task/context,
not the parent conversation. They have configured built-in/MCP tools, but not
the parent's dynamically registered Python tools or this orchestration tool.
Each invocation is destroyed after its script and spawned work finish. Explicitly
await tasks created directly with asyncio.create_task. The default run timeout
is 600s; timeout_sec can be 1..3600. At most 4 invocations run simultaneously.

Every invocation returns run_id, status, trace_dir, manifest_path and
responses_path. Full records are saved incrementally under agents/<id>/:
messages.json is the latest history, events.jsonl contains requests, streamed
output, tool arguments/results and errors. Use existing file/shell tools to read
these paths as needed; they are automatically granted read access. The process
is temporary; its records survive completion, errors, timeout and CLI restarts.
Only explicit respond() content, status and output previews enter the main
conversation. If previews are truncated, the full records remain on disk.
Inside Python, Agent.trace_dir, .messages_path and .events_path locate its records.
The orchestration Python runs as the CLI user, not inside the tool sandbox.
"""
