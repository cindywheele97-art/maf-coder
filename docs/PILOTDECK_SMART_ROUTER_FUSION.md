# PilotDeck × MAF-Coder 融合方案

> **文档用途**：Smart Router、Prompt Engineering、Rules 体系与 MAF-Coder 的融合设计。  
> **仓库状态基准**：`190e7fe`（Phase C complete，294 tests green）。  
> **上游调研**：OpenBMB [PilotDeck](https://github.com/OpenBMB/PilotDeck)（TypeScript agent OS，THUNLP / OpenBMB）。  
> **建议实施阶段**：Phase D 核心 merged 之后的 **Phase D+**（不阻塞 BehaviorValidator）。

---

## 1. 两个系统的本质差异

| 维度 | PilotDeck | MAF-Coder |
|------|-----------|-----------|
| 编排单元 | WorkSpace + 交互 turn | Mission + Task DAG |
| 路由主轴 | **任务复杂度** → tier → model | **角色** → model + 异-provider 约束 |
| Prompt 形态 | 运行时多层拼装（PILOTDECK.md + rules + skills） | 静态 `prompts/<role>.md` + schema 契约 |
| Rules | `~/.pilotdeck/rules/*.md`、`.pilotdeck/rules/*.md` | `agent_team_soul_v3.1.md`（宪法）+ `AGENTS.md` + 权限层 |
| Sub-agent | turn 内 `agent` tool 动态 spawn | 规划期 `tasks.yaml` + Scheduler 串/并行 |
| 成本优化 | Judge 小模型 + main/sub 分工 | 角色级 primary/fallback（`droid_whispering.yaml`） |
| 审计 | `router/stats.jsonl`、mutation log | `EventLog` + `ArtifactStore` |

**结论**：PilotDeck 的 Smart Router 补的是 **「复杂度感知、成本感知」** 层；MAF-Coder 已有的是 **「组织/安全感知」** 层（角色边界、validator 异-provider、write-once contract）。融合方向是 **双轴路由**，不是二选一。

---

## 2. PilotDeck Smart Router 结构

核心代码：`PilotDeck/src/router/`（TypeScript）。

### 2.1 决策流水线

```
decide(input)
  ├─ SessionRouterStore sticky（tier / model / orchestrating）
  ├─ custom router extension（可选）
  ├─ decideScenario（explicit | subagent | default）
  ├─ tokenSaver（若启用）
  │    ├─ sticky hit → 复用 tier
  │    └─ classifyAndRoute：Judge LLM → parse <tier>NAME</tier>
  ├─ autoOrchestrate（complex tier 等）
  │    ├─ 注入 orchestration system-reminder
  │    ├─ 工具白名单（agent, read_file, grep, glob, read_skill）
  │    └─ slimSystemPrompt
  └─ execute：fallback chain + zero-usage retry + content-aware 不回退
```

### 2.2 tokenSaver（最值得移植）

**Judge prompt 模式**（`generateJudgePrompt.ts`）：

- 输入：最后一条 user message + tier 描述 + `rules[]` + 可选 `previousTier`
- 输出：仅 `<tier>NAME</tier>`
- 失败：回退 `defaultTier`（默认 `medium`）

**四档 tier 语义**（PinchBench 校准）：

| Tier | 含义 | 典型用途 |
|------|------|----------|
| `simple` | 问候、确认、单步 Q&A | 最便宜模型 |
| `medium` | 单次 tool、短代码 | 中等模型 |
| `complex` | **必须 sub-agent 编排** | 旗舰 orchestrator |
| `reasoning` | 难但 **单 agent 可完成** | 强模型，不 spawn |

**关键创新**：`complex` vs `reasoning` 分离，避免「什么都 spawn sub-agent」。

**Continuation sticky**：短消息（「继续」「ok」「好的」）继承 `previousTier`，避免误判为 `simple`。

### 2.3 autoOrchestrate（与 MAF Scheduler 的关系）

触发 `complex` 时：注入 orchestrator 指令、工具白名单、可选 slim system prompt、强制便宜 `subagentModel`。

- **MAF 不需要照搬 turn 级 autoOrchestrate** — Scheduler + `tasks.yaml` 已是 plan 级编排。
- **可借模式**：编排者减工具、执行者全工具（Orchestrator task 的 `permission.allowed_tools`）。

### 2.4 配置面（`pilotdeck.yaml` → `router:`）

```yaml
router:
  tokenSaver:
    enabled: true
    judge: provider/cheap-model
    defaultTier: medium
    judgeTimeoutMs: 15000
    tiers:
      simple:   { model: ..., description: "..." }
      medium:   { model: ..., description: "..." }
      complex:  { model: ..., description: "..." }
      reasoning:{ model: ..., description: "..." }
    rules: [...]   # 自然语言，注入 judge prompt
  autoOrchestrate:
    triggerTiers: [complex]
    allowedTools: [agent, read_file, ...]
  stats:
    baselineModel: provider/flagship
```

### 2.5 关键源文件（PilotDeck）

| 路径 | 说明 |
|------|------|
| `src/router/RouterRuntime.ts` | decide → mutate → execute |
| `src/router/config/schema.ts` | 配置类型与默认 tier 描述 |
| `src/router/tokenSaver/classifyAndRoute.ts` | Judge 分类 |
| `src/router/tokenSaver/generateJudgePrompt.ts` | Judge prompt 模板 |
| `src/router/orchestrate/applyOrchestration.ts` | Orchestrator 模式突变 |
| `src/router/session/SessionRouterStore.ts` | Sticky 状态 |
| `src/agent/loop/AgentLoop.ts` | 每 turn 调用 router |
| `src/context/instructions/InstructionDiscovery.ts` | 多层 PILOTDECK.md / rules 加载 |
| `scripts/bootstrap-pilotdeck-config.mjs` | 默认 router YAML 模板 |

---

## 3. MAF-Coder 现有路由

`src/maf_coder/models/router.py` — `ModelRouter`：

1. `role` → primary / fallback（`config/droid_whispering.yaml`）
2. 静态 `forbidden_providers`
3. 动态：**review_validator / adversarial_subagent ≠ coder provider**
4. LiteLLM `complete()` + 成本追踪

**缺口**：同一 role 内，无法按 task 复杂度选不同 model（例如 Coder 小 patch vs 跨 crate 重构）。

`behavior_validator` 已在 yaml 中配置，但 **BehaviorValidator agent 尚未实现**（见主方案文档）。

---

## 4. 融合架构：双轴 Smart Router

```
Task (role + goal + risk + criteria)
    → RoleRouter（droid_whispering roles）
    → TierRouter（PilotDeck tokenSaver 模式）
    → Constraints（异-provider / forbidden）
    → LiteLLM complete
    → EventLog: ROUTE_DECISION
```

### 4.1 何时跑 Judge？

| 场景 | Judge |
|------|-------|
| Orchestrator 规划 | 否（固定 Opus） |
| Coder Worker | **是** |
| Research Worker | **是** |
| ReviewValidator | 否（异-provider 硬约束） |
| adversarial_subagent | 否（永远 cheap） |
| BehaviorValidator | 可选 medium/reasoning |

Judge 输入建议为 **结构化 Task 摘要**，而非裸 user message：

```text
role: coder_worker
goal: ...
criteria_count: N
risk: ...
depends_on: [...]
patch_files_estimate: ...
```

### 4.2 配置扩展（`droid_whispering.yaml`）

```yaml
smart_router:
  enabled: true
  judge:
    model: google/gemini-2.5-flash
    temperature: 0
    max_tokens: 256
    timeout_ms: 15000
  default_tier: medium
  tiers:
    simple:   { model: anthropic/claude-sonnet-4-6, max_tokens: 8000 }
    medium:   { model: anthropic/claude-sonnet-4-6, max_tokens: 32000 }
    reasoning:{ model: anthropic/claude-opus-4-7,  max_tokens: 32000 }
  rules:
    - "Security audit with zero findings → simple"
    - "Cross-crate refactor or new public API → reasoning"
    - "Validator roles: never tier below medium"
  per_role:
    coder_worker: { enabled: true }
    research_worker: { enabled: true }
    review_validator: { enabled: false }
  sticky:
    enabled: true
    inherit_on: ["continue", "ok", "好的", "继续"]
  stats:
    baseline_model: anthropic/claude-opus-4-7
```

**解析顺序**：`tier.model` → 再套 RoleRouter 的 forbidden / 异-provider → 失败则 tier 内 fallback 或 role fallback。

### 4.3 `complex` tier 在 MAF 中的映射

**不要**在 SDK turn 内 spawn 任意 sub-agent（破坏 contract / handoff 审计）。

**要**：`complex` → Orchestrator **重规划信号** → `dispatch_task` 拆新 Task 进 DAG（Research ∥ Security → Coder → Review → Behavior）。

---

## 5. Prompt Engineering 融合

### 5.1 三层 Prompt 栈

| 层 | PilotDeck | MAF-Coder | 融合建议 |
|----|-----------|-----------|----------|
| 宪法 | 弱 | `agent_team_soul_v3.1.md` | 保持 MAF 宪法 |
| Harness rules | PILOTDECK.md + rules | AGENTS.md / CLAUDE.md | 项目 `.maf/rules/*.md` |
| Mission rules | `.pilotdeck/rules/` | 无 | `missions/<id>/rules/*.md` |
| Role prompt | PromptAssembler | `prompts/<role>.md` | 保持；注入 mission rules |
| Runtime patch | router orchestration | `build_first_user_message` | TierRouter mutation log |

### 5.2 建议目录布局

```text
maf-coder/
├── agent_team_soul_v3.1.md
├── AGENTS.md / CLAUDE.md
├── .maf/rules/
│   ├── rust.md
│   └── routing.md          # 与 smart_router.rules 单源
├── prompts/
└── missions/<id>/rules/    # 可选 mission override
```

**加载顺序**（后加载优先）：  
`soul` → `.maf/rules/*.md` → `prompts/<role>.md` → `missions/<id>/rules/*.md` → Task `build_first_user_message`

### 5.3 Judge 与 rules 打通

`.maf/rules/routing.md` 内容可编译进 `smart_router.rules[]`，避免两处维护。

### 5.4 闭环

1. Plan：`tasks.yaml` 写 `risk_level` / criteria → TierRouter 先验  
2. Run：Judge + routing rules → tier  
3. Audit：`EventLog.log_route_decision`  
4. Retro：对比 baseline 成本 → `mission_retro.md`

---

## 6. Rules 体系分工

| 规则类型 | 载体 | 执行 |
|----------|------|------|
| 组织边界 | soul.md §3 | 角色职责 |
| Provider 安全 | `ModelRouter._VALIDATOR_ROLES` | **代码硬约束** |
| 工具权限 | `Permission` + `permissions.py` | PreToolUse |
| Prompt 行为 | `prompts/*.md` | LLM 遵循 |
| 路由策略 | `smart_router.rules` | Judge LLM |
| 任务验收 | `validation_contract.yaml` | Validators |

**原则**：

- 代码 invariant（异-provider、path escape、handoff completeness）→ **永不交给 Judge**
- 成本/复杂度启发式 → TierRouter + `routing.md`
- 业务验收 → contract + validators

---

## 7. Phase D+ 落地步骤（给实现者）

| 步骤 | 交付物 | 测试 |
|------|--------|------|
| 1 | `schemas/routing.py`：`RouteDecision`, `TierName` | round-trip |
| 2 | `models/tier_router.py`：`classify_task()`, `parse_tier()` | mock judge |
| 3 | 扩展 `droid_whispering.yaml` + `ModelRouter.resolve_model(role, task)` | `test_tier_router.py` |
| 4 | `EventLog.log_route_decision()` | event_log test |
| 5 | `BaseAgent.run` 调用 resolve | `test_base_agent.py` |
| 6 | `.maf/rules/routing.md` + loader | 集成测试 |
| 7 | CLI stats（可选） | — |

**MVP（1–2 天）**：仅 Coder Worker tier judge + EventLog。

**Judge 模型建议**：与 `adversarial_subagent` 同级 cheap model，temperature=0，max_tokens=256。

---

## 8. 不建议照搬

1. Turn 级 autoOrchestrate **替换** MAF DAG  
2. PILOTDECK.md **替代** soul.md  
3. 整包 port TypeScript router（Python 复刻核心 ~200 行即可）  
4. `complex` = 任意 multi-step（必须保留「仅编排」语义）  
5. Judge 决定 validator provider（异-provider 必须 stay in code）

---

## 9. 一句话公式

> **MAF-Coder 的 Role Router 决定「谁来做、和谁不能同厂」；PilotDeck 的 Tier Router 决定「这个人用多大力气做」。**  
> Prompt 栈 = soul（宪法）+ rules（可演进）+ prompts（角色）+ contract（验收）；Smart Router 是 rules 里「成本/复杂度」部分的运行时执行器。

---

## 10. 参考链接

- PilotDeck 仓库：https://github.com/OpenBMB/PilotDeck  
- MAF 模型配置：`config/droid_whispering.yaml`  
- MAF 路由实现：`src/maf_coder/models/router.py`  
- 主路线图：`docs/MAF_CODER_MASTER_PLAN.md`
