# MAF-Coder v2 Build Plan

> 质量优先，时间不限。本计划按"成熟度阶段"组织，不按时间组织。每个阶段有独立的入口准则、关键工作项、退出门槛。**只有退出门槛全部通过才能进入下一阶段。**
>
> 与 v1 (2 周 demo) 的根本区别：v1 试图在 2 周内做完所有最小功能；v2 承认多天 Rust 生产级框架不是 2 周能做对的事，按阶段把质量做厚。
>
> 配套文件：`agent_team_soul_v3.md`（宪法）、`config/rust_sandbox.dockerfile`（沙箱）、`config/droid_whispering.yaml`（模型路由）。

---

## 决策日志（v3 之前所有锁定决策）

为方便后续 onboard 新人，所有锁定决策在此一表汇总：

| # | 维度 | 决策 | 来源 |
|---|---|---|---|
| 1 | 编排框架 | OpenAI Agents SDK + LiteLLM | Round 1 Q3（多供应商）|
| 2 | 任务类型 | bugfix/feature/refactor 通用 | Round 1 Q1 |
| 3 | 质量门槛 | Factory 16-day 级生产 | Round 2 Q1 |
| 4 | 模型策略 | 每角色独立，Validator 与 Coder 异供应商 | Round 1 Q3 + Round 2 |
| 5 | 编排角色 | 单一 Orchestrator | 我的决定（v2 优化点 1）|
| 6 | Worker 角色 | Research + Coder + Security | 我的决定（Round 2 Q3）|
| 7 | Validator 角色 | ReviewValidator + BehaviorValidator(headless) | 我的决定（Round 2 Q2）|
| 8 | Validation Contract | 一等工件，规划阶段锁定 | v2 优化点 2 |
| 9 | Handoff 文档 | 强制结构化 | v2 优化点 4 |
| 10 | 语言生态 | Rust 通用框架 | Round 2 Q4 + Round 3 Q2 |
| 11 | Sandbox | 本地 Docker + 持久化缓存 | Round 3 Q5 |
| 12 | 联网 | 完全开放 + 内容净化层 | Round 3 Q3 |
| 13 | HITL | 4-8h 状态同步 + 高风险审批 | Round 3 Q4 |
| 14 | 预算 | 不设硬上限，阈值提醒 | Round 4 Q1 |
| 15 | 跨 mission 记忆 | 项目内 + 全局 Rust 教训 | Round 4 Q2 |
| 16 | 交付方式 | 推分支 + 开 PR | Round 4 Q3 |
| 17 | 卡住恢复 | 三级分诊：低自动重试，高问人 | Round 4 Q4 |

---

## 总体阶段图

```
Phase A: 地基（基础设施 + 单 agent 跑通）
   ↓ 退出：单一 Coder 能在 sandbox 跑通一个最小 Rust bugfix，工件齐全
Phase B: 单 Coder + ReviewValidator
   ↓ 退出：5 个真实 Rust 小项目跑通 bugfix/small feature，首次通过率 ≥ 50%
Phase C: 并行 Worker 矩阵（Research + Coder + Security）
   ↓ 退出：中等 Rust 项目跑通 refactor，所有 worker 并行有序，事件日志完整
Phase D: BehaviorValidator + 双 Validator 链
   ↓ 退出：真实 Rust service 跑通 "add endpoint" 含端到端行为验证
Phase E: 多天能力（Checkpoint + Status + Stuck Recovery + 预算）
   ↓ 退出：跑通至少一个 48 小时 mission，含 1 次 checkpoint 回滚 + 1 次状态同步 + 1 次用户指令调整
Phase F: 跨 mission 记忆 + PR 工作流
   ↓ 退出：第二个 mission 能复用第一个 mission 的教训；GitHub PR 自动生成且 Review-friendly
Phase G: 真实场景验证（多项目 + 多天）
   ↓ 退出：你愿意把它推荐给别人用
```

每阶段时长不固定。**质量到位才进下一个阶段**，进度滑动只通过推迟，不通过削减。

---

## Phase A · 地基

