# MAF-Coder 主方案与 Phase D 执行手册

> **文档用途**：项目整体状态、路线图、Phase D 任务包；交给 Composer / Claude Code 直接写代码。  
> **仓库状态基准**：`190e7fe` — Phase C complete，**294 tests green**，working tree clean。  
> **配套文档**：Smart Router 融合见 `docs/PILOTDECK_SMART_ROUTER_FUSION.md`。  
> **规范来源**：`MAF-Coder_v2_Build_Plan.md`、`AGENT_TOOLS_SPEC.md §11/§17`、`WORKED_EXAMPLE.md`、`agent_team_soul_v3.1.md`。

---

## 1. 项目是什么

**MAF-Coder** 是用于 **自主 Rust coding mission** 的多 Agent Python 框架（编排器、Worker、Validator、黑板、沙箱）。  
开发本仓库时写的是 **Python**；目标代码库是 **Rust**（由 sandbox 内 agent 操作）。

---

## 2. 当前实态（Re-review 2026-05）

### 2.1 已完成

| Phase | 交付 | Commit 线索 |
|-------|------|-------------|
| A | Schemas, ModelRouter, ArtifactStore, EventLog, prompts×3, smoke | `81cce43` … |
| B | BaseAgent, Sandbox, Coder, Review, Orchestrator, Scheduler, MissionDriver, CLI | `0ff2b86` … `4d3a709` |
| C | Research, Security, Sanitizer, egress logging | `190e7fe` |

**代码规模**：`src/maf_coder/` 42 个 Python 模块；`tests/` 29 个测试模块。

### 2.2 验证门禁（提交前必过）

```bash
cd ~/Projects/maf-coder
source .venv/bin/activate   # Python 3.11+
pytest
ruff check src tests
ruff format --check src tests
mypy src/maf_coder
```

当前：**294 passed**。

### 2.3 关键缺口（阻塞完整 mission）

| 缺口 | 说明 |
|------|------|
| **BehaviorValidator 零实现** | 无 `behavior.py`、`behavior_tools.py`、`prompts/behavior_validator.md` |
| **MissionDriver 未注册** | `agent_factory` 无 `Role.BEHAVIOR_VALIDATOR` |
| **双 Validator 链无代码 gate** | Orchestrator prompt 有流程，Scheduler 不 enforce Review PASS → Behavior |
| **Smart Router 未实现** | 仅 role 级路由；见 D+ 文档 |
| **文档滞后** | `ARCHITECTURE.md §10`、`CLAUDE.md` 仍写 Phase B in progress |

### 2.4 已预埋（可直接用）

- `BehaviorVerdict` / `BehaviorObservation` — `schemas/verdict.py`
- `ArtifactStore.save/load_behavior_verdict`
- `BehaviorProbeSpec` + `project_profiler._default_probe_for()`
- `Role.BEHAVIOR_VALIDATOR` + `droid_whispering.yaml` behavior_validator 段
- `WORKED_EXAMPLE.md` t5 task 模板
- `AGENT_TOOLS_SPEC.md §11` 工具签名

---

## 3. 硬 Invariant（禁止破坏）

每项均有测试；禁用测试需显式讨论。

1. **`Handoff.triggers_second_pass`** — 仅当 `incomplete`、`issues_discovered`、`deviations_from_plan` 全空时为 True。  
2. **`ModelRouter` 异-provider** — `review_validator` / `adversarial_subagent` 不得与 `coder_provider_in_use` 同 provider。  
3. **`validation_contract` write-once** — 除非 `allow_overwrite=True`（Human Gate）。  
4. **`ArtifactStore` path safety** — 禁止逃出 `mission_dir`。  
5. **SDK `@function_tool`** — 工厂期用 identity（`agents/_sdk.py`）；真实 wrap 仅在 `BaseAgent._execute_sdk`（`wrap_for_sdk`）。  
6. **Pydantic** — 所有模型 `ConfigDict(extra="forbid")`（router 的 RoleConfig 等 3 处例外勿扩散）。

---

## 4. 目标架构（Phase D 完成后）

```text
Mission Start → project_profiler → plan + contract (LOCK) + tasks DAG
  Per milestone:
    Research ∥ Security  (parallel)
    Coder              (serial, slot=1)
    ReviewValidator    (after Coder)
    if review PASS:
      BehaviorValidator  (after Review)
    if both PASS → checkpoint
```

**Validator 顺序**（soul.md + ARCHITECTURE.md）：

- ReviewValidator：静态 gate（cargo + contract 断言覆盖）  
- BehaviorValidator：**仅** Review PASS 后；headless 探针（5 策略）  
- Security：可与 Coder 并行，不替代上述顺序  

**WORKED_EXAMPLE 参考**：`WORKED_EXAMPLE.md` — axum `/version` endpoint，task t4=review，t5=behavior。

---

## 5. 路线图

