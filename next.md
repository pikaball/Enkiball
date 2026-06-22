# WebHack 后续改进记录

本文总结 WebHack 在实际测试中暴露出的系统结构性问题，以及后续应优先推进的改进方向。重点是把 WebHack 从“会话型 agent + 任务列表”继续推进为一个可长期运行、可审计、可恢复、适合安全敏感场景的编排系统。

## 目标架构

WebHack 应该是一个 durable operations system，而不是一个大型聊天 agent。

核心角色：

- 前台 supervisor：面向用户的全局统筹者。它拥有完整系统控制权，可以创建和修改任务、写状态、启动/停止 loop、调度/中断 executor，但默认职责应是收集约束、建立任务树、协调和安全干预，而不是亲自执行具体探测。
- 后台 loop：真正的日常运行 supervisor。它独立运行，每一轮尽量无历史，基于 durable workspace 进行任务审核、运行中 executor 审计、状态图维护、任务拆解和调度。
- executor：一次性、聚焦的叶子任务执行者。它绑定一个 leaf task，读取 durable context，执行具体目标工作，输出证据和结果，但不拥有任务树/状态图写权限。
- durable workspace：任务树、状态图、human notes、events 的事实源。长期任务的正确性应依赖这里，而不是依赖某个 chat history。

前台 supervisor 和 loop 设计上可以拥有同等完整控制权。问题不在于权限必须被移除，而在于系统要让前台 supervisor 更稳定地选择“统筹、委派、少量安全干预”，而不是在前台会话中变成 executor 或 loop。

## 实测暴露的问题

### 1. 前台 supervisor 变成了 executor

测试中，前台 supervisor 直接调用 Chrome 工具读取页面、执行 JavaScript、捕获网络流量、点击菜单、测试 IDOR URL。

这导致：

- 前台 history 被大量浏览器工具结果污染。
- 具体目标探测绕过了 task -> executor -> review 的路径。
- 后续状态变化很难判断来源。
- 前台 supervisor 在事实上承担了 executor 的工作。

期望行为：

- 具体 target 交互应任务化，交给 executor。
- 前台只做少量环境确认、安全检查或用户明确要求的操作。
- 任何需要浏览页面、抓流量、尝试参数、读取应用数据的工作，默认都应该被转化为 leaf task。

### 2. 前台 supervisor 变成了 loop

loop 启动后，前台 supervisor 仍然频繁调用 `capture_executor` 查看 executor 进度。

这导致：

- 前台 history 快速膨胀。
- executor 输出被大量塞进对话上下文。
- 前台 supervisor 对 executor 细节过拟合。
- loop 的常规监控职责被前台重复执行。

期望行为：

- loop 负责常规监控、审核、拆解和调度。
- 前台只在用户询问实时状态、怀疑安全风险、需要协调决策或执行人工 override 时查看 executor。
- 前台查看 executor 时优先 `list_executors`，只有必要时对单个 executor 做 capture。
- 完整 executor 输出应主要给人通过 CLI 查看，而不是塞入 LLM 前台 history。

### 3. 前台 supervisor 直接把任务标记为 done

测试中，前台 supervisor 根据 executor capture 直接调用 `update_task(status="done")`。

这绕过了预期生命周期：

```text
running -> review -> loop review -> done / failed / blocked / decomposed / retry
```

实际造成的问题：

- task 进入 `done`，但 `attempts` 为空。
- task 仍保留旧的 `executor_id`。
- `evidence` 为空。
- 无法审计这个 done 是基于哪次 executor attempt 和哪条证据做出的。

期望行为：

- executor 结束后先形成 attempt。
- task 进入 `review`。
- loop review 再决定最终状态。
- 前台直接改 done 应该是非常明确的人工 override，而不是普通路径。

后续建议：

- 增加任务状态机约束。
- task 离开 `running` 时自动清理 `executor_id`。
- 对状态变更记录 actor/source，例如 foreground、loop-review、active-audit、CLI、debug。

### 4. root 和 parent task 的状态语义不清

测试中 root task 已有多个子任务，但状态仍是 `todo`。

这会造成误导：

- `/task list` 看起来 root 仍未开始。
- parent task 和 leaf task 的语义混在一起。
- 后续 roll-up 审核缺少清晰规则。

