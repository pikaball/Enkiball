# 安全研究agent - Enkiball

Enkiball是一个专注于安全研究任务的agent，其核心设计理念是在巨量上下文中，让llm像人类一样认知和调度任务。

## 总体架构

三个核心组件： Task Controller, main agent, subagent

Task Controller维护task结构，一个task结构如下所示：

```
Task
    Task_id
    Parent，如果是main task则置为0, 是subtask则指main task，如果是subtask,则不开放`Subtasks`字段
    Target, 对需要完成的任务的准确全面描述，包括要达成什么样的目标，以及完成任务采用什么思路进行
    Context, 为了完成这个任务所需要的上下文，不应嵌入过长内容，有文件引用应直接写文件名而不是填充文件内容，长度限制在512B以内
    Subtasks, 即为完成这这个任务而建立的subtask的Task_id和对应描述
    Process，当前任务的进度，发现，成果，以及能证明成果的分析链/证据链，长度限制在2048B以内
    Status，分为空白(Blank)，执行中（processing），挂起(Suspend: because waiting for task_xxx/ because failed)，结束(Done)
```

## 基本交互范式与逻辑

一个session在任何时刻和一个main task绑定，session创建时自动建立一个空白task, 用户也可以手动切换task, 但对当前会话中的main agent无感，用户和main agent进行交互，纯cli交互，流式输出。

main agent具有对task controller的完全操作权限，用户可以输入要求，main agent进行思考分析，然后通过用工具封装的task controller来编辑当前任务，Task的每个结构（Target, Context, Subtasks, Process, Status）都有独立的工具进行编辑，这是为了task结构永远不被破坏。

main agent本身被要求必须用subagent来完成任务，当main agent分析完任务时，可以在main task中建立subtask, 并异步启动一个subagent来执行subtask，subagent则不被允许创建另一个subagent，任何创建subagent的需求最终都由main agent来执行。subagent本身可以编辑自己的task内容，但不开放对Subtasks字段的编辑工具。

main agent可以完全操作subagent, 就像人类操作main agent一样，可以与其对话，可以掐断输出，可以压缩history

当前main task的Status不为Blank时，后台启动监察循环，在每一次监察循环中，main agent查看subtasks的状态，对每一个subtask对应的subagent进行如下操作：

0. 如果subtask是结束状态，或已经被标记为失败的尝试，则跳过
1. 从task controller获取subtask的完整内容，检查当前subagent的工作流是否偏离了subtask的目标
2. 检查subagent的输出和思考内容，如果发现方向发生了偏离，则中止并与其对话进行提醒
3. 允许编辑和压缩subagent的history，确保其注意力集中，压缩的原则是保守的，去除无关内容，保留任何可能有用的信息。可压缩的内容比如：subagent尝试执行命令，但执行环境有一些问题，subagent进行了修复，然后执行成功，此时这个修复过程应从history中删掉以防止注意力偏移
4. 评价当前subagent的未来预期，如果判定其难以完成任务，则可以继续将这个subtask细分成多个subtask, 或者创建新的subtask用于尝试另外的思路；如果subagent连续3次被评价为继续下去没有完成任务的可能，则subagent被标记为挂起（失败的尝试）并停止其执行。
5. 更新main task

监察循环的交互不占用用户对话的上下文，两者完全依赖task Controller来进行信息交互

## 当前可用基础框架（已实现）

目前仓库已经提供一个可运行的 CLI 基座，满足以下能力：

1. 交互式终端界面（`prompt_toolkit`）：
   - 支持方向键历史导航
   - 支持 Tab 自动补全
   - 支持命令历史建议
2. 普通输入直连 LLM，对话采用流式输出（`openai` SDK）
3. `/xxx` 特殊命令系统：
   - 模型切换
   - 会话管理（创建、切换、导出）
   - MCP 开关管理 + MCP 进程启停
4. 工具系统（Tool Call）：
   - `read_file`
   - `write_file`
   - `run_command`
   - 这些工具不暴露为用户命令，而是由 LLM 在对话中按需自动调用
5. MCP 采用配置文件驱动（`config/agent.json`）：
   - 支持 local(`command` + `args`) 和 remote(`url`) 两类 server
   - 用户可手动编辑 `enabled` 字段开关 server
   - 也可在 CLI 内通过 `/mcp` 交互面板逐个开关（启用时会立即尝试启动）
6. 会话存储：
   - 每次启动默认创建一个新的“未保存会话”，不会续接上次会话
   - 首次真实对话发生后，自动用首条输入生成会话标题并保存到本地 `data/sessions/*.json`
   - 后续每轮对话会自动保存
   - 支持在 CLI 内切换 session
   - 支持完整对话导出为 markdown

## 启动方式

```bash
uv sync
cp .env.example .env
# .env
# API_KEY=your_key
# BASE_URL=https://your-api-base/v1
uv run enkiball
```

如果你希望安装为可直接执行的系统命令（无需 `uv run`）：

```bash
uv tool install --editable .
enkiball
```

`BASE_URL` 为可选项；若不填则使用官方默认地址。  
`agent.json` 只保留模型、采样参数和 MCP 配置，例如：

```json
{
  "llm": {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "temperature": 0.2,
    "max_tokens": 2048
  },
  "mcp": {
    "servers": {
      "local-example": {
        "enabled": true,
        "command": "node",
        "args": ["/path/to/local-mcp-server.js"],
        "transport": "jsonl"
      },
      "remote-example": {
        "enabled": true,
        "type": "streamable-http",
        "url": "http://127.0.0.1:12306/mcp"
      }
    }
  }
}
```

## CLI 命令

```text
/help
/exit
/model
/model <name>
/revert [n]
/image add <path...>
/image list
/image remove <index>
/image clear
/image help
/session
/session list
/session current
/session new [name]
/session use <id|index>
/session rename <name>
/session export [path]
/session help
/mcp
/mcp list
/mcp enable <name|index>
/mcp disable <name|index>
/mcp toggle <name|index>
/mcp start <name|index>
/mcp stop <name|index>
/mcp status
/mcp help
/mcp path
```

## 图片发送

支持在同一条用户消息中混合发送文本和图片：

```text
/image add ./screenshots/a.png ./screenshots/b.jpg
you> 请结合这两张图分析页面中的安全风险
```

只发图片也可以：

```text
/image add ./capture.png
you> 
```

辅助命令：

- `/image list` 查看已挂载图片
- `/image remove <index>` 删除一个挂载图片
- `/image clear` 清空挂载图片
- `/revert [n]` 撤回后会恢复该轮图片挂载，便于编辑重发

## 工程文档

- 架构说明：`docs/ARCHITECTURE.md`
- 开发路线图：`docs/ROADMAP.md`