| Phase | 内容 | 优先级 |
|-------|------|--------|
| **D** | BehaviorValidator + 双 Validator 链 | **P0 — 现在** |
| **D+** | Smart Router（TierRouter，PilotDeck 融合） | P1，D merged 后 |
| **E** | Checkpoint, Status Report, Budget, Stuck, Resume | P2 |
| **F** | 跨 mission memory | P3 |
| **G** | 7 天真实 mission、wasm 完整探针 | P4 |

**AGENT_TOOLS_SPEC §17 顺序**：Step 14 = BehaviorValidator；Step 15 = Phase E；Step 16 = memory。

---

## 6. Phase D — 详细规格

### 6.1 Build Plan 工作项映射

| 项 | 内容 |
|----|------|
| D1 | 探针策略接口 + sandbox runner + 超时 + 证据保存 |
| D2 | 5 类探针：cli / backend_service / library / embedded / wasm（wasm 最小版） |
| D3 | Validator 冲突仲裁 |
| D4 | Contract `behavior_probe` 断言与 assertion_id 引用 |

### 6.2 探针策略（profiler 已映射）

| strategy | 项目类型 |
|----------|----------|
| `cli_assert_cmd_probe` | CLI, MIXED |
| `backend_service_health_probe` | BACKEND_SERVICE |
| `library_example_probe` | LIBRARY |
| `embedded_host_test_probe` | EMBEDDED |
| `wasm_node_probe` | WASM（D 阶段：`cargo build --target wasm32-unknown-unknown` 等最小集） |

配置来源：`project_profile.yaml` → `behavior_probe.{strategy,start_command,ready_check,endpoints_to_probe,timeout_sec}`。

### 6.3 BehaviorValidator 工具（AGENT_TOOLS_SPEC §11）

| Tool | 说明 |
|------|------|
| `start_service(command, ready_check, timeout_sec=300)` | 长驻服务 + ready 检测 |
| `stop_service(service_id)` | 清理 |
| `probe_http(url, method, body, expected_status)` | sandbox 内 curl（localhost，**不走** `check_network_allowed` SSRF 路径） |
| `probe_cli(binary, args, stdin, expected_exit_code)` | CLI 探针 |
| `save_behavior_evidence(task_id, name, content)` | `behavior_evidence/<task_id>/` |
| `save_behavior_verdict(task_id, result, probe_strategy, observations, evidence_path, failure_reason?)` | `verdicts/<task_id>.behavior.json` |

可选（WORKED_EXAMPLE 要求 `behavior_trace/t5.md`）：

- `save_behavior_trace(task_id, markdown)` → `behavior_trace/<task_id>.md`（§11 未列，建议在 D2 增加）

### 6.4 Validator 冲突仲裁（D3）

| Review | Behavior | 动作 |
|--------|----------|------|
| PASS | FAIL | Orchestrator 重规划（implementation path issue） |
| FAIL | — | 不 dispatch Behavior |
| FAIL | PASS | **Human Gate**（异常） |
| PASS | PASS | milestone checkpoint 候选 |

实现：`orchestrator/validator_chain.py` 或 `dispatch_task` 内 helper + `escalate_to_human_gate`。

### 6.5 Phase D 退出门槛（Build Plan）

- [ ] 真实 HTTP service：加 endpoint + Behavior 探针确认  
- [ ] 真实 CLI：加子命令 + stdout 探针  
- [ ] 真实 library：example + doc test 探针  
- [ ] Behavior 在 ≥2 case 捕获 Review 未捕获的逻辑 bug（需 live mission，代码侧先保证能力）  
- [ ] 探针失败时 `behavior_evidence/` 证据完整  

**单元测试门槛**（Composer 必须达到）：

- [ ] cli / backend / library 三类 probe 有 sandbox 单测  
- [ ] 双 validator gate 有 test  
- [ ] behavior verdict round-trip（扩 `test_artifact_store.py`）

---

## 7. Phase D — PR 任务包（Composer 执行顺序）

### PR-D1：探针框架 + behavior_tools

**新建**：

```text
src/maf_coder/validators/
  __init__.py
  probes/
    __init__.py
    base.py       # ProbeStrategy ABC, ProbeResult
    cli.py
    backend.py
    library.py
    embedded.py
    wasm.py
    registry.py # strategy name → class

src/maf_coder/agents/tools/behavior_tools.py
tests/validators/test_probes.py
tests/agents/test_behavior_tools.py
```

**参照**：`review_tools.py`、`security_tools.py`、`apply_patch_in_fresh_worktree` 的 sandbox 相对路径约定。

**服务注册**：`build_behavior_tools` 内闭包 `services: dict` 供 start/stop；勿用 `/tmp` 绝对路径（LocalShellSandbox 会泄漏 host 路径）。

**权限**：每个 tool 调用 `check_tool_allowed(ctx.task.permission, tool_name)`。

---

### PR-D2：BehaviorValidatorAgent + Prompt

**新建**：

```text
src/maf_coder/agents/behavior.py
prompts/behavior_validator.md
tests/agents/test_behavior_agent.py
```

**模式**：同 `ReviewValidatorAgent` / `SecurityWorkerAgent`。