**目标**：把 sandbox、配置、消息层、工件层、最简单的 Orchestrator + Coder 跑通。这阶段不追求质量，追求"整套机制能运转"。

### 入口准则
- 决策日志 17 项全部确认（已完成）
- 拿到必要的 API keys（Anthropic / OpenAI / Google）

### 关键工作项

**A1. 项目骨架**
- 初始化 Python 仓库（`uv init` 或 `poetry init`）
- 依赖：`openai-agents`、`litellm`、`pydantic`、`pyyaml`、`gitpython`、`docker`、`rich`、`pytest`
- 仓库布局按 v1 计划但扩展（见 §仓库结构）

**A2. Rust Sandbox 镜像**
- 基于 `rust:1-bookworm-slim` 构建（Dockerfile 在 `config/rust_sandbox.dockerfile`）
- 装齐：rustup（stable + nightly）+ clippy + rustfmt + rust-analyzer + cargo-audit + cargo-deny + cargo-geiger + cargo-expand + cargo-machete + cargo-outdated + cargo-nextest + sccache
- C 工具链：gcc / clang / lld / mold
- protoc / wasm-pack / wasm-bindgen-cli
- 工具：git / gh / glab / ripgrep / fd / jq / gitleaks / trufflehog
- 持久化 volume：`/cargo-cache`（CARGO_HOME）、`/target-cache`（共享 target/）、`/sccache`

**A3. 配置文件**
- `config/droid_whispering.yaml`（每角色模型路由）
- `config/permissions/*.yaml`（每角色权限边界）
- `agent_team_soul_v3.md` 作为系统提示派生源

**A4. Pydantic Schema 层**
- `Message`、`Task`、`Handoff`、`ValidationContract`、`ProjectProfile`、`Verdict`、`StatusReport`、`Checkpoint`
- 全部带 JSON Schema 导出 + 反序列化验证

**A5. 工件黑板**
- `missions/<mission_id>/` 目录布局规范
- `ArtifactStore`：读写抽象，所有 agent 经此访问工件
- `EventLog`：append-only jsonl，记录每条 agent action / tool call / cost

**A6. 模型路由器**
- 基于 `droid_whispering.yaml` 的 `ModelRouter`
- 三供应商 smoke test：能成功 ping GPT-5 / Claude Sonnet 4.6 / Gemini 2.5 Pro 三个供应商，工具调用稳定

**A7. 最小 Orchestrator + 最小 Coder**
- 跳过 Validation Contract、跳过 Research、跳过 Security
- 只为验证"流水线能动"
- 给一个 hello-world Rust crate（有一个失败 test），让 Coder 修，最后能产出 patch + handoff

### 退出门槛

- [ ] Docker 镜像构建成功，容器内 `cargo build --release` 一个 hello-world crate < 30s（含缓存）
- [ ] 三供应商 smoke test 全过：每个供应商至少 5 次 tool call 100% 成功
- [ ] `maf-coder mission "<目标>"` 命令能跑完一个 hello-world bugfix 案例
- [ ] 工件齐全（plan.md / patch.diff / handoff.md / events.jsonl）
- [ ] `pytest tests/` 全过（Schema 层 + Router + ArtifactStore 单测）

### 风险与缓解
- **Docker 镜像膨胀**：装齐 Rust 工具链 + C 工具链 + 各 cargo-*，预计 3-5 GB。用多阶段构建 + apt 清理 mitigate
- **LiteLLM 在 Gemini tool calling 上不稳定**：A6 阶段直接验证；不稳就先把 Gemini 排除出 v1，用 GPT-5 + Claude 双供应商

---

## Phase B · 单 Coder + ReviewValidator

**目标**：把"实现+静态验证"环路做对。引入 Validation Contract、强制 handoff、对抗 sub-agent。

### 入口准则
- Phase A 退出门槛全部通过

### 关键工作项

**B1. Validation Contract 子系统**
- Orchestrator 在规划阶段产出 `validation_contract.yaml`
- Locked 标志位 + Coder/Validator 端的强制读校验
- Contract 与 task 的 assertion ID 双向引用

