# Autonomous Challenge Solver

`ctf-solver/` is a general-purpose autonomous challenge solver copied from the
legacy `webhack/` harness.
The legacy directory is intentionally unchanged. This variant keeps the same
durable task tree, executor lifecycle, review loop, and CLI commands, but its
prompts and runtime defaults target fully authorized, time-limited challenge
events.

From the contest workspace:

```bash
cd ~/work/ctf/2026xhlj
python /home/pika/work/Enkiball/ctf-solver/main.py --cwd "$PWD"
# equivalent when running from the Enkiball checkout:
python -m ctf-solver --cwd "$PWD"
```

Use `--config /path/to/enki.json` when the provider configuration is not at
Enkiball's normal global location.

The default shell network is `host`, because executors may need to reach event
services and challenge endpoints. The supervisor decides whether the event is
controlled by an API, a browser UI, local files, or a combination.
`--sandbox-network none` can be used for offline attachment work.
`--no-sandbox` is available only when the challenge tooling requires host
execution.

In the first ordinary chat turn, tell the agent to inspect and start the event.
The supervisor discovers rules, challenges, resources, targets, and the
verification workflow using whatever interfaces are available. It then creates
actionable tasks, prepares their resources, and starts the loop automatically.
Every executor gets an isolated Enkiball session directory under the
workspace's `executor-sessions/` folder. The full transcript, including tool
calls and results, is persisted there.

Useful commands remain:

```text
/status
/task list
/exec list
/exec cap <exec_id>
/btw <prompt...>
/tick
/loop start|stop
/state show
```

Durable state is stored under `~/.config/enkiball/ctf-solver/sessions/` and is
separate from the legacy WebHack state.

`/btw` is provided by the Enkiball CLI base. It sees the current conversation
and durable challenge context, uses the full configured tool set, and does not append
its question or answer to the foreground conversation history.
