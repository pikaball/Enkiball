# Enkiball Teacher

基于 Enkiball Agent 基座的严格技术导师，通过苏格拉底式追问和四级掌握度评估体系，确保学生从底层逻辑和工程角度真正掌握目标技术。

## 快速开始

```bash
# 确保基座可用
uv sync

# 启动 Teacher（默认 http://127.0.0.1:8765）
./teacher/start.sh

# 指定地址和端口
./teacher/start.sh 0.0.0.0 8080

# 停止
./teacher/stop.sh
```

## 配置

首次使用前需确保 `teacher/config/enki.json` 中已配置可用的 LLM provider。项目自带了一份配置（`teacher/config/`），默认使用 deepseek，如需其他模型直接修改 `teacher/config/enki.json` 和 `teacher/config/state.json` 即可。

## 使用流程

1. 浏览器打开 `http://127.0.0.1:8765`
2. 输入 `/learn <技术>` 设定学习目标
3. 教师会先进行诊断性提问，探测你的真实水平
4. 确认后生成个性化课程，逐级通关
5. L4 Capstone 课题通过即毕业

## 掌握度体系

| 等级 | 名称 | 要求 |
|------|------|------|
| L1 | 精准记忆 | 精确回忆概念的所有边界条件和细节 |
| L2 | 深层理解 | 解释设计原理、因果关系和 trade-off |
| L3 | 工程应用与分析 | 解决非平凡工程问题，分析复杂故障根因 |
| L4 | 毕业课题 | 完成有实际工程意义的完整课题 |

每一级需要回答 3-5 个递增难度的专家级问题，全部通过才能解锁下一级。

## 命令

| 命令 | 说明 |
|------|------|
| `/learn <主题>` | 设定或修改学习目标 |
| `/progress` | 查看当前学习进度 |
| `/assess` | 请求进行等级评估 |
| `/yield` | 认输，请求完整解答（标记失败） |
| `/code` | 进入代码评审模式 |
| `/reset` | 重置对话 |

## 课程协商

学习目标和课程计划并非一成不变。学生可以在任何时候对课程提出修改意见，教师会基于专家判断决定是否采纳。修改后课程通过 `set_curriculum` 工具自动持久化。

## 目录结构

```
teacher/
├── start.sh                   # 启动脚本
├── stop.sh                    # 停机脚本
├── requirements.txt           # 运行时依赖
├── config/                    # 独立配置
│   ├── enki.json              # Provider / MCP 配置
│   └── state.json             # 当前 provider / model
├── main.py                    # FastAPI 服务入口
├── teacher_core.py            # TeacherMaster 编排核心
├── prompts/
│   ├── orchestrator.txt       # 教师系统 prompt
│   └── assessor.txt           # 评估考官 prompt
├── models/
│   └── __init__.py            # 数据模型
├── web/static/                # Web 前端
│   ├── index.html
│   ├── style.css
│   └── app.js
└── data/                      # 运行时数据（自动创建）
    ├── state.json             # 学习状态
    ├── profiles/
    ├── curricula/
    └── sessions/
```

## 设计决策

- **不修改基座核心逻辑**：Teacher 完全通过 Enkiball 的 `register_tool` 和 `system_prompt` 机制实现，基座仅新增 `extra_body` 透传（无破坏性）
- **单 Agent 多角色**：通过 system prompt 在同一 Agent 内扮演教师/考官/课程设计者，避免多 Agent 编排的复杂度和上下文开销
- **独立配置**：Teacher 拥有独立的 `config/` 目录，与基座 CLI 互不干扰