**B2. ReviewValidator 完整实现**
- Cargo 全套 gate：build / test / clippy / fmt / nextest
- 失败时输出 precise reason（不是"测试挂了"，是"tests/foo.rs::test_bar 第 12 行 assertion failed: expected 5 got 4"）
- 与 Coder 强制异供应商（在 Router 层强约束）

**B3. 对抗 sub-agent**
- 一次性 sub-agent：上下文只含 patch + contract + research（无 Coder 思路）
- 任务：判断 patch 是否覆盖 contract 全部断言；额外生成 proptest 草稿
- 草稿归档到 `adversarial_tests/`，不强制运行（v1 阶段）

**B4. 重试 / 升级逻辑**
- 单 task 失败：自动 1 次重试（不同 temperature）
- 同 task 连续 2 次失败：升级 Orchestrator 重规划
- 同 milestone 累计 3 次失败：升级 Human Gate
- 全程 Event Log 记录

**B5. Project Profiler v1**
- 读 Cargo.toml + workspace 检测 + rust-toolchain 检测
- 输出 `project_profile.yaml`（仅 lib/CLI 两种 profile，service/embedded/wasm 推到 Phase D）

**B6. CLI 改进**
- `maf-coder mission "<goal>" --repo <path>`：以指定 git repo 为目标
- `maf-coder status <mission_id>`：当前状态
- `maf-coder logs <mission_id> --tail`：实时事件流

### 退出门槛（关键 quality bar）

- [ ] 5 个真实开源 Rust 小项目（公开 crates，500-5000 LOC）跑通 bugfix 任务
- [ ] 5 个真实开源 Rust 小项目跑通 small-feature 任务
- [ ] **首次通过率 ≥ 50%**（10 次重复的中位数；任务 1-2 类）
- [ ] **最终通过率 ≥ 85%**（含重试）
- [ ] 失败案例的 ReviewValidator 失败原因都准确（人工抽 10 个 case，准确率 ≥ 9/10）
- [ ] 对抗 sub-agent 生成的草稿在至少 30% 的 case 里能指出 Coder 测试遗漏的边界

### 风险与缓解
- **Coder 反复输出格式不符合 patch.diff 标准**：在 Coder prompt 中加 few-shot；在 Coder 输出后用 git 强校验
- **Contract 起草质量不稳定**：Orchestrator prompt 重点强化"在没有具体实现思路前就要写出可验证断言"

---

## Phase C · 并行 Worker 矩阵

**目标**：激活 Research + Security Worker，让它们与 Coder 并行有序运转。引入外部内容净化层。

### 入口准则
- Phase B 退出门槛全部通过

### 关键工作项

**C1. Research Worker**
- 只读权限 profile
- 工具：read / glob / grep / cat / cargo metadata / cargo tree / cargo doc --no-deps / cargo expand
- 外部 HTTP GET（经净化层）
- 输出 `research_notes/<topic>.md` + `code_map/<module>.md` + `dependency_brief.md` + `workspace_overview.md`

**C2. 外部内容净化层**
- HTML → Markdown 标准化
- 指令注入扫描器（模式列表 + 启发式）
- `<external>` 标签包裹
- 下游使用警示文本注入

**C3. Security Worker**
- 只读权限
- 工具链：cargo-audit / cargo-deny / cargo-geiger / gitleaks / trufflehog
- 输出 `security_audit.json`（机器可读 + 严重度）+ `security_notes.md`
- 与 Coder 并行触发

**C4. Scheduler 真并行**
- DAG 调度器：根据 task 间依赖 + 读写约束（写串行 / 读并行）决定并发度
- Lease + heartbeat：长任务超时检测
- 资源 token 池：限制同时跑的 LLM 调用数

**C5. 联网治理**
- Egress logger：每次外部请求写 `egress.jsonl`
- 域名速率限制
- 流量预算（每 mission 1000 次默认）
- 黑名单：可执行下载、可疑域名

**C6. Project Profiler v2**
- 扩展支持 backend_service / embedded / wasm 类型识别
- Feature matrix 完整探测
- toolchain 探测（rust-toolchain.toml）