期望行为：

- 只有 leaf task 可以是 `todo` 或 `running`。
- 有 children 的 task 应进入 `decomposed`、`blocked`、`done`、`failed` 或 `cancelled`。
- parent task 的完成应由子任务状态和 loop 的归纳审核决定。

后续建议：

- 创建 child 时，如果 parent 是 `todo`，自动改为 `decomposed`。
- `ready_tasks()` 只返回 `todo` leaf task。
- 增加 task tree invariant 检查和修复逻辑。

### 5. 初始任务拆分存在重叠

测试中的初始任务同时包含：

- 横向任务：网络流量捕获与 API 分析。
- 模块任务：学籍管理、培养管理、学位管理等。

实际运行中，横向任务开始主动探索多个模块，和模块任务发生重复。

问题：

- 多个 executor 做同一类探索。
- 浏览器目标互相干扰概率上升。
- loop 后续不得不中断部分重复任务。
- 任务结果归属不清。

期望行为：

- 初始任务要尽量互斥。
- 如果有全局观察任务，要明确它是否允许主动导航。
- 如果任务只是抓取其他 executor 触发的流量，应在 context 里写清楚“不主动探索模块”。
- 有依赖关系的任务应在 context 中明确依赖，避免抢跑。

后续建议：

- 改进任务创建策略，减少横向任务和模块任务的重叠。
- 后续如确有必要，再增加轻量依赖字段；暂时不急着引入复杂调度模型。

### 6. 安全约束没有硬化

用户强调敏感系统、禁止破坏性操作，并多次表达“只读/GET-only”倾向。但实际过程中 executor 分析甚至可能调用了 POST 接口，因为目标系统的部分只读数据 API 本身使用 POST。

核心问题：

- “只读语义”和“HTTP GET-only”没有被系统清晰区分。
- prompt 约束不足以保证 HTTP method 级别安全。
- 当站点使用 POST 做只读查询时，系统应停下来确认，而不是自动扩大授权。

期望行为：

- 如果用户明确说 GET-only，那么 POST 即使看起来是只读接口，也应被视为 blocker 或 clarification point。
- 如果需要 read-only POST，应明确询问用户是否允许。
- 访问他人敏感信息，即使只是读取，也应视为高敏感操作。

后续建议：

- 任务 context 中明确 allowed/forbidden method。
- 未来接入 HTTP 工具时考虑工具层拦截。
- 对跨账号/他人敏感数据访问引入更明确的确认流程，但暂时不做完整审批引擎。

### 7. 敏感数据落盘过多

executor 输出和 task result 中出现了学号、姓名等个人信息。

问题：

- WebHack 的目标是发现敏感信息泄露，但自身不应无必要地长期保存敏感信息。
- 直接把完整个人信息写入 durable task result 或 attempts 会增加风险。

期望行为：

- durable state 和 task result 应尽量脱敏。
- 使用 evidence reference 或摘要，而不是直接保存完整个人信息。
- 只保存证明漏洞所需的最小信息。

后续建议：

- 在写 attempts/result/state 前做基础 redaction。
- 对学号、手机号、身份证号、姓名等信息进行最小化保存。
- 长期更好的做法是 artifact 引用，而不是直接保存完整响应。

### 8. Chrome targetId 纪律仍然脆弱

测试中出现：

- 前台 supervisor 自造 `webhack-task-root`。
- 前台 supervisor 自造 `webhack-idor-test`。
- executor 页面出现跳转到其他 host 的情况。
- 有任务 context 中建议 executor 创建 `-v2` targetId。

问题：

- targetId 命名规则不够统一。
- foreground target 和 task-bound executor target 混用。
- 如果模型自由创造 targetId，并发隔离会变得不可审计。

期望行为：

- 正常任务执行使用 `webhack-task-<task_id>`。
- executor 不应自造 targetId。
- 如果需要 attempt-level targetId，也应由代码分配，而不是 prompt 里临时生成。
- active audit 应能发现 URL/host 偏离任务目标。

后续建议：

- 进一步观察稳定 task target 是否足够。
- 如果不够，再引入代码分配的 attempt targetId，例如 `webhack-task-<task_id>-attempt-<n>`。
- 增加 active audit 对当前 URL/host 的检查。