- `role = Role.BEHAVIOR_VALIDATOR`  
- `parse_output` → `BehaviorRunSummary(verdict_path=...)`  
- `build_first_user_message`：强调只读 + 探针 + 证据；列出 review verdict 须 PASS  

**更新**：`src/maf_coder/agents/__init__.py` exports。

---

### PR-D3：MissionDriver + Validator 链 gate

**修改**：

- `orchestrator/mission_driver.py` — `agent_factory[Role.BEHAVIOR_VALIDATOR] = lambda: behavior`  
- `agents/tools/orchestrator_tools.py` — `dispatch_task`：behavior_validator 须 `depends_on` 含 review task 且 `load_review_verdict(...).result == PASS`  
- 可选 `orchestrator/scheduler.py` — behavior task ready 前检查 upstream verdict  

**新建**：`tests/orchestrator/test_validator_chain.py`

---

### PR-D4：冲突仲裁

**新建/修改**：

- `orchestrator/validator_chain.py` — `resolve_dual_validator(review, behavior) -> ValidatorChainDecision`  
- Orchestrator tools 或文档化 stuck recovery 事件类型  

**测试**：table-driven cases（含 Review FAIL + Behavior PASS → human gate）。

---

### PR-D5：文档同步

- `ARCHITECTURE.md §10` — B/C complete，D in progress  
- `CLAUDE.md` — current phase 段落  
- 确认 `AGENTS.md` Phase 指针（若存在）  

---

## 8. 代码库地图（实现时快速定位）

```text
src/maf_coder/
├── agents/
│   ├── base.py              # BaseAgent.run, TaskContext, wrap_for_sdk
│   ├── review.py            # 模板：Validator agent
│   ├── security.py          # 模板：Worker + verdict save
│   ├── tools/
│   │   ├── review_tools.py
│   │   ├── security_tools.py
│   │   └── orchestrator_tools.py  # dispatch_task
├── orchestrator/
│   ├── mission_driver.py    # agent_factory — 待加 behavior
│   ├── scheduler.py         # BEHAVIOR_VALIDATOR slot=1
│   └── project_profiler.py  # behavior_probe 默认值
├── models/router.py         # 异-provider
├── blackboard/
│   ├── artifact_store.py    # save_behavior_verdict
│   └── event_log.py
├── schemas/verdict.py       # BehaviorVerdict
└── sandbox/client.py        # exec, write_file

prompts/                     # 缺 behavior_validator.md
config/droid_whispering.yaml # behavior_validator 已配置
tests/                       # 294 tests
```

---

## 9. Phase D+ — Smart Router（第二批）

**详见** `docs/PILOTDECK_SMART_ROUTER_FUSION.md`。

简要步骤：

1. `models/tier_router.py` + `smart_router:` yaml 段  
2. `ModelRouter.resolve_model(role, task=...)`  
3. `EventLog.log_route_decision`  
4. `.maf/rules/routing.md`  

**不在 D 阻塞项内。**

---

## 10. Composer / Claude Code 首条指令（复制即用）

```text
在 ~/Projects/maf-coder 执行 Phase D PR-D1：

阅读 docs/MAF_CODER_MASTER_PLAN.md §7 PR-D1 与 AGENT_TOOLS_SPEC.md §11。

实现：
- src/maf_coder/validators/probes/*（5 策略 + registry）
- src/maf_coder/agents/tools/behavior_tools.py（§11 全部工具）

参照 review_tools.py、security_tools.py；测试用 LocalShellSandbox。
遵守 docs/MAF_CODER_MASTER_PLAN.md §3 硬 invariant。

完成后：pytest && ruff check src tests && mypy src/maf_coder 全绿。
不要 commit，除非用户要求。
```

后续依次 PR-D2 → D5。

---

## 11. Claude Code 迁移清单

在主线程加载时建议：

1. 读 `docs/MAF_CODER_MASTER_PLAN.md`（本文件）— 执行顺序  
2. 读 `docs/PILOTDECK_SMART_ROUTER_FUSION.md` — D+ 设计  
3. 读 `AGENT_TOOLS_SPEC.md §11` — 工具签名  
4. 读 `WORKED_EXAMPLE.md` t4/t5 — 端到端形状  
5. 会话记忆：`~/.claude/projects/-Users-john-Projects-maf-coder/memory/MEMORY.md`（若存在）

**环境**：

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

**不要跳过 Phase D 做 Phase E/F/G。**

---

## 12. Git 状态说明

- 最新 commit：`190e7fe` feat: Phase C  
- 分支：`main`，working tree clean（撰写本文档时）  
- **无 git remote** — push 前需 `git remote add origin <url>`

---

## 13. 文档维护

| 文件 | 更新时机 |
|------|----------|
| 本文件 | Phase D/E 里程碑完成时 |
| PILOTDECK_SMART_ROUTER_FUSION.md | TierRouter 落地后 |
| `ARCHITECTURE.md §10` | 每 phase 结束 |
| `CLAUDE.md` / `AGENTS.md` | current phase 变更时 |

---

*Generated for handoff from Cursor → Claude Code. Baseline: 294 tests, Phase C complete.*