### 退出门槛

- [ ] 在 3 个中等 Rust 项目（5000-20000 LOC，workspace 结构）跑通 refactor 类任务
- [ ] Research / Coder / Security 三 Worker 在同一 mission 内并行运转：事件日志显示 Research 与 Coder 时间重叠 ≥ 30%
- [ ] 净化层抓取 50 个真实外部页面，注入扫描器至少识别 90% 的已知注入模式（自建测试集）
- [ ] Security Worker 在 5 个引入新依赖的 case 中至少识别 1 个真实风险（如 yanked crate / deprecated）
- [ ] 多 Worker 并行不出现工件写冲突（黑板锁机制有效）

### 风险与缓解
- **Research Worker 抓取过多无关内容污染上下文**：在 prompt 中强约束"每条 note 不超过 200 字 + 引用 URL"
- **并行下 LLM API rate limit**：在 Router 层加 token bucket
- **依赖冲突真发现率低**：可以接受，Security 主要价值是兜底而非主动发现

---

## Phase D · BehaviorValidator + 双 Validator 链

**目标**：把"行为对不对"做实。引入按项目类型分派的 headless 探针。

### 入口准则
- Phase C 退出门槛全部通过

### 关键工作项

**D1. BehaviorValidator 框架**
- 探针策略接口
- 探针 runner（在 sandbox 内）
- 超时 + 证据保存机制
- 与 ReviewValidator 顺序：Review PASS 后才跑 Behavior

**D2. 5 类探针实现**
- `cli_assert_cmd_probe`（CLI 类）
- `backend_service_health_probe`（HTTP 服务）
- `library_example_probe`（库）
- `embedded_host_test_probe`（嵌入式）
- `wasm_node_probe`（WebAssembly）

**D3. Validator 冲突仲裁**
- ReviewValidator PASS + BehaviorValidator FAIL 的处置：触发"实现路径有问题"信号 → Orchestrator 重规划
- 反过来（Review FAIL + Behavior PASS）几乎不可能；若出现，强制升级 Human Gate

**D4. Contract 断言扩展**
- 单条断言可声明 verification_method = behavior_probe + 具体 probe target
- 探针失败时引用具体断言 ID

### 退出门槛

- [ ] 1 个真实 Rust HTTP service 项目跑通"添加新 endpoint"任务，Behavior 探针确认端点工作
- [ ] 1 个真实 Rust CLI 工具跑通"添加新子命令"任务，CLI 探针验证 stdout 模式
- [ ] 1 个真实 Rust library 跑通"添加新公开 API"任务，example + doc test 探针通过
- [ ] BehaviorValidator 在至少 2 个 case 里捕获到 ReviewValidator 没捕获的逻辑 bug（这是它存在的意义）
- [ ] 探针失败时证据完整（stdout/stderr/logs 都在 `behavior_evidence/`）

### 风险与缓解
- **Service 项目启动配置复杂（环境变量、数据库连接）**：探针支持从 `project_profile.yaml` 读启动参数；不支持的情况降级到只跑编译验证
- **WebAssembly 探针环境复杂**：D 阶段只做最小版本（cargo build --target wasm32-unknown-unknown + wasm-pack build），完整 node 探针推到 Phase G

---

## Phase E · 多天能力

**目标**：把单次 4-8 小时任务能力扩展到多天。Checkpoint + Status Report + Stuck Recovery + 预算守门四大基础设施。

### 入口准则
- Phase D 退出门槛全部通过

### 关键工作项

**E1. Checkpoint 系统**
- 每个 milestone 完成：git tag + Docker container commit + artifact archive + mission_state.json 更新
- 命令：`maf-coder resume <mission_id>`、`--from m<n>`、`rollback --to m<n>`
- Sandbox snapshot 存储管理（旧 snapshot 自动 GC）

**E2. Status Report 协议**
- Orchestrator 内置 4-8h 定时器（可配）
- Status Report 模板按 §5.2 严格执行
- 推送渠道适配器：webhook / 邮件 / desktop notification / CLI tail / SMS（按用户偏好开启子集）