### 9. state graph 需要更严格约束

测试中的 state graph 总体有用，但暴露了几个问题：

- confidence 出现了 `high`，不符合预期枚举。
- state maintenance 曾出现 JSON parse error。
- 有些 fact 包含太多信息，不够原子化。

期望行为：

- state graph 只存稳定业务/站点事实。
- 不存任务进度、计划、executor 状态、泛泛总结。
- confidence 固定为 `observed`、`inferred`、`claimed`、`assumed`。
- facts 更小、更容易去重和引用证据。

后续建议：

- `_validate_fact()` 强制 confidence enum。
- 对 LLM 输出做更鲁棒的 JSON 提取。
- 只在有候选事实或状态图漂移时运行 state maintenance。
- 增加 fact dedupe。

### 10. `/btw` 语义已经明确

`/btw` 的正确语义：

- 基于当前前台对话 snapshot 的旁路问话。
- 能看到前面的 foreground history。
- `/btw` 自身 user/assistant turn 不写回 foreground history。
- 不应拥有写入工具，否则后续普通对话看不到导致写入的上下文。

当前行为：

- `/btw` 使用临时 agent 加载当前 foreground messages。
- 只注册 read-only tools。
- 问完丢弃临时 history。

这个方向是正确的。

### 11. `/task` 安全控制已经必要

实际运行证明，需要能从任务层面快速停止执行。

当前目标行为：

- `/task list` 展示完整任务树和每个节点是否 active。
- `/task stop <id>` 停止指定 task 的 executor。
- `/task stop-all` 停止所有 running task。

后续建议：

- stop reason 不应总写入 `blocked_reason`，除非任务确实 blocked。
- interrupt/duplicate/drift 等原因更适合写入 attempt metadata 或 result。

## 后续优先工程事项

### 1. 强化任务状态机

建议优先实现：

- parent task 不允许保持 `todo/running`。
- 只有 leaf task 能 `todo/running`。
- task 离开 `running` 时自动清 `executor_id`。
- `running -> done` 不应是普通路径。
- `done` task 应有 attempt/evidence 或明确 override 记录。

### 2. 给状态变更记录来源

每次写 task/state/human note 时记录 actor/source：

- foreground supervisor
- loop review
- active audit
- CLI
- debug

这样后续发现状态变化时，可以追溯“是谁/哪个组件/哪条路径”改的。

### 3. 改善 foreground `update_task` 语义

不一定要移除 supervisor 权限，但普通工具语义应更安全。

可考虑：

- 普通 `update_task` 只改 goal/context/completion_criteria/blocker 等字段。
- 状态 override 走单独工具或要求显式 reason。
- 所有 override 进入 event log。

### 4. 降低 foreground capture 污染

建议：

- LLM 工具版 `capture_executor` 默认只返回短摘要。
- 完整输出保留给 CLI `/exec cap` 给人看。
- `list_executors` 提供足够的轻量状态摘要。

### 5. 强化 state graph validation

建议：

- confidence enum 强制化。
- JSON 提取更鲁棒。
- state maintenance 失败时不污染状态。
- facts 去重。

### 6. 敏感数据最小化

建议：

- attempts/result 写入前做基础脱敏。
- state facts 中避免完整个人敏感信息。
- 未来使用 artifact/evidence 引用替代大段原始内容。

### 7. 继续验证 targetId 策略

下一轮测试重点观察：

- task-bound targetId 是否仍会串页面。
- SSO/window.open 是否导致 target 绑定丢失。
- 是否需要 attempt-level targetId。

如果需要，应由代码统一分配，而不是让模型自造。

### 8. 改善任务创建质量

建议：

- 初始任务应减少重叠。
- 横向任务要明确是否主动导航。
- task context 中写清 hard constraints。
- 依赖关系先写入 context，暂不引入复杂调度字段。

## 暂时不做

以下能力暂时不引入，避免过度设计：

- 复杂 priority/resource 调度。
- 完整人工审批策略引擎。
- 浏览器/account/session resource lease。
- 大规模扩展 task schema。
- 漏洞扫描器、HTTP replay engine、匹配规则系统。

原则：先把 orchestration core 做稳，再按真实需求增加能力。
