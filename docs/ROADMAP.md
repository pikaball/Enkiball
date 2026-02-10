# Enkiball Roadmap

## Phase 0 (Done): CLI + LLM + MCP Foundation

- Interactive CLI with slash commands.
- Streaming chat output.
- Built-in local tools for LLM.
- MCP local and remote transport support.

## Phase 1: Core Runtime and Reliability

Goals:
- Stabilize transport/session behavior.
- Add runtime observability and regression tests.

Deliverables:
1. Unified logging and trace IDs (request, tool-call, MCP request).
2. Retry/backoff and health-check for MCP clients.
3. Pytest suite for:
   - tool-call loop
   - MCP stdio/jsonl/content-length
   - streamable-http response/session handling
4. Config schema validation and migration versioning.

## Phase 2: Task Controller (Main Architecture Start)

Goals:
- Implement persistent task system from README design.

Deliverables:
1. Storage-backed Task model:
   - `task_id`, `parent`, `target`, `context`, `subtasks`, `process`, `status`.
2. Task controller API and tool set:
   - field-level mutation tools
   - read/list/search tools
3. Session binding to one main task and task switching.

## Phase 3: Main Agent + Subagent Runtime

Goals:
- Build multi-agent execution model around task graph.

Deliverables:
1. Subagent spawn/stop/resume primitives.
2. Main-agent orchestration loop.
3. Subagent permission boundary (no nested spawn).
4. Conversation/history compaction strategies.

## Phase 4: Supervisor Loop

Goals:
- Add asynchronous supervision and recovery policies.

Deliverables:
1. Periodic evaluation over active subtasks.
2. Deviation detection and corrective intervention.
3. Failure-attempt accounting and suspend policy.
4. Main task progress auto-update.

## Phase 5: Security Research Productization

Goals:
- Make Enkiball usable for long-running security workflows.

Deliverables:
1. Profiles/policies for recon, vuln validation, reporting.
2. Evidence pipeline (command output, file artifact linking, provenance).
3. Human approval checkpoints for risky operations.
4. Session replay and report generation.

## Phase 6: Production Readiness

Goals:
- Ship stable releases for repeated real-world usage.

Deliverables:
1. Packaging and release channels.
2. Telemetry/metrics dashboard.
3. Compatibility matrix (models, MCP servers, OS).
4. Hardening and threat-model-based audits.