**E3. User Messages 反向通道**
- `user_messages/<mission_id>/` 目录监听
- 普通指令：milestone 边界检查
- `!urgent` 前缀：下一 task 边界立刻检查
- 处理后归档到 `processed_messages/`

**E4. Stuck Recovery 三级分诊**
- 触发条件 → 风险等级映射器
- 低风险自动重试 / 中风险 Orchestrator 重规划 / 高风险升级 Human Gate
- 全程事件日志，retro 可分析

**E5. 预算守门**
- 实时 cost tracker（per agent / per role / per mission）
- 阈值触发器（50% 标注 / 80% 提醒 + 谨慎模式 / 100% 暂停 / 150% 强制暂停）
- 谨慎模式：关并行 + 降模型 + 减重试

**E6. 长任务韧性测试**
- 模拟外部依赖中断（crates.io 5xx）的重试逻辑
- 模拟 LLM 供应商 rate limit 的降级路径
- 模拟 sandbox 资源耗尽（target/ 占满）的清理

### 退出门槛

- [ ] 跑通至少一个 48 小时连续 mission（真实 Rust 项目，多 milestone 任务）
- [ ] 该 mission 期间至少经历 1 次 checkpoint 回滚后重试成功
- [ ] 该 mission 期间至少经历 1 次 Status Report 推送
- [ ] 该 mission 期间至少经历 1 次用户通过 `user_messages/` 注入指令的成功调整
- [ ] 该 mission 期间至少触发 1 次预算阈值警告（80%），系统正确切换谨慎模式
- [ ] 第二个 48 小时 mission 在不同项目上跑通

### 风险与缓解
- **48 小时 mission 期间 Docker 容器异常退出**：Docker container restart policy + 状态文件持久化在 host volume + resume 命令自动恢复
- **target/ 占满磁盘**：sccache + 定期 cargo clean 策略 + per-mission target 隔离
- **LLM 长会话 token 累积爆炸**：分阶段总结 + 阶段间用 handoff 重启上下文

---

## Phase F · 跨 mission 记忆 + PR 工作流

**目标**：把单次任务能力变成持续复利的能力。每个 mission 的经验沉淀，下个 mission 可受益。

### 入口准则
- Phase E 退出门槛全部通过

### 关键工作项

**F1. 项目内记忆库**
- 每个 git repo 一个 `.maf-coder/memory.db`（SQLite）+ `.maf-coder/memory.index/`（向量索引，lancedb 或 chromadb）
- 存：mission_retro / contract / handoff / profile 历史
- Embeddings：用便宜模型（如 OpenAI text-embedding-3-small）

**F2. Retrieval 集成点**
- Orchestrator 规划阶段：检索相似目标的历史 plan + retro
- Research Worker 开始时：检索相似主题的历史 notes
- Coder Worker 开始时：检索该模块的历史 handoff
- Retrieval 结果带置信度 + 时间衰减 + 来源 mission ID

**F3. 防忆污染**
- Prompt 中显式标记为 `<historical_lesson confidence="..." age_days="..." mission_id="...">`
- 与当前情境冲突时新 mission 不受拘束
- 手动 quarantine 接口

**F4. 全局教训库**
- `~/.maf-coder/global_lessons.db`
- 只接收 retro 中显式标注 `global_lesson: yes` 的条目
- 50 条触发一次语义聚合去重

**F5. PR Workflow**
- GitHub / GitLab 集成：`gh` 与 `glab` CLI 包装
- PR 描述自动生成（按 §9.2 模板）
- 自动 link 到 mission 工件目录
- PR 创建前 gitleaks 终扫

**F6. mission_retro.md 模板**
- 强制结构化：what worked / what failed / surprises / global_lessons
- Orchestrator 在 mission 结束时生成
- 用户可以人工编辑后再入记忆库

### 退出门槛

