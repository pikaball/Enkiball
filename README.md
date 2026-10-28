# Enkiball

Enkiball 是一个面向长上下文任务的 Python LLM 基座，同时提供交互式 CLI 和 Python 库 API。

当前核心能力包括多 provider/model 管理、流式对话、图片输入、工具调用、MCP 接入、会话持久化、历史压缩、实时输出捕获和后台执行。

## 目录

- [安装](#安装)
- [配置](#配置)
- [CLI 使用](#cli-使用)
- [Python API](#python-api)
- [数据结构](#数据结构)
- [自定义工具](#自定义工具)
- [Skills](#skills)
- [CLI 嵌入和自定义命令](#cli-嵌入和自定义命令)
- [内置 LLM 工具](#内置-llm-工具)
- [项目结构](#项目结构)

## 安装

```bash
pip install enkiball
```

从源码开发安装：

```bash
pip install -e .
```

安装后 `enkiball` 命令全局可用，可在任意目录下启动。

运行环境要求：Linux、Python `>=3.10`。默认命令沙箱需要系统安装 `bubblewrap`（Debian/Ubuntu：`sudo apt install bubblewrap`）。Python 依赖由 pip 自动安装。

## 配置

配置文件位于 `~/.config/enkiball/enki.json`。首次运行时会自动创建默认配置；将其中的 `api_key` 替换为你的 API key，并按需调整 provider 和模型：

```json
{
  "system_prompt": "You are Enkiball, a practical assistant for long-context tasks.",
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
- config 和 state 在每个实例启动时各读取一次。运行中使用实例内存快照；`/model`、`/provider switch` 的选择仍保存供后续启动使用，但不会影响其他已启动实例。共享文件以最后一次写入为准。
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
        "environment": {
          "NODE_ENV": "production",
          "MCP_TOKEN": "replace-me"
        },
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
- Stdio server 使用 `command`、`args`、`transport`，以及可选的环境变量映射 `environment`（兼容旧字段 `env`）；配置值会覆盖同名宿主环境变量。
- 远程 server 使用 `url`；`type=streamable-http` 时走 Streamable HTTP，否则默认按 SSE 处理。

## CLI 使用

```bash
enkiball
```

内置文件和命令工具默认只能访问启动目录及其子目录。可以指定工作目录和额外授权目录：

```bash
enkiball --cwd /path/to/project --dir-permission /path/to/shared --ro-permission /path/to/reference
```

CLI 默认在工具请求访问授权目录之外时提供 `once`、`always`、`deny` 三个选项。`once` 只放行当前请求，`always` 将目录授权到当前进程结束，`deny` 拒绝；按 `Esc` 会中断当前 LLM turn 并返回输入提示。使用 `--no-approval` 可改为静默拒绝。可重复传入 `--dir-permission` 授予读写权限，或用 `--ro-permission` 只授予读取权限。

普通输入直接发送给 LLM，支持流式输出。按 `Ctrl+J` 换行，`Enter` 发送。`Esc` 可中断请求发送后等待响应、流式数据等待、重试等待和 MCP 工具调用，也适用于 `/compact`；已收到的对话内容会保留，中断的压缩不会替换历史。

使用 `/session use` 或 `/session fork` 恢复历史时，CLI 会按实时对话相同的样式重绘 user、assistant、Markdown 表格和工具调用；历史 tool result 不会整段回显。

正文、列表、代码块和工具预览的长行由终端自然折行，CLI 不再按窗口宽度插入换行和补齐空格，方便鼠标选择复制。代码中的实际换行和缩进仍保留，工具侧边标记、行号只出现在原始行的开头。表格和有高度限制的 thinking 预览仍按终端尺寸排版。实时回复和历史回放使用相同规则。

Markdown 标题与相邻内容之间保留空行，流式输出分段提交时也会补齐标题间距。正文中的 `$...$`、`\(...\)` 以及块公式 `$$...$$`、`\[...\]` 会转换为终端可复制的 Unicode 数学文本，支持常见希腊字母、上下标、分数和根号。无法解析或不支持的公式会保留完整原文和分隔符；代码块、行内代码中的 LaTeX 保持原样。公式尚未闭合时不会因其中的空行提前提交，中断时则显示已收到的原文。

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
/btw <prompt...>           发起一次不写入当前历史的临时对话
/debug <python>            在 CLI 进程中执行 Python cell
/notice show               查看待发送的 sys-notice
/notice add <message>      添加一条待发送的 sys-notice
/image add <path...>       挂载图片
/image list                查看已挂载图片
/image remove <index>      移除一张图片
/image clear               清空挂载图片
/session list              列出所有会话
/session current            当前会话信息
/session new [name]        新建会话
/session use <id|index>    切换会话（屏幕恢复历史）
/session rename <name>     重命名当前会话
/session export [path]     导出会话为 markdown
/session fork <turn>       从第 N 轮用户输入处 fork 新会话
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
    Skill,
    SkillRegistry,
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
    agent.notice_queue.append("The background scan has completed.")
    result = agent.chat("scan open ports on localhost")
    print(result.content)

    for token in agent.stream("explain the result"):
        print(token, end="", flush=True)
```

`notice_queue` 是公开的、兼容 `list[str]` 的延迟通知队列，上层 harness 可直接
`append()`、`extend()` 或 `clear()`。每次任意工具调用返回后，队列在该时刻的
全部内容会作为 `<sys-notice>` 附加到工具结果中并一次性消费；如果模型没有调用
工具，通知会继续留在队列中，不会插入或中断当前对话流。

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
    on_tool_event=None,
    session_dir=None,
    cwd=None,
    dir_permissions=None,
    ro_permissions=None,
    approval=False,
    sandbox=True,
    sandbox_network="none",
    sandbox_env=None,
    sandbox_inherit_env=None,
    state_path="~/.config/enkiball/state.json",
    skill_dirs=None,
)
```

- `config_path` — 配置文件路径，默认 `~/.config/enkiball/enki.json`。
- `system_prompt` — 覆盖配置文件中的 system prompt。
- `auto_start_mcp` — 初始化时是否启动配置中 `enabled=true` 的 MCP server。
- `on_tool_event` — 工具调用事件回调，签名为 `(event: str) -> None`。
- `session_dir` — session JSON 文件存储目录。session 存储位置只允许通过这个构造参数自定义。
- `cwd` — 内置文件和命令工具的工作目录，默认是创建 agent 时的当前目录。
- `dir_permissions` — 内置工具可以额外读写的目录列表；相对路径以 `cwd` 为基准。
- `ro_permissions` — 内置工具只能读取的额外目录列表；相对路径以 `cwd` 为基准。
- `approval` — 是否允许交互前端处理越界目录审批。API 没有绑定交互前端时仍会静默拒绝；CLI 提供 `once`、`always`、`deny` 三选项。
- `sandbox` — 是否启用命令沙箱，默认 `True`；设为 `False` 时 `run_command` 直接继承宿主环境执行。
- `sandbox_network` — 沙箱命令的网络模式：`"none"`（默认，隔离网络）或 `"host"`（共享宿主网络，包括 localhost、LAN 和互联网）。
- `sandbox_env` — 显式传给沙箱命令的环境变量映射。
- `sandbox_inherit_env` — 允许从宿主继承的环境变量名称列表；名称不存在时拒绝执行。
- `state_path` — active provider/model 的持久化状态文件。已启动实例始终独立；不同 harness 可传不同路径，进一步隔离下次启动的默认选择。
- `skill_dirs` — 包含 skill 子目录的目录列表。默认扫描 `~/.config/enkiball/skills`；传入空列表可禁用 skills。

`Enkiball` 支持 context manager。退出 `with` 块时会调用 `shutdown()` 停止后台 executor 和 MCP server。

### 对话接口

#### `chat()`

同步发送一轮用户输入，阻塞直到完整回复结束。

```python
result = agent.chat(
    "analyze this screenshot",
    images=["screen.png"],
    on_token=lambda token: print(token, end="", flush=True),
    on_tool_event=lambda event: print(f"\n[tool] {event}"),
    on_thinking=lambda text: print(f"\n[thinking] {text}"),
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
- `on_token` — 可选流式 token 回调。
- `on_tool_event` — 可选工具调用事件回调；与构造时回调不同，作用于本轮。
- `on_thinking` — 可选 reasoning/thinking 增量回调。
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
import time

future = agent.chat_in_background("run a long analysis")

while not future.done():
    snapshot = agent.capture()
    print(snapshot.content[-120:])
    time.sleep(0.1)

result = future.result()
```

- `chat_in_background(...)` — 在线程池中启动 `chat()`，返回 `Future[ChatResult]`。
- `capture(collect_thinking=False)` — 返回 `LiveSnapshot`，可从任意线程读取当前或最近一轮的累计输出；正文、工具调用和工具结果按发生顺序拼接。传入 `True` 时，thinking 也在原位置插入。
- `wait(timeout=None)` — 同步阻塞，等待调用时的活动 chat/compact 轮次及已经提交的后台任务结束，包括初始化和资源清理。结束或当前空闲时返回 `True`，超时返回 `False`；超时不会中断任务。失败或取消也算结束，结果与异常需通过 `future.result()` 获取。在异步事件循环中等待应使用 `await asyncio.to_thread(agent.wait, timeout)`。
- `interrupt()` — 请求当前流式任务停止。
- `interrupted` — 当前是否已经请求 interrupt。

`capture()` 不消费缓冲区。同一轮中保持相同的 `collect_thinking` 参数，旧快照的 `content` 始终是新快照的前缀；下一轮对话开始时清空。工具调用在参数收集完整、即将执行时写入，工具结果在执行结束后写入，不包含命令执行过程中的 stdout 增量。图片工具结果包含文字和图片占位标记，不包含图片数据。后台任务刚提交时可能尚未开始，判断该任务是否完成请使用 `future.done()` 或 `wait()`。

模型请求只对连接故障、超时、HTTP 408/409/429 和 5xx 错误自动重试。一旦本次请求已输出正文或 thinking，断流会直接抛错，避免重新生成导致重复输出；参数、鉴权和普通回调错误不自动重试。重试判断独立于 `capture()`。

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
```

- `config` — 当前配置的深拷贝。
- `provider` — 当前 provider 名称。
- `providers` — 所有 provider 配置。
- `model` — 当前模型名，可读写。
- `switch_provider(name)` — 切换 provider，返回是否成功并持久化选择。
- `switch_model(model)` — 切换模型并持久化选择。
- `list_models(provider_name=None, remote=True, timeout=5.0)` — 返回 config 模型和远程发现模型的合并列表。

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
- `show_thinking` — CLI 是否显示 reasoning/thinking 内容；不影响 API 回调、`ChatResult.thinking` 和内部 thinking 采集。通过 `capture(collect_thinking=True)` 可将 thinking 包含在快照中；该开关不控制服务端是否启用推理。
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
print(agent.mcp.status())

ok, message = agent.mcp.start("ssh-mcp")
ok, message = agent.mcp.stop("ssh-mcp")

agent.mcp_enable("ssh-mcp", save=True)
agent.mcp_disable("ssh-mcp", save=True)
```

- `mcp` — 直接访问 `MCPManager`。
- `mcp_enable(name, save=False)` / `mcp_disable(name, save=False)` — 修改 server enabled 状态并启停；`save=True` 时写回配置文件。

`MCPManager` 还提供：

- `start_enabled()` — 启动所有 enabled server。
- `stop_all()` — 停止所有已启动 server。
- `get_tools()` — 将 MCP tools 转换为 OpenAI function tool schema。
- `call_tool(unique_name, args, cancel_event=None)` — 调用 MCP tool，名称格式为 `mcp__<server>__<tool>`；可传入 `threading.Event`，置位后退出等待并抛出 `TurnInterrupted`。

### 会话持久化接口

`Enkiball` 初始化时会创建 `SessionStore`。如需自定义 session JSON 存储位置，只能在构造 `Enkiball` 时传入 `session_dir`：

```python
agent = Enkiball(session_dir="/path/to/sessions")
```

会话 API 用法：

```python
store = agent.session_store

agent.save_session("first run")
sessions = agent.list_sessions()
record = agent.load_session(sessions[0].session_id)
```

- `session_store` — 当前绑定的 `SessionStore`。
- `save_session(name=None)` — 保存当前消息为 session。
- `load_session(session_id)` — 加载 session 并恢复到引擎消息历史。
- `list_sessions()` — 列出已持久化 session。

默认 session 存储位置为 `~/.config/enkiball/sessions`。如果传入自定义 `session_dir`，markdown export 目录会使用该目录同级的 `exports/`。每个 session JSON 会记录创建它的规范化 `cwd`。当前 cwd 只能列出和加载绑定到当前目录或其祖先目录的 session；其他目录及旧版未绑定 cwd 的 session 不进入当前作用域。

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
- `thinking` — provider 返回的 reasoning/thinking 内容，不受 CLI 显示开关影响。
- `interrupted` — 本轮是否被停止回调或 interrupt 中断。

### `LiveSnapshot`

```python
@dataclass
class LiveSnapshot:
    content: str = ""
    in_progress: bool = False
```

用于读取当前或最近一轮对话的线程安全快照，仅包含以上两个字段。

- `content` — 按发生顺序拼接的正文、可选 thinking、`<tool_call_pending ...>` 和 `<tool_result ...>` 文本；包含本轮多次模型请求的输出。
- `in_progress` — 当前对话是否仍在执行，包含资源清理阶段；任务提交到后台队列但尚未开始时为 `False`。它不表示执行是否成功，也不跟踪独立的 `compact()` 操作。

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
    cwd: str | None
```

- `to_dict()` — 转为 JSON 可序列化字典。
- `from_dict(data)` — 从字典恢复 `SessionRecord`。

### `SessionStore`

`SessionStore` 管理 session 文件和 markdown 导出，并按构造时绑定的 cwd 过滤 session。

- `begin_new(name=None, initial_messages=None, persisted=False)` — 创建并切换到新 session。
- `create(name=None, initial_messages=None)` — 创建并持久化新 session。
- `list_sessions()` — 按更新时间倒序列出属于当前 cwd 或其祖先目录的 session。
- `load(session_id)` — 从磁盘加载当前 cwd 作用域内的 session。
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

工具名不能覆盖内置工具或 `mcp__` 前缀；重复注册会直接报错。一个 `Enkiball` 同时只运行一个 chat/compact operation，并发启动会 fail fast。`interrupt()` 会停止流式响应，并终止正在执行的 `run_command` 进程组。

MCP 的 Stdio、SSE、Streamable HTTP 工具调用均接收同一个停止信号：退出本地等待、关闭当前 HTTP 请求，并尽力发送 `notifications/cancelled`（通知发送最多等待约 0.25 秒）。正常取消保留 MCP 连接供后续调用；Stdio 若在写入半个消息时中断，会关闭该进程以免破坏后续协议，需要重新启动。服务端可能忽略取消通知，因此不保证远端任务已经停止或回滚。需要关闭本地 MCP 服务时，使用 `/mcp stop` 或 `agent.mcp.stop(name)`，会终止它所属的进程组。自定义 Python 工具仍需要自行实现取消逻辑。

## Skills

Enkiball 支持 [Agent Skills](https://agentskills.io/specification) 格式。每个 skill 是一个包含 `SKILL.md` 的目录：

```text
~/.config/enkiball/skills/
└── code-review/
    ├── SKILL.md
    ├── references/
    ├── scripts/
    └── assets/
```

最小 `SKILL.md`：

```markdown
---
name: code-review
description: Review code changes for bugs and regressions. Use when asked to review a diff or pull request.
---

Inspect the changed code first. Report findings ordered by severity and include file references.
```

启动时只把 skill 的名称和 description 放入 `skill` 工具描述；模型判断相关后调用该工具加载正文。用户也可以在消息中使用准确的 `$skill-name` 显式激活：

```python
agent.chat("Use $code-review to inspect the current changes")
```

相关 API：

- `agent.skills` — 当前有效的 `Skill` 元数据。
- `agent.skill_diagnostics` — 无效格式和重名等发现诊断。
- `agent.load_skill(name, resource=None)` — 加载正文或相对资源。
- `agent.reload_skills()` — 重新扫描并更新 `skill` 工具。

skill 目录以只读方式加入 sandbox。`allowed-tools` 会被解析并保留，但不会授予工具权限；skill 中的脚本仍通过现有 `run_command`、sandbox 和审批规则执行。资源路径不得逃出所属 skill 目录，正文和单个 UTF-8 资源上限均为 64 KiB。

## CLI 嵌入和自定义命令

`EnkiballCLI` 可以作为库使用。外部代码可以传入预配置的 `Enkiball`、注册工具、注册 slash command，然后启动 REPL。

```python
from enkiball import CommandContext, Enkiball, EnkiballCLI

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
def cmd_target(argv, context: CommandContext):
    """/target <host>  - set the active target host"""
    if len(argv) < 2:
        context.console.print("usage: /target <host>", style="yellow")
        return True
    messages = context.agent.messages
    messages.append({
        "role": "system",
        "content": f"Active target: {argv[1]}",
    })
    context.agent.load_messages(messages)
    context.console.print(f"target set: {argv[1]}", style="green")
    return True   # True = keep CLI running, False = exit

cli.register_command(
    "target", cmd_target,
    help_text="Set the active target host",
    completions=["help"],   # 补全建议
)

cli.run()
```

命令处理函数签名：`(argv, context: CommandContext) -> bool`。
- `argv` — `shlex.split` 后的列表，`argv[0]` 是命令本身
- `context.agent` — 当前 `Enkiball` 实例
- `context.console` — Rich `Console`
- `context.session_store` — 当前 `SessionStore`
- `context.prompt_session` — prompt_toolkit `PromptSession`
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
- `debug_scope` — `/debug` 使用的 Python scope 字典；CLI 直接使用该字典，并默认补入 `agent` 和 `cli`。
- `btw_context` — 可选的临时对话上下文回调；其内容只注入 `/btw`，不会写入前台历史。

自定义 LLM 工具通过 `cli.agent.register_tool(...)` 注册。

`/btw <prompt...>` 使用当前 agent 的完整工具集和对话上下文执行一次临时问答，
但不会把这次问答追加到当前 session 历史。工具本身产生的副作用会保留。

`/debug` 按 notebook cell 方式执行 Python：普通输出直接写入终端，末尾表达式的结果以 `repr` 显示。它在 CLI 宿主进程中执行，不受工具 sandbox 或目录权限限制。

## 内置 LLM 工具

LLM 在对话中可以按需调用以下内置工具。这些工具是模型工具，不是用户 slash command。

- `read_file` — 按字符偏移读取 UTF-8 文本文件，`start`/`end` 默认为 `0`/`1000`，区间为 `[start, end)`。
- `read_image` — 模型主动读取本地图片，参数为 `path`（绝对路径或相对 `cwd` 的路径），遵循文件读取权限。支持 PNG、JPEG、GIF、WebP，单图最大 20 MiB；图片内容会附加到下一次模型请求，需要模型及端点支持视觉输入，无需手动 `/image add`。会话保存读取时的图片快照，删除或修改原文件不影响回放，但会增加会话文件大小。
- `write_file` — 写入或追加 UTF-8 文本文件。
- `edit` — 精确替换现有 UTF-8 文本文件中的唯一匹配，支持 `replace_all`。
- `run_command` — 执行 shell 命令并返回 exit code、stdout、stderr；默认使用 Linux bubblewrap 沙箱。可用 `privilleged=true` 请求一次性宿主执行审批。
- `skill` — 按需加载 skill 正文或其中的 UTF-8 资源；仅在发现到有效 skill 时注册。

`run_command` 的工具说明会随请求更新当前执行模式、工作目录、网络模式、可写/只读挂载目录及特权审批是否可用。模型可在明确需要沙箱外访问时直接调用 `privilleged=true`，也可在诊断出沙箱限制后重试；该参数触发单次审批，以当前宿主用户执行，不代表 sudo/root 提权。审批不可用时会明确告知模型，禁用沙箱时也会注明命令已经在宿主执行。

文件工具只能访问 `cwd`、`dir_permissions` 和 `ro_permissions`；其中 `cwd` 与 `dir_permissions` 可写，`ro_permissions` 只读。命令工具使用相同挂载权限，其他宿主路径不可见，网络默认隔离，环境变量经过清理；可通过 `sandbox_network="host"` 共享宿主网络，通过 `sandbox_env` 或 `sandbox_inherit_env` 显式传入环境变量。裸 CLI 对应提供 `--sandbox-network {none,host}`、可重复的 `--sandbox-env NAME` 和 `--ro-permission DIR`。超时或 turn interrupt 会终止整个进程组。交互式 `read + always` 只授予只读目录并以 `--ro-bind` 挂载；write/edit 授权才允许写入。当前 exec 沙箱要求 Linux 和 `bubblewrap`；后端不可用时拒绝执行，不会自动退化为宿主 shell。`sandbox=False` 会显式关闭命令沙箱。沙箱启用时，`privilleged=true` 每次都只提供 `allow once` 或 `deny`，通过后该命令直接在宿主环境执行。

CLI 的 `--privilleged` 模式会显式关闭命令沙箱，并允许文件工具读写任意绝对路径。该模式是启动级授权，不再逐条审批命令。

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
├── skills.py            Agent Skills 发现、验证和按需加载
└── tools.py             内置本地工具实现
```

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)（`GPL-3.0-only`）。

## 发布检查

```bash
python -m unittest discover -s tests -q
uv run --no-project --with build python scripts/build_release.py
uvx twine check --strict dist/*
```

发布构建脚本生成 wheel 和源码包，并清除源码归档中的本机文件所有者与时间戳。产物位于 `dist/`。
