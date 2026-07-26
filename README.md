# Enkiball

Enkiball 是一个面向安全研究和长上下文任务的 agent 基座，同时提供交互式 CLI 和 Python 库 API。

当前核心能力包括多 provider/model 管理、流式对话、图片输入、工具调用、MCP 接入、会话持久化、历史压缩、实时输出捕获和后台执行。更上层的 Task Controller、SubAgent、WebHack 编排能力仍在演进中。

## 目录

- [安装](#安装)
- [配置](#配置)
- [CLI 使用](#cli-使用)
- [Python API](#python-api)
- [数据结构](#数据结构)
- [自定义工具](#自定义工具)
- [CLI 嵌入和自定义命令](#cli-嵌入和自定义命令)
- [内置 LLM 工具](#内置-llm-工具)
- [项目结构](#项目结构)

## 安装

```bash
pip install -e .
```

安装后 `enkiball` 命令全局可用，可在任意目录下启动。

运行环境要求见 `pyproject.toml`：Python `>=3.10`，主要依赖为 `openai`、`prompt_toolkit`、`rich`、`httpx`。

## 配置

配置文件位于 `~/.config/enkiball/enki.json`，首次运行前需手动创建：

```bash
mkdir -p ~/.config/enkiball
```

```json
{
  "system_prompt": "You are Enkiball, a practical assistant for security research tasks.",
  "auto_compact": {
    "enabled": false,
    "threshold_ratio": 0.7
  },
  "providers": {
    "ollama": {
      "api_key": "123",
      "base_url": "http://127.0.0.1:11434/v1",
      "models": {
        "qwen3.5:9b": {
          "temperature": 1,
          "max_tokens": 2048
        }
      }
    },
    "openai": {
      "api_key": "sk-xxx",
      "base_url": "https://api.openai.com/v1",
      "models": {
        "gpt-4o": {
          "temperature": 0.2,
          "max_tokens": 4096
        }
      }
    }
  },
  "mcp": {
    "servers": {
      "ssh-mcp": {
        "enabled": false,
        "command": "node",
        "args": ["/path/to/ssh-mcp-server/dist/index.js"],
        "transport": "jsonl"
      },
      "remote-mcp": {
        "enabled": false,
        "type": "streamable-http",
        "url": "http://127.0.0.1:12306/mcp"
      }
    }
  }
}
```

字段说明：

- `system_prompt` — 自定义系统提示词，省略则使用内置默认值
- `auto_compact.threshold_ratio` — 自动压缩触发比例，默认 `0.7`；按 `models.<name>.max_tokens * threshold_ratio` 计算 compact 触发点
- `providers` — 多 provider 支持，每个 provider 直接写 `api_key` 和 `base_url`
- `models` — dict 格式，key 为模型名，value 为该模型的参数（`temperature`、`max_tokens` 等）；这里的 `max_tokens` 是模型端点单次请求允许/生成相关参数，不是 compact 阈值
- 未在 config 中定义的模型也可使用（通过 `/v1/models` 端点自动发现），使用默认参数
- 当前选中的 provider 和 model 保存在 `~/.config/enkiball/state.json`（运行时状态，与配置分离）
- `mcp.servers` — MCP 服务器配置，支持 Stdio（jsonl/content-length）、SSE、Streamable HTTP 三种传输

所有持久化数据（会话历史、导出、命令历史）存储在 `~/.config/enkiball/` 下。

### Provider 和 model

`providers` 是一个字典，key 是 provider 名称，value 是该 provider 的连接配置。

```json
{
  "providers": {
    "openai": {
      "api_key": "sk-xxx",
      "base_url": "https://api.openai.com/v1",
      "models": {
        "gpt-4o": {
          "temperature": 0.2,
          "max_tokens": 4096
        }
      }
    }
  }
}
```

- `api_key` 和 `base_url` 会传给 OpenAI-compatible client。
- `models` 中定义的参数会在请求该模型时使用。
- 未定义但远程 `/models` 端点可发现的模型也可以切换使用，此时使用默认请求参数。
- 当前 provider/model 不写回主配置文件，而是写入 `~/.config/enkiball/state.json`。

### MCP 服务器

`mcp.servers` 支持三类传输：

```json
{
  "mcp": {
    "servers": {
      "local-jsonl": {
        "enabled": false,
        "command": "node",
        "args": ["/path/to/server.js"],
        "transport": "jsonl"
      },
      "local-content-length": {
        "enabled": false,
        "command": "python",
        "args": ["/path/to/server.py"],
        "transport": "content-length"
      },
      "remote-http": {
        "enabled": false,
        "type": "streamable-http",
        "url": "http://127.0.0.1:12306/mcp"
      }
    }
  }
}
```

- `enabled=true` 的服务器会在 `Enkiball(auto_start_mcp=True)` 初始化时自动启动。
- Stdio server 使用 `command`、`args`、`env`、`transport`。
- 远程 server 使用 `url`；`type=streamable-http` 时走 Streamable HTTP，否则默认按 SSE 处理。

## CLI 使用

```bash
enkiball
```

内置文件和命令工具默认只能访问启动目录及其子目录。可以指定工作目录和额外授权目录：

```bash
enkiball --cwd /path/to/project --dir-permission /path/to/shared
```

CLI 默认在工具请求访问授权目录之外时询问用户，并在同意后授权该目录到当前进程结束。使用 `--no-approval` 可改为静默拒绝。`--dir-permission` 可以重复传入。

普通输入直接发送给 LLM，支持流式输出。按 `Ctrl+J` 换行，`Enter` 发送。`Esc` 中断当前输出。

### 命令列表

```
/help                      帮助
/exit                      退出
/model [name]              查看或切换模型（无参数时显示可用模型列表，含远程发现）
/provider                  查看当前 provider
/provider list             列出所有 provider 及其模型
/provider switch <name>    切换 provider
/compact                   压缩历史（摘要后续只发摘要给 LLM，完整历史仍保留）
/autocompact [on|off]      查看或切换自动压缩
/revert [n]                撤回最近 n 轮对话，自动恢复输入和图片
/thinking                  开关 thinking/reasoning 输出
/tokens                    查看累计 token 用量
/image add <path...>       挂载图片
/image list                查看已挂载图片
/image remove <index>      移除一张图片
/image clear               清空挂载图片
/session                   进入 session 交互面板
/session list              列出所有会话
/session current            当前会话信息
/session new [name]        新建会话
/session use <id|index>    切换会话（屏幕恢复历史）
/session rename <name>     重命名当前会话
/session export [path]     导出会话为 markdown
/session fork <turn>       从第 N 轮用户输入处 fork 新会话
/mcp                       进入 MCP 交互面板
/mcp list                  列出 MCP 服务器
/mcp enable <name|index>   启用并启动
/mcp disable <name|index>  禁用并停止
/mcp start <name|index>    启动进程
/mcp stop <name|index>     停止进程
/mcp status                查看运行状态
/mcp path                  输出 MCP 配置文件路径
```

## Python API

### 导出对象

包根路径导出以下对象：

```python
from enkiball import (
    ChatResult,
    CommandHandler,
    DEFAULT_COMPACT_PROMPT,
    Enkiball,
    EnkiballCLI,
    LiveSnapshot,
    RetryEvent,
    SessionRecord,
    SessionStore,
    TokenUsage,
)
```

- `Enkiball` — 推荐入口，高层 agent API。
- `EnkiballCLI` — 可嵌入的交互式 CLI runner。
- `CommandHandler` — slash command 处理器，可用于自定义 CLI。
- `ChatResult` — 单轮对话结果。
- `LiveSnapshot` — 当前或最近一次流式输出快照。
- `RetryEvent` — LLM 请求重试事件。
- `TokenUsage` — 累计 token 统计。
- `SessionRecord` / `SessionStore` — 会话持久化数据结构和存储器。
- `DEFAULT_COMPACT_PROMPT` — 默认历史压缩 prompt。

### 快速开始

```python
from enkiball import Enkiball

with Enkiball() as agent:
    result = agent.chat("scan open ports on localhost")
    print(result.content)

    for token in agent.stream("explain the result"):
        print(token, end="", flush=True)
```

配置文件路径可通过构造参数覆盖：

```python
agent = Enkiball(config_path="/path/to/custom/enki.json")
```

### Enkiball 构造参数

```python
agent = Enkiball(
    config_path="~/.config/enkiball/enki.json",
    system_prompt=None,
    auto_start_mcp=True,
    extra_tools=None,
    on_tool_event=None,
    session_dir=None,
    cwd=None,
    dir_permissions=None,
    approval=False,
)
```

- `config_path` — 配置文件路径，默认 `~/.config/enkiball/enki.json`。
- `system_prompt` — 覆盖配置文件中的 system prompt。
- `auto_start_mcp` — 初始化时是否启动配置中 `enabled=true` 的 MCP server。
- `extra_tools` — 直接注入的 OpenAI function tool schema 列表。
- `on_tool_event` — 工具调用事件回调，签名为 `(event: str) -> None`。
- `session_dir` — session JSON 文件存储目录。session 存储位置只允许通过这个构造参数自定义。
- `cwd` — 内置文件和命令工具的工作目录，默认是创建 agent 时的当前目录。
- `dir_permissions` — 内置工具可以额外访问的目录列表；相对路径以 `cwd` 为基准。
- `approval` — 是否允许交互前端处理越界目录审批。API 没有绑定交互前端时仍会静默拒绝；CLI 默认绑定 yes/no 询问。

`Enkiball` 支持 context manager。退出 `with` 块时会调用 `shutdown()` 停止后台 executor 和 MCP server。

### 对话接口

#### `chat()`

同步发送一轮用户输入，阻塞直到完整回复结束。

```python
result = agent.chat(
    "analyze this screenshot",
    images=["screen.png"],
    system_context="Temporary context for this turn.",
)

print(result.content)
print(result.tool_calls)
print(result.thinking)
print(result.interrupted)
```

参数：

- `message` — 用户文本。
- `images` — 可选图片路径列表，会转换为 OpenAI-compatible image input。
- `on_retry` — 可选重试回调，接收 `RetryEvent`。
- `system_context` — 只追加到当前会话历史中的临时 system 消息。
- `should_stop` — 可选停止回调，返回 `True` 时中断流式循环。

返回 `ChatResult`。

#### `stream()`

同步流式发送一轮输入，逐 token yield。生成器结束时的 return value 是 `ChatResult`。

```python
stream = agent.stream("explain the findings")
try:
    while True:
        token = next(stream)
        print(token, end="", flush=True)
except StopIteration as stop:
    result = stop.value
```

如果不需要读取 generator return value，也可以直接：

```python
for token in agent.stream("explain the findings"):
    print(token, end="", flush=True)
```

### 异步接口

异步接口与同步接口共享同一个 `ChatEngine` 和消息历史。

```python
import asyncio
from enkiball import Enkiball


async def main():
    with Enkiball() as agent:
        result = await agent.achat("scan open ports on localhost")
        print(result.content)

        async for token in agent.astream("explain the result"):
            print(token, end="", flush=True)

        async for event in agent.astream_events("analyze this"):
            if event["type"] == "token":
                print(event["data"], end="")
            elif event["type"] == "thinking":
                print(f"\n[thinking] {event['data']}")
            elif event["type"] == "tool":
                print(f"\n[tool] {event['data']}")
            elif event["type"] == "retry":
                print(f"\n[retry] {event['data'].error}")
            elif event["type"] == "done":
                result = event["data"]

        summary = await agent.acompact()


asyncio.run(main())
```

- `achat(...) -> ChatResult` — 异步一次性返回完整结果。
- `astream(...) -> AsyncGenerator[str, None]` — 异步 token stream。
- `astream_events(...) -> AsyncGenerator[dict, None]` — 异步结构化事件流，事件类型包括 `token`、`thinking`、`tool`、`retry`、`done`。
- `acompact(...) -> str` — 异步压缩历史。

### 后台执行和实时 capture

后台执行适合 fire-and-poll 场景：

```python
future = agent.chat_in_background("run a long analysis")

while not future.done():
    snapshot = agent.capture()
    print(snapshot.content[-120:])

result = future.result()
```

- `chat_in_background(...)` — 在线程池中启动 `chat()`，返回 `Future[ChatResult]`。
- `capture()` — 返回 `LiveSnapshot`，可从任意线程读取当前流式输出。
- `wait(timeout=None)` — 等待当前流式任务结束，超时返回 `False`。
- `interrupt()` / `cancel()` — 请求当前流式任务停止。
- `interrupted` — 当前是否已经请求 interrupt。

### Provider 和模型接口

```python
print(agent.provider)
print(agent.providers)
print(agent.model)

agent.switch_provider("openai")
agent.switch_model("gpt-4o")
agent.model = "gpt-4o-mini"

print(agent.list_models())
print(agent.list_models(remote=False))
print(agent.refresh_models(timeout=5.0))
```

- `config` — 当前配置的深拷贝。
- `provider` — 当前 provider 名称。
- `providers` — 所有 provider 配置。
- `model` — 当前模型名，可读写。
- `switch_provider(name, save=True)` — 切换 provider，返回是否成功。
- `switch_model(model, save=True)` — 切换模型。
- `list_models(provider_name=None, remote=True, timeout=5.0)` — 返回 config 模型和远程发现模型的合并列表。
- `refresh_models(timeout=5.0)` — 强制拉取当前 provider 的远程模型列表。

### System prompt、thinking 和 token 统计

```python
print(agent.system_prompt)
agent.system_prompt = "You are a pentesting assistant."

agent.show_thinking = True
print(agent.show_thinking)

print(agent.token_usage.total_tokens)
agent.reset_token_usage()
```

- `system_prompt` — 当前 system prompt，可读写。
- `show_thinking` — 是否在引擎回调中暴露 reasoning/thinking 内容。
- `token_usage` — `TokenUsage` 实例，累计 prompt/completion/total tokens。
- `reset_token_usage()` — 清空累计 token 统计。

### 历史和 compact

```python
summary = agent.compact()
summary = agent.compact(prompt="只保留安全发现")

print(agent.history())
print(agent.messages)
print(agent.last_reply())

agent.revert(2)
forked = agent.fork_at_turn(3)
agent.reset()
agent.load_messages(forked)
```

- `compact(prompt=None, on_token=None, on_retry=None)` — 总结历史并设置 compact 边界。完整历史仍保留，但后续 LLM 请求只发送 compact marker 之后的消息。
- `auto_compact_enabled` — 是否启用自动压缩。
- `auto_compact_threshold_ratio` — 自动压缩阈值比例。
- `auto_compact_effective_threshold_tokens` — 当前生效的 token 阈值。
- `history(include_system=False, include_tool_messages=False)` — 导出过滤后的历史。
- `messages` — 导出原始消息列表副本。
- `last_reply()` — 返回最近一条 assistant 回复。
- `reset()` — 清空会话历史并保留 system prompt。
- `load_messages(messages)` — 替换当前消息历史。
- `revert(steps=1)` — 撤回最近 N 个 user turn，返回实际撤回数量。
- `fork_at_turn(user_turn_index)` — 返回截至第 N 个 user turn 的消息列表。
- `user_turn_count()` — 当前 user turn 数量。

### MCP 接口

```python
print(agent.mcp_servers())
print(agent.mcp_status())

ok, message = agent.mcp_start("ssh-mcp")
ok, message = agent.mcp_stop("ssh-mcp")

agent.mcp_enable("ssh-mcp", save=True)
agent.mcp_disable("ssh-mcp", save=True)

manager = agent.mcp
```

- `mcp` — 直接访问 `MCPManager`。
- `mcp_servers()` — 返回配置中的 MCP server 字典。
- `mcp_status()` — 返回 server 运行状态。
- `mcp_start(name)` / `mcp_stop(name)` — 启停单个 server。
- `mcp_enable(name, save=False)` / `mcp_disable(name, save=False)` — 修改 server enabled 状态并启停；`save=True` 时写回配置文件。

`MCPManager` 还提供：

- `start_enabled()` — 启动所有 enabled server。
- `stop_all()` — 停止所有已启动 server。
- `get_tools()` — 将 MCP tools 转换为 OpenAI function tool schema。
- `call_tool(unique_name, args)` — 调用 MCP tool，名称格式为 `mcp__<server>__<tool>`。

### 会话持久化接口

`Enkiball` 初始化时会创建 `SessionStore`。如需自定义 session JSON 存储位置，只能在构造 `Enkiball` 时传入 `session_dir`：

```python
agent = Enkiball(session_dir="/path/to/sessions")
```

会话 API 用法：

```python
store = agent.init_session_store()

agent.save_session("first run")
sessions = agent.list_sessions()
record = agent.load_session(sessions[0].session_id)
```

- `init_session_store()` — 返回构造时初始化的 `SessionStore`；不接收路径参数。
- `session_store` — 当前绑定的 `SessionStore`。
- `save_session(name=None)` — 保存当前消息为 session。
- `load_session(session_id)` — 加载 session 并恢复到引擎消息历史。
- `list_sessions()` — 列出已持久化 session。

默认 session 存储位置为 `~/.config/enkiball/sessions`。如果传入自定义 `session_dir`，markdown export 目录会使用该目录同级的 `exports/`。

### 生命周期接口

```python
with Enkiball() as agent:
    agent.chat("hello")

stopped = agent.shutdown()
```

- `shutdown()` — 停止后台 executor 和所有 MCP server，返回 MCP stop 消息列表。
- `__enter__()` / `__exit__()` — 支持 `with Enkiball() as agent` 用法。
- `repr(agent)` — 输出当前 provider、model、消息数量和 token 统计摘要。

## 数据结构

### `ChatResult`

```python
@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]]
    thinking: str
    interrupted: bool
```

- `content` — assistant 文本内容。
- `tool_calls` — 本轮工具调用事件，当前格式为 `{"raw": "tool(args)"}`。
- `thinking` — reasoning/thinking 内容，仅在 provider 返回且启用时有值。
- `interrupted` — 本轮是否被停止回调或 interrupt 中断。

### `LiveSnapshot`

```python
@dataclass
class LiveSnapshot:
    content: str
    thinking: str
    tool_events: list[str]
    in_progress: bool
    started_at: float | None
    updated_at: float | None
    elapsed: float
```

用于读取当前或最近一次流式任务的线程安全快照。

### `RetryEvent`

```python
@dataclass
class RetryEvent:
    attempt: int
    max_retries: int
    delay_seconds: int
    next_retry_at: float
    error: str
```

请求失败并即将重试时传给 `on_retry` 回调。

### `TokenUsage`

```python
@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
```

- `add(prompt, completion)` — 增加一次请求用量。
- `to_dict()` — 转为普通字典。

### `SessionRecord`

```python
@dataclass
class SessionRecord:
    session_id: str
    name: str
    created_at: str
    updated_at: str
    messages: list[dict[str, Any]]
```

- `to_dict()` — 转为 JSON 可序列化字典。
- `from_dict(data)` — 从字典恢复 `SessionRecord`。

### `SessionStore`

`SessionStore` 管理 session 文件和 markdown 导出。

- `begin_new(name=None, initial_messages=None, persisted=False)` — 创建并切换到新 session。
- `create(name=None, initial_messages=None)` — 创建并持久化新 session。
- `list_sessions()` — 按更新时间倒序列出 session。
- `load(session_id)` — 从磁盘加载 session。
- `save(record)` — 保存 `SessionRecord`。
- `save_messages(session_id, messages)` — 保存指定 session 的消息。
- `save_current_messages(messages, force=False)` — 保存当前 session 消息。
- `rename_current(name)` — 重命名当前 session。
- `switch_to(record)` — 切换当前 session 指针。
- `resolve(raw)` — 将 index 或 session id 解析为 session id。
- `export_session(session_id, out_path=None)` — 导出 markdown。
- `has_user_turn(messages)` — 判断消息列表是否包含用户轮次。

## 自定义工具

LLM 可调用的工具支持运行时注册，函数签名 `func(args: dict) -> str | dict | list`：

```python
from enkiball import Enkiball

agent = Enkiball()

# 装饰器形式
@agent.register_tool(
    name="ping",
    description="Ping a host once",
    parameters={
        "type": "object",
        "properties": {"host": {"type": "string"}},
        "required": ["host"],
    },
)
def ping(args):
    import subprocess
    return subprocess.check_output(["ping", "-c1", args["host"]]).decode()

# 直接调用形式
def scan(args):
    return {"ports": [22, 80, 443]}     # dict 会被自动 JSON 编码

agent.register_tool(scan, description="Scan ports",
                    parameters={"type": "object", "properties": {}})

# 也可以传完整 OpenAI schema
agent.register_tool(my_func, schema={"type": "function", "function": {...}})

print(agent.list_custom_tools())
agent.unregister_tool("ping")
```

返回值约定：
- 返回 `str` → 原样发给 LLM
- 返回 `dict` / `list` → 自动包装为 `{"ok": true, "result": ...}` 后 JSON 编码
- 抛出异常 → 自动捕获为 `{"ok": false, "error": "..."}` 发给 LLM

## CLI 嵌入和自定义命令

`EnkiballCLI` 可以作为库使用。外部代码可以传入预配置的 `Enkiball`、注册工具、注册 slash command，然后启动 REPL。

```python
from enkiball import Enkiball, EnkiballCLI

agent = Enkiball()

# 注册业务工具
@agent.register_tool(name="recon", description="Recon a target",
                     parameters={"type": "object",
                                 "properties": {"target": {"type": "string"}},
                                 "required": ["target"]})
def recon(args):
    return f"recon results for {args['target']}: ..."

cli = EnkiballCLI(agent, banner="MyPentestAgent v1.0 - /help for commands")

# 注册自定义斜杠命令
def cmd_target(argv, handler, engine, session):
    """/target <host>  - set the active target host"""
    if len(argv) < 2:
        handler.console.print("usage: /target <host>", style="yellow")
        return True
    engine.messages.append({
        "role": "system",
        "content": f"Active target: {argv[1]}",
    })
    handler.console.print(f"target set: {argv[1]}", style="green")
    return True   # True = keep CLI running, False = exit

cli.register_command(
    "target", cmd_target,
    help_text="Set the active target host",
    completions=["help"],   # 补全建议
)

cli.run()
```

命令处理函数签名：`(argv, handler, engine, session) -> bool`
- `argv` — `shlex.split` 后的列表，`argv[0]` 是命令本身
- `handler` — `CommandHandler` 实例（可访问 `console`、`config`、`session_store`、`staged_images` 等）
- `engine` — `ChatEngine`（消息、tokens、模型）
- `session` — prompt_toolkit `PromptSession`，可用于子 REPL（参考 `/mcp`、`/session`）
- 返回 `True` 继续运行，`False` 退出 CLI

`pyproject.toml` 中的 `enkiball` 入口仍指向内置 `cli:main`，等价于 `EnkiballCLI().run()`。

`EnkiballCLI(...)` 构造参数：

- `agent` — 已初始化的 `Enkiball`，省略时按默认配置创建。
- `console` — Rich `Console`。
- `history_path` — prompt_toolkit 输入历史文件路径。
- `banner` — CLI 启动提示文本。
- `input_transform` — 输入转换回调，签名为 `(text, image_paths) -> text | (text, system_context)`。
- `turn_lock` — 可选锁对象，用于外部协调单轮执行。
- `on_session_change` — session 切换回调，签名为 `(session_id) -> None`。

`EnkiballCLI.register_tool(...)` 会直接代理到 `agent.register_tool(...)`。

## 内置 LLM 工具

LLM 在对话中可以按需调用以下内置工具。这些工具是模型工具，不是用户 slash command。

- `read_file` — 读取 UTF-8 文本文件。
- `write_file` — 写入或追加 UTF-8 文本文件。
- `edit` — 精确替换现有 UTF-8 文本文件中的唯一匹配，支持 `replace_all`。
- `run_command` — 在 Linux bubblewrap 沙箱中执行 shell 命令并返回 exit code、stdout、stderr。

文件工具只能访问 `cwd` 和 `dir_permissions`。命令工具使用相同目录作为可写根目录，其他宿主路径不可见，网络默认隔离，环境变量经过清理，超时会终止整个进程组。当前 exec 沙箱要求 Linux 和 `bubblewrap`；后端不可用时拒绝执行，不会退化为宿主 shell。

工具 schema 和分发逻辑位于 `src/enkiball/llm_tools.py`，文件实现位于 `src/enkiball/tools.py`，权限和命令沙箱位于 `src/enkiball/sandbox.py`。

## 项目结构

```text
src/enkiball/
├── __init__.py          包根导出
├── agent.py             Enkiball 高层库 API
├── cli.py               CLI runner 和入口
├── cli_renderer.py      Rich Markdown 和工具调用渲染
├── commands.py          slash command 处理
├── config.py            配置、provider/model、state 管理
├── llm.py               ChatEngine，LLM 请求和工具循环
├── llm_tools.py         内置 LLM 工具 schema 和分发
├── mcp.py               MCPManager
├── mcp_client.py        MCP transport client
├── message_codec.py     消息、图片和 API payload 编解码
├── sandbox.py           目录权限和 Linux 命令沙箱
├── session_store.py     会话持久化和导出
└── tools.py             内置本地工具实现
```

## 架构设计（规划中）

----

初版设想（2026.01）

三个核心组件：Task Controller、Main Agent、SubAgent。

Task Controller 维护结构化任务树，每个 Task 包含 Target、Context、Subtasks、Process、Status。Main Agent 通过工具操作 Task Controller 来分解和调度任务，SubAgent 异步执行子任务。监察循环独立于用户对话上下文运行，通过 Task Controller 进行信息交互。

-----

2026.4.18

先明确webhack几个高层次的设计。

其一是任务和状态的思维悬浮，这是通用agent的能力方面。通过外部server保存任务（任务树）和状态（考虑是否需要状态服务器）来模拟悬浮状态，但是不指望AI完全自觉地查看server，这是因为AI往往意识不到已经输出了多长的信息，llm会倾向于将当前的最近上下文进一步展开，所以实践来看最好是硬性的注入。
考虑一下注入的方式，最简单的实现是每次交互时在输入的开头注入，但是这样在单轮长context的场景下效果会很差。另一种也许可行的方式是监测输出长度，到达阈值后直接interrupt，然后注入悬浮信息和continue指令。

有必要吗……？supervisor本身也可以充当悬浮的角色做监察循环，随时中断executors并发送修正指令。

感觉还是有必要，设想这样的场景：executor0完成了请求规范解析工作，executor1随后被分配了参数fuzz任务，但executor1执行的过程中发现了一些参数上的限制，需要executor0继续进一步解析，因为executor0才有这个任务的上下文，他最合适。此时选择有二：一是等待supervisor监测，然后supervisor来继续给executor0发指令，在之后的循环里再交给executor1；二是直接用ACP/A2A协议跟executor0通信。显然后者是效率更好的，那要达成后者的效果，executor1就必须要能把控全局状态，知道executor0的存在，所以悬浮信息向executor的注入应该仍然是有必要的。

否定二，不利于系统状态一致性。

那么在意识到supervisor后再回过头来看注入的方式。感觉这样也许可以：每次executor交互时注入悬浮状态，并且在supervisor的监察循环里，如果判定当前agent已经偏离方向，那么强制打断并注入后continue。

如果偏离方向应该直接结束偏离方向的executor并提取有价值信息，新开一个继续跑。

其二是人机协同，这是黑盒web测试的特化能力方面。因为让现在的ai不好测web站点的重要限制就是业务逻辑，各种认证和交互都刚需现实人类身份，而且不能放心让ai做一切操作，需要人类来决定风险程度是否可以进行某一种操作。首先最容易想到的是开一个带窗口的playwright，agent操作觉得需要被确认的时候给人发信号，人在浏览器里认证或点击或告诉ai可以做。这里的问题是敏感操作本身是不可规则化识别的，而且也需要能always allow某一种操作，不然人类交互没完没了了。感觉这个没有最终解，只能尽可能地让ai多意识到这些东西。

可以让ai请求的同时给出匹配规则，人类同意规则后加入白名单。

人机协同的另一层是人类随时可以和agent交流工作进度，这当然也是supervisor实现的，supervisor实时提取全局状态和executor进度来输出报告，并从人类输入下发任务给executors而不必中断当前流。

supervisor这个角色不打算设计为一个单纯的对话流，而是有个循环在跑一些固定的异步任务，用户跟supervisor的交互和这个循环是独立的，同时还支持单次对话不污染对话流，大概是提供一个命令如/btw <prompt>。

固定的任务：

- 对每个executor，审查其当前进度有没有偏离任务目标，有没有去做一些在其他地方已经得到最终确定结果的重复事情
- 维护进度文档，处理更改请求，这个文档同时也是状态图，给目标做状态建模，必须要有严格的格式约束，不能写太多“接下来在做什么”“已经完成了什么”之类的内容，因为这些是可以从task server拿的，状态图就应该专注于业务逻辑和站点信息，还有人类给的附加信息如账户凭证
- 维护任务树，标记已经完成或宣告失败的任务，写入新的子任务并分配executor；对已经完成的任务，附加报告信息，给状态图提交更改请求