- [ ] 在同一项目上跑两个相关 mission，第二个 mission 的 plan 能看到对第一个 mission retro 的引用
- [ ] 全局教训库累计至少 20 条跨项目可复用的 Rust 教训
- [ ] 至少 1 个 PR 在 GitHub 上成功创建，描述完整，人类 reviewer 反馈"信息够用"
- [ ] Retrieval 结果在 5 个抽查 case 里至少 3 个对当前 mission 有帮助（人工评估）
- [ ] 防污染机制经测试：故意构造冲突案例，新 mission 不被历史误导

### 风险与缓解
- **向量检索召回质量差**：先用最简的 embedding + 关键词混合检索，效果不行再升级
- **全局教训积累过慢**：人工 seed 一批高质量教训（你自己写过的 Rust 经验帖）
- **PR 描述自动生成质量参差**：用 Orchestrator 级模型，加 few-shot

---

## Phase G · 真实场景验证

**目标**：在真实生产场景下跑得起来。多项目、多任务、多天。

### 入口准则
- Phase F 退出门槛全部通过

### 关键工作项

**G1. 真实多天 mission**
- 在你手头真实 Rust 项目上跑至少一个 7-day mission
- 任务复杂度：跨 5+ crate 的中等 refactor 或新功能

**G2. 多项目轮换**
- 同一周内在至少 3 种项目类型上跑 mission（lib / CLI / service）
- 验证通用性

**G3. 健康指标基线**
- 一套你后续可以对自己说"v3.1 / v3.2 比 v3.0 进步"的指标
- 包括：首次通过率 / 最终通过率 / 平均成本 / 平均 wall-clock / 人工介入率 / PR review 通过率

**G4. 复盘机制**
- 每周一次 mission 集合复盘
- 把高质量 retro 提升到全局库
- 把反复发生的失败模式写进 Coder/Validator prompt 的"反例"

**G5. 文档化与可分享**
- README 写到能让别人 onboard
- agent_team_soul.md 持续维护
- 一份"已知失败模式"清单

### 退出门槛（你愿意推荐给别人用的标准）

- [ ] 在真实 Rust 项目上 7-day mission 跑通且 PR 被你 review-and-merge
- [ ] 不同项目类型下首次通过率稳定 ≥ 60%（中位数）
- [ ] 不同项目类型下最终通过率稳定 ≥ 90%
- [ ] 人工介入率（Human Gate 触发次数 / mission 总数）≤ 20%
- [ ] 你能自然地说出"我每周用 MAF-Coder 完成了 N 件事，节省了 M 小时"
- [ ] README + soul.md 让一个不了解的人 1 天内能本地跑起来

---

## 仓库结构（最终态）

```
maf-coder/
├── pyproject.toml
├── README.md
├── agent_team_soul.md             # v3 宪法（live document）
├── CHANGELOG.md
├── config/
│   ├── droid_whispering.yaml
│   ├── rust_sandbox.dockerfile
│   ├── permissions/
│   │   ├── orchestrator.yaml
│   │   ├── research.yaml
│   │   ├── coder.yaml
│   │   ├── security.yaml
│   │   ├── review_validator.yaml
│   │   └── behavior_validator.yaml
│   └── probes/
│       ├── cli_assert_cmd_probe.yaml
│       ├── backend_service_health_probe.yaml
│       ├── library_example_probe.yaml
│       ├── embedded_host_test_probe.yaml
│       └── wasm_node_probe.yaml
├── prompts/
│   ├── orchestrator.md
│   ├── research_worker.md
│   ├── coder_worker.md
│   ├── security_worker.md
│   ├── review_validator.md
│   ├── behavior_validator.md
│   └── adversarial_subagent.md
├── src/maf_coder/
│   ├── __init__.py
│   ├── cli.py
│   ├── orchestrator/
│   │   ├── planner.py
│   │   ├── scheduler.py
│   │   ├── status_reporter.py
│   │   ├── budget_guard.py
│   │   ├── stuck_recovery.py
│   │   └── contract.py
│   ├── workers/
│   │   ├── research.py
│   │   ├── coder.py
│   │   └── security.py
│   ├── validators/
│   │   ├── review.py
│   │   ├── behavior.py
│   │   ├── probes/
│   │   │   ├── cli_probe.py
│   │   │   ├── service_probe.py
│   │   │   ├── library_probe.py
│   │   │   ├── embedded_probe.py
│   │   │   └── wasm_probe.py
│   │   └── adversarial_subagent.py
│   ├── profiler/
│   │   └── project_profiler.py
│   ├── sandbox/
│   │   ├── docker_runtime.py
│   │   ├── checkpoint.py
│   │   └── snapshot.py
│   ├── sanitizer/
│   │   ├── content_sanitizer.py
│   │   └── injection_detector.py
│   ├── blackboard/
│   │   ├── artifact_store.py
│   │   └── event_log.py
│   ├── models/
│   │   └── router.py
│   ├── memory/
│   │   ├── project_memory.py
│   │   ├── global_lessons.py
│   │   └── retrieval.py
│   ├── delivery/
│   │   ├── git_workflow.py
│   │   └── pr_creator.py
│   ├── messaging/
│   │   ├── user_inbox.py
│   │   └── notifier.py
│   └── schemas/
│       ├── message.py
│       ├── task.py
│       ├── handoff.py
│       ├── validation_contract.py
│       ├── project_profile.py
│       ├── verdict.py
│       ├── status_report.py
│       └── checkpoint.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│       ├── phase_a_hello_world/
│       ├── phase_b_small_projects/
│       ├── phase_c_refactor/
│       ├── phase_d_service/
│       ├── phase_e_multiday/
│       └── phase_g_real/
└── missions/                    # 运行时（gitignored）
```

---

## 关键工程注意点（每个阶段都要小心）

### Rust 编译时间是头号成本
- target/ 必须跨 mission 持久化（Docker volume）
- sccache 全程开启
- 多 Worker 共享同一 target（在 git worktree 隔离的同时）需要 careful design——Phase C 处理
- `cargo check` 优先于 `cargo build`：Coder 自检阶段用 check 不用 build
- `cargo nextest` 比 `cargo test` 快 1.5-3x，Phase B 起就用

### Prompt 是产品，不是代码
- 所有 agent prompts 在 `prompts/` 下；改 prompt 比改 Python 代码效果大得多
- 每次 prompt 改动跟 git commit + retro 标注
- 不要把 prompt 写死在 Python 里

### 沙箱与宿主的边界
- 所有 cargo / git / 外部工具调用必须在容器内
- 工件持久化通过 mount，不通过 docker cp
- Sandbox 异常退出后 host 端必须能 resume

### 模型路由的可观察性
- 每次 LLM 调用打 trace：role / model / tokens_in / tokens_out / cost / latency
- 异常率 / latency 异常时报警
- 供应商切换 fallback 时要记录原因，不是悄悄换

### 跨语言协作（远期）
- 当前所有 agent prompt 是中英混合
- 工件 markdown 用中文也可（你自己读）
- 代码注释和 prompt 命令式部分用英文（模型理解更稳）

---

## 不在 v3 范围（明确推到 v4+）

- Mission Control UI（web 仪表盘）
- 多用户 / 多租户
- A2A 协议（跨进程 agent）
- 真正的浏览器自动化 BehaviorValidator
- 跨语言支持（Python/JS/Go/Java）
- 云端 sandbox（按需起 VM）
- Mission 编排的可视化编辑器
- PR 评论触发 follow-up mission（v3 仅预留接口）

---

## 立即可执行的第一步

```bash
mkdir maf-coder && cd maf-coder
git init && git branch -m main
python -m venv .venv && source .venv/bin/activate
pip install openai-agents litellm pydantic pyyaml gitpython docker rich pytest
mkdir -p src/maf_coder config/{permissions,probes} prompts tests/{unit,integration,e2e} missions
cp <path>/agent_team_soul_v3.md ./agent_team_soul.md
cp <path>/rust_sandbox.dockerfile ./config/
cp <path>/droid_whispering.yaml ./config/
```

然后开始 Phase A 的 A2（先把 Docker 镜像构建跑通），因为整个 Rust 流水线的吞吐率瓶颈在这里。
