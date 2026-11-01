# Soulseed 技术版本档案

> 本文档记录 Soulseed 从 v1.0.1 到 v1.2.0 阶段 2.6 的记忆系统技术演进。v1.2.0 仍在开发中，阶段 2.6 的完成不代表整个版本发布。本文同时服务于版本回顾、架构复盘、问题追溯和后续开发规划。
>
> 重要的历史边界：当前 Git 仓库没有 v1.0.1–v1.1.0 的逐版本 Tag，也没有为每个版本建立独立 Commit。Git 中可以直接确认的主要提交是基础实现、审计与幂等加固、原子 Proposal 纠正三个阶段。因此，本文对逐版本的归属严格区分代码证据、现有开发报告和无法进一步确认的历史信息；无法拆分的内容不会伪造 Commit 映射。

## 目录

- [版本与 Git 证据总表](#版本与-git-证据总表)
- [总体演进路线](#总体演进路线)
- [v1.0.1 — 记忆存储与生命周期管理](#v101--记忆存储与生命周期管理)
- [v1.0.2 — 确定性记忆提取](#v102--确定性记忆提取)
- [v1.0.3 — 记忆检索](#v103--记忆检索)
- [v1.0.4 — 中文检索误召回修复](#v104--中文检索误召回修复)
- [v1.0.5 — 变更审计与安全加固](#v105--变更审计与安全加固)
- [v1.0.6 — 纠正机制架构审查](#v106--纠正机制架构审查)
- [v1.0.7 — CorrectionIntent 分类](#v107--correctionintent-分类)
- [v1.0.8 — 只读冲突检测器](#v108--只读冲突检测器)
- [v1.0.9 — Proposal 存储与生命周期](#v109--proposal-存储与生命周期)
- [v1.1.0 — 用户确认后的原子记忆纠正服务](#v110--用户确认后的原子记忆纠正服务)
- [v1.2.0 — 记忆纠正闭环（开发中，阶段 1–2.6）](#v120--记忆纠正闭环开发中阶段-12)
- [当前已知限制与后续方向](#当前已知限制与后续方向)

## 文档范围与证据约定

本文只记录当前仓库能够支持的事实。

- “实现事实”指能够从当前代码、数据库迁移或测试中直接验证的内容。
- “开发报告”指本项目开发过程中明确记录、但不一定能由单个历史 Commit 独立还原的版本信息。
- “无法确认”指 Git 历史或当前文件不足以证明的内容。此类内容会明确标注，不用推测补齐。
- Migration 已编写、测试数据库已应用、真实数据库已应用是三个不同状态，文中分别说明。

## 版本与 Git 证据总表

| 版本 | 功能归属 | 可确认的 Git 证据 | 版本级 Commit 是否可独立确认 |
|---|---|---|---|
| v1.0.1 | 记忆存储与生命周期 | `e88e10b` 基础 Soulseed 实现中包含 `memory_items`、`memory_sources` 和 Migration 2 | 否，基础提交同时包含后续基础能力 |
| v1.0.2 | 确定性记忆提取 | `e88e10b` 中包含 `extractor.py`、`MemoryService.persist_candidate()` 和提取测试 | 否 |
| v1.0.3 | 记忆检索 | `e88e10b` 中包含 `retriever.py`、`context_builder.py`、聊天集成和检索测试 | 否 |
| v1.0.4 | 中文检索误召回修复 | 当前 `retriever.py` 和检索测试包含边界过滤、弱词过滤和误召回用例；未找到独立提交 | 否 |
| v1.0.5 | 变更审计与安全加固 | `240961d`：`feat(memory): add transactional change audit and idempotency safeguards` | 该提交可确认，但是否只对应 v1.0.5 仍无 Tag 证明 |
| v1.0.6 | 纠正机制架构审查 | 现有开发阶段记录；未找到独立实现 Commit | 否 |
| v1.0.7 | `CorrectionIntent` 分类 | `3ad566b` 中包含 `CorrectionIntent` 和提取分类测试 | 无独立 Commit |
| v1.0.8 | 只读冲突检测器 | `3ad566b` 中包含 `conflict_detector.py` 和冲突测试 | 无独立 Commit |
| v1.0.9 | Proposal 存储与生命周期 | `3ad566b` 中包含 Proposal 模型、仓储、服务、Migration 5 和 Proposal 测试 | 无独立 Commit |
| v1.1.0 | 用户确认后的原子纠正 | `3ad566b`：`feat(memory): implement atomic proposal correction`，包含 Migration 6、`MemoryCorrectionService` 和纠正测试 | 可确认该提交包含 v1.1.0 实现，但同时承载 v1.0.7–v1.0.9 的代码 |

可直接确认的提交顺序如下：

```text
fb87e84  Initial commit
  └─ ecfb593  baseline checkpoint
      └─ e88e10b  feat: establish initial Soulseed foundation
          └─ 240961d  feat(memory): add transactional change audit and idempotency safeguards
              └─ 3ad566b  feat(memory): implement atomic proposal correction
```

仓库当前没有版本 Tag。本文不把提交时间、提交标题或文件数量当作精确的版本发布时间表。

## 总体演进路线

Soulseed 的记忆系统不是一次性引入一个“大而全”的智能记忆模块，而是沿着风险逐步收紧的路径演进：

```mermaid
flowchart LR
    A[记忆存储] --> B[确定性提取]
    B --> C[检索与上下文组装]
    C --> D[中文误召回治理]
    D --> E[变更审计与幂等]
    E --> F[纠正意图分类]
    F --> G[只读冲突检测]
    G --> H[Proposal 生命周期]
    H --> I[用户确认后的原子应用]
```

各阶段的依赖关系是：

1. 没有稳定的数据模型，就不能安全保存记忆和来源。
2. 没有确定性提取边界，自动记忆会把问题、假设或第三方内容写入正式记忆。
3. 没有检索与上下文预算，记忆无法在对话中发挥作用，或者会污染模型上下文。
4. 没有审计、幂等和生命周期，记忆一旦被错误写入就难以解释、恢复或安全重试。
5. 纠正不是普通的“再写一条记忆”：系统必须先理解纠正意图、检测潜在冲突，再等待用户确认。
6. Proposal 将“发现候选变更”和“应用正式变更”分开，v1.1.0 才把确认后的应用动作纳入一个原子事务。

记忆系统始终是人格理解和对话的辅助基础设施。它可以补充与用户相关的事实，但不应该反过来覆盖“大肥鱼”的核心人格、关系边界、成长原则和诚实边界。`MemoryContextBuilder` 明确将记忆包装为参考资料，并声明它不是指令、不能覆盖核心人格或用户当前明确表达；`persona.py` 也保证运行时补充提示只能追加，不能替换人格资料。

---

## v1.2.0 — 记忆纠正闭环（开发中，阶段 1–2.6）

### 版本信息

- 版本名称：记忆纠正流程编排层
- 当前状态：v1.2.0 仍在开发中，目前完成阶段 1 和阶段 2.1–2.6；不是 v1.2.0 整体发布
- 阶段范围：CorrectionFlowService 编排、ChatService 接入、候选级异常隔离、Proposal 幂等与历史重放安全
- 阶段 1 验证记录：后端完整测试 `133 passed`
- 阶段 2.6 最终独立复核：后端完整测试 `158 passed, 0 failed, 0 skipped`；存在一条 Starlette/httpx 弃用警告

阶段 1 本身没有接入 `ChatService`、HTTP API 或前端。阶段 2.1–2.6 随后将纠正候选编排接入聊天主链路，但当前仍没有 Proposal HTTP API、前端确认与应用交互，也没有形成完整的聊天纠正闭环。

### 阶段 1 已完成能力

阶段 1 新增独立的 `CorrectionFlowService`，将已存在的确定性记忆候选、`CorrectionIntent`、只读冲突检测和 Proposal 服务组织成独立的后端编排层。它接收已经持久化的真实 user message 和外部候选，不重复提取消息，也不直接修改正式记忆。

当前流程边界为：

```text
已提取候选
  -> 意图分流
  -> 查询 active 记忆
  -> 只读冲突检测
  -> Proposal 创建或澄清
  -> 等待后续用户确认
```

`CorrectionFlowService` 返回结构化结果，区分：

- `no_action`
- `proposal_created`
- `proposal_reused`
- `proposal_accepted`
- `proposal_applied`
- `proposal_rejected`
- `proposal_cancelled`
- `proposal_expired`
- `clarification_needed`
- `coexisting`
- `processing_failed`

历史 Proposal 被幂等复用时会保留真实状态，不再把 accepted、rejected、cancelled 或 expired Proposal 伪装成新创建的 Proposal。

### Proposal 创建时的目标版本一致性

`ProposalCreate` 增加可选的 `expected_target_memory_updated_at`。`CorrectionFlowService` 创建纠正 Proposal 时，显式传入冲突检测实际使用的目标版本。

`MemoryProposalRepository` 在创建事务中重新读取目标，并同时校验：

1. 目标记录存在；
2. 目标仍然是 `active`；
3. 当前 `updated_at` 与 expected version 完全一致。

校验失败时不会自动改用最新版本，也不会自动重试。目标变化和目标失效分别返回可识别的处理失败结果，避免出现“旧冲突证据对应新目标快照”的 Proposal。

该机制只强化纠正编排使用的 Proposal 创建流程。没有 expected version 的既有普通 Proposal 创建调用继续保留原有兼容语义。

### 结构化冲突匹配信号

`MemoryConflictResult` 新增结构化匹配信号：

- `exact_key_match`
- `shared_terms`
- `same_slot`
- `same_attribute`
- `temporary_signal`
- `explicit_replacement_signal`

这些字段用于区分主题相关、同一事实属性、临时行为和明确替代，不让编排层解析中文 evidence 字符串。

当前长期变化的安全判断不再仅依据：

```python
related = exact_key or bool(shared_terms) or same_slot
```

`LONG_TERM_CHANGE` 还必须具备：

- 可靠的同一事实属性信号；
- 明确的长期替代表达；
- `recommended_action = PROPOSE_CORRECTION`；
- 没有临时或一次性行为信号。

因此，学习 C++ 与学习 Rust 不会仅因共同出现“学习”而创建替代 Proposal；“最近尝试 Python”和“在 Linux 上运行过一次程序”也不会仅凭主题或外部错误的长期变化标记绕过防护。

明确长期改变同一属性，例如主要开发环境从 Windows 永久换成 Linux，仍然可以创建 pending Proposal，等待用户确认后再由 v1.1.0 的 `MemoryCorrectionService` 原子应用。

### 与 v1.1.0 的边界

本阶段保留 `EXPLICIT_CORRECTION` 的既有行为和 Proposal 幂等机制，没有重写 v1.1.0 的 `MemoryCorrectionService`，没有改变正式记忆的原子应用流程。

本阶段没有新增数据库 Migration，也没有执行真实数据库迁移。Proposal 创建一致性、正式记忆替代、来源写入、审计和事务回滚仍由已有 v1.1.0 服务负责。

### 阶段 2.1–2.5：ChatService 主链路接入

`ChatService` 在持久化 user message 后调用确定性提取器一次，并将 `CorrectionIntent.NONE` 候选交给普通记忆持久化路径，将纠正候选交给 `CorrectionFlowService`。纠正流程失败会记录日志并在聊天 correction 摘要中保留失败状态；普通聊天仍可继续。纠正 Proposal 的创建与后续用户确认、正式应用保持分离。

聊天响应保留原 assistant message 字段，并增加结构化 `correction` 摘要。摘要包含顶层状态和逐候选 `items`；Proposal 已创建或复用时包含真实 Proposal ID，澄清和失败状态不会伪造 Proposal ID。候选状态不一致时顶层使用 `mixed`。

### 阶段 2.6：候选隔离、并发幂等与历史重放安全

- `CorrectionFlowService.process()` 对每个候选独立处理；一个候选出现未预期异常时返回该候选的 `processing_failed`，继续处理后续候选，并保留已成功候选的 Proposal ID 和顺序。
- Proposal 并发创建遇到幂等唯一键冲突时，仓储回滚失败事务，在新连接中按幂等键查询已提交记录，并核对 request fingerprint；相同请求复用既有 Proposal，不同请求参数报告幂等冲突，其他数据库完整性错误不会被冒充为复用成功。
- 历史重放按来源消息、候选和 correction 类型识别既有 Proposal，并保留 pending、accepted、applied、rejected、cancelled、expired 等真实生命周期状态。同一来源消息重放保持幂等；不同来源消息可以重新发起纠正。
- 历史目标因外键 `ON DELETE SET NULL` 被删除后，重放仍识别原 Proposal；apply 会在原子事务内验证目标快照，拒绝将已删除的原目标伪装为无目标 Proposal。应用失败不会留下 replacement、正式记忆变更或 application log。
- 模型调用失败不自动接受或应用已创建 Proposal；assistant 消息保存失败不生成虚假的完整聊天成功响应，也不回滚或改写 Proposal 的真实状态。
- 阶段 2.6 最终验证执行后端完整测试 `158 passed, 0 failed, 0 skipped`，`compileall` 通过，`git diff --check` 通过；测试报告有一条 Starlette/httpx 弃用警告。

### 当前未完成内容

以下内容仍未实现，不应视为 v1.2.0 已完成：

- 尚无 HTTP 请求级幂等键；当前业务级重放保护依赖已持久化的来源 message_id；
- 尚无 Proposal HTTP API 和完整的前端确认、拒绝及应用交互；
- 尚未实现从聊天消息、Proposal 确认到正式记忆更新的完整用户可操作闭环；
- 尚未实现多用户认证或公网部署安全；当前不能据此宣称适用于公网多用户环境；
- 尚未进行真实模型 API 端到端验证；阶段测试不调用真实模型 API；
- 没有新增 Migration，也没有修改 v1.1.0 `MemoryCorrectionService` 的原子应用语义。

---

## v1.0.1 — 记忆存储与生命周期管理

### 版本信息

- 版本名称：记忆存储与生命周期管理
- 内部阶段编号：v1.0.1
- Git 证据：基础提交 `e88e10b`，没有独立版本 Tag
- 核心目标：建立可以被独立保存、查询、追溯和安全淘汰的长期记忆基础设施。

### 开发背景与问题

初始对话系统只有会话、消息和人格提示。人格系统负责“系统是谁以及应该如何表达”，但不负责保存用户长期事实。若把用户事实直接混入人格提示或聊天历史，会产生几个问题：

- 无法区分人格设定、原始消息和整理后的记忆。
- 无法查询某条记忆来自哪条消息。
- 无法表示记忆已经失效、被替代或被删除。
- 后续检索和纠正只能直接操作对话文本，无法形成稳定的数据边界。

因此，第一阶段把记忆作为独立的数据域建立起来，并且保持它与人格系统分离。

### 核心设计与实现

记忆域的核心实体是 `MemoryItem` 和 `MemorySource`：

| 实体 | 主要职责 |
|---|---|
| `memory_items` | 保存记忆类型、正文、规范化键、重要性、置信度、生命周期状态和时间字段 |
| `memory_sources` | 保存记忆对应的原始消息、来源角色、证据类型、摘录和创建时间 |
| `MemoryRepository` | 负责记忆的创建、读取、更新、状态变更、删除、来源挂接和基础约束 |
| `MemoryService` | 对仓储操作提供面向业务的服务接口 |
| `MemoryContextBuilder`、`MemoryRetriever` | 后续版本使用的上层模块，不属于本版本存储职责 |

记忆类型由 `MemoryType` 限定为：`user_preference`、`relationship`、`event`、`goal`、`growth`。状态由 `MemoryStatus` 限定为：

```text
active -> invalid
active -> expired
active -> superseded
active -> deleted
```

被替代的记忆通过 `superseded_by_id` 指向替代记忆，而不是直接覆盖原始行。软删除隐藏记忆但保留数据；永久删除只在审计和外键规则允许的范围内移除实体，原始消息不随记忆被删除。

### 关键文件与模块

- `backend/app/memory/models.py`：定义 `MemoryType`、`MemoryStatus`、`MemoryCreate`、`MemoryUpdate` 和 `MemoryItem` 等数据模型。
- `backend/app/memory/repositories.py`：实现记忆 CRUD、状态生命周期、active `canonical_key` 唯一性和来源关联。
- `backend/app/memory/service.py`：向应用层暴露记忆服务。
- `backend/app/db.py`：Migration 2 创建 `memory_items`、`memory_sources` 及相关索引。
- `backend/tests/test_memory.py`：覆盖创建、读取、更新、筛选、来源、重复键、状态和删除行为。

### 数据库变化

Migration 2 新增：

- `memory_items`
- `memory_sources`
- active `canonical_key` 唯一索引
- 类型、状态和时间索引
- `memory_sources.memory_id` 到 `memory_items.id` 的外键
- `memory_sources.message_id` 到 `messages.id` 的外键

Migration 已写入代码，并由测试数据库初始化流程执行。没有证据表明该阶段单独迁移了某个生产数据库。

### 安全性与可靠性

- 记忆正文和 canonical key 经过非空、规范化和分数范围校验。
- 来源消息必须存在，来源角色必须与原消息角色一致。
- active 记忆不能共享同一个 canonical key。
- 外键和删除规则保护记忆、来源和消息之间的关系。
- 人格资料不写入记忆表，记忆不能替换人格配置。

### 测试与验证

当前测试文件可以确认该基础能力覆盖了记忆读写、来源关联、状态隐藏、永久删除和替代关系。v1.0.1 单独版本的历史测试数量无法从 Git 中独立确认，因为测试文件后来持续演进。

### 成果、限制与下一版本关系

本版本建立了长期记忆的持久化边界和生命周期边界，但不会自动从用户消息中提取记忆，也不会把记忆检索回模型上下文。下一步需要解决“什么内容可以进入正式记忆”的问题，这形成 v1.0.2 的确定性提取阶段。

---

## v1.0.2 — 确定性记忆提取

### 版本信息

- 版本名称：确定性记忆提取
- 内部阶段编号：v1.0.2
- Git 证据：`e88e10b` 基础实现中包含提取器和提取测试；没有独立 Tag
- 核心目标：从用户明确表达中稳定提取候选记忆，同时拒绝低价值、不确定或非用户事实。

### 开发背景与问题

仅有存储接口意味着所有记忆都必须由其他代码手工构造。若直接让模型决定“应该记住什么”，系统很难解释为什么写入某条记忆，也无法稳定测试。更危险的是，模型可能把问题、假设、引用他人的话或短暂情绪写成长期事实。

因此，当前实现选择保守的确定性规则：先识别可观察的表达模式，再生成候选；无法确认时不写入正式记忆。

### 核心设计与实现

`DeterministicMemoryExtractor` 只接受 `role == user` 的消息。它会：

1. 清理空白并拒绝空内容。
2. 拒绝问句、假设、第三方前缀和低价值短句。
3. 按偏好、目标、成长、关系、事件等规则识别候选。
4. 生成 `memory_type`、`content`、`canonical_key`、重要性、置信度和证据类型。
5. 使用 canonical key 去重。
6. 将候选交给 `MemoryService.persist_candidate()`，由正式记忆仓储负责唯一性、来源和幂等。

提取器不会把助手回复作为用户事实来源，也不会仅凭一个模糊的模型判断替代现有记忆。候选的 `EvidenceType` 反映证据性质；普通明确陈述使用 `explicit_fact`，明确纠正使用 `correction`。

### 关键文件与模块

- `backend/app/memory/extractor.py`：实现 `MemoryCandidate` 和 `DeterministicMemoryExtractor`。
- `backend/app/memory/service.py`：`persist_candidate()` 检查来源角色、写入记忆、挂接来源，并对重复处理保持幂等。
- `backend/app/chat_service.py`：在用户消息保存后调用提取流程；提取失败记录日志但不让聊天请求整体失败。
- `backend/tests/test_memory_extractor.py`：覆盖明确偏好、目标、事件、关系、成长、低价值内容、第三方内容、助手消息和重复处理。

### 安全性与可靠性

- 只从 user 消息提取。
- 问题、假设、第三方陈述和低价值内容不会进入正式记忆。
- 处理重复消息时使用基于消息 ID、候选 canonical key 的幂等键。
- 记忆写入失败不会伪造成功；聊天流程对提取异常采用隔离处理。

### 测试与验证

测试重点是“宁可不记，也不要把不可靠信息写成事实”。当前测试还验证：同一消息重复处理不会创建重复记忆，重复 canonical key 不会让聊天失败。

### 成果、限制与下一版本关系

本版本解决了记忆进入系统前的确定性门槛，但候选仍需要被检索才能对后续对话产生作用。下一版本增加检索和上下文组装，同时继续保持人格与记忆的边界。

---

## v1.0.3 — 记忆检索

### 版本信息

- 版本名称：记忆检索
- 内部阶段编号：v1.0.3
- Git 证据：`e88e10b` 基础实现中包含 `retriever.py`、`context_builder.py` 和检索测试
- 核心目标：只把与当前问题相关的少量正式记忆作为参考资料注入模型上下文。

### 开发背景与问题

记忆被保存并不等于对话能够使用它。如果每轮对话都加载全部记忆，会浪费上下文、增加误导风险，并把不相关事实暴露给模型。系统需要一个按需、可解释、有限预算的检索层。

### 核心设计与实现

`MemoryRetriever` 扫描有界数量的 active 记忆，在 Python 中执行确定性关键词匹配和评分。主要步骤是：

1. 将查询规范化并提取英文词、数字词及中文片段。
2. 排除停用词、低信息量片段和没有强匹配的结果。
3. 在记忆正文和 canonical key 中匹配候选词。
4. 根据覆盖率、短语命中、重要性和置信度计算分数。
5. 排除非 active、已删除和过期记忆。
6. 加载 `memory_sources`，返回包含来源、分数和命中词的 `MemoryMatch`。
7. 按相关性、重要性、置信度和稳定字段排序，并限制返回数量。

`MemoryContextBuilder` 不负责检索。它只负责把 `MemoryMatch` 变成有预算的 `<memory_reference>` 资料块：限制总字符数、条目数和单条记忆长度，转义 XML 特殊字符，并附带 memory ID、类型和来源消息 ID。

当前轮的用户消息先保存、再尝试检索；当轮新提取的候选在后续提取阶段写入，因此不会被同一轮的检索过程立即注入。这避免了当前输入在模型回复前被系统错误地当成已经确认的长期背景。

### 关键文件与模块

- `backend/app/memory/retriever.py`：实现 `MemoryRetriever` 和 `MemoryMatch`。
- `backend/app/memory/context_builder.py`：实现记忆参考资料的预算和安全组装。
- `backend/app/chat_service.py`：在调用模型前把记忆参考资料作为独立 system message 加入。
- `backend/app/config.py`：提供记忆上下文最大字符数、条目数和单条长度配置。
- `backend/tests/test_memory_retrieval.py`：覆盖相关性、过滤、排序、来源追溯、预算和聊天上下文集成。

### 人格边界

记忆上下文明确标记为参考资料，不是指令，不能覆盖核心人格或用户当前明确表达。人格资料仍然由 `persona.py` 从 `persona_profile.md` 加载；运行时补充约束只能追加。

### 成果、限制与下一版本关系

本版本建立了“保存的记忆 → 按需召回 → 有限上下文”的闭环，但关键词匹配对中文短片段较敏感，低信息量 n-gram 可能让无关记忆被召回。下一版本专门处理中文误召回。

---

## v1.0.4 — 中文检索误召回修复

### 版本信息

- 版本名称：中文检索误召回修复
- 内部阶段编号：v1.0.4
- Git 证据：当前 `MemoryRetriever` 和 `test_memory_retrieval.py` 可以确认修复后的实现与测试；没有发现独立 v1.0.4 Commit 或 Tag
- 核心目标：降低中文短片段造成的低质量召回，同时保留有意义的中文主题词。

### 开发背景与问题

中文没有天然的空格分词。检索器为了支持中文，会从较长字符串中生成二元和三元片段。若所有片段都视为等价关键词，`我更`、`你还`、`喜欢`、`学习` 等高频或低信息量片段就可能在无关记忆中命中，造成误召回。

误召回的后果不是只有排序变差：错误记忆会进入模型上下文，可能影响模型对用户的理解。因此，检索器需要判断“词面命中”是否具有足够信息量，而不是单纯扩大召回覆盖率。

### 修复策略

当前实现采用多层过滤：

- 停用词集合过滤常见功能词。
- `_low_information_boundary_chars` 只在片段边界过滤低信息字符，避免把包含这些字符但本身有意义的词全部删除。
- `_weak_cjk_terms` 标记单独出现时不足以形成可靠命中的中文词。
- `score_memory()` 要求至少一个 strong term 或完整短语命中，否则分数为零。
- 保留正文和 canonical key 的匹配，并继续使用相关性、重要性和置信度进行排序。

这不是简单的“禁止所有二元词”，而是让低信息片段不能单独成为召回依据，同时保留主题词、较长片段和完整短语的作用。

### 关键文件与测试

- `backend/app/memory/retriever.py`：边界字符、弱中文词和 strong term 过滤。
- `backend/tests/test_memory_retrieval.py`：覆盖低信息中文片段不应误召回、无关查询不应命中以及相关记忆仍然可以召回。

### 成果、限制与下一版本关系

该修复使当前关键词检索更保守、更适合中文，但它仍然不是语义检索，也不能判断用户是否在纠正已有记忆。后续系统需要对记忆变化进行审计，并在纠正路径上引入更严格的状态管理。

---

## v1.0.5 — 变更审计与安全加固

### 版本信息

- 版本名称：变更审计与安全加固
- 内部阶段编号：v1.0.5
- 可确认 Commit：`240961d` — `feat(memory): add transactional change audit and idempotency safeguards`
- 核心目标：让每次正式记忆变更都可追溯、可验证、可安全重试，并在审计失败时整体回滚。

### 开发背景与问题

有了创建、更新和生命周期状态后，单看 `memory_items` 当前值无法回答：谁改变了它、旧值是什么、为什么改变、来源消息是什么、是否是重复请求。纠错机制尤其依赖这些信息，否则系统可能在不知情的情况下覆盖用户事实，或者在网络重试时重复执行。

### 核心设计与实现

Migration 3 建立 `memory_change_log`，记录：

- 记忆 ID和操作类型：创建、更新、失效、过期、替代、软删除、永久删除。
- 旧/新正文、canonical key、状态、重要性和置信度。
- 变更原因、来源消息 ID、操作人类型和时间。
- 幂等键。

Migration 4 增加 `request_fingerprint` 和对应索引。仓储层对创建、更新、状态变更和永久删除统一计算请求指纹：同一个幂等键重复到达且参数一致时返回已有结果；参数不一致或跨记忆复用时抛出 `IdempotencyConflictError`。

变更实体和审计记录在同一个 `Database.connection()` 上下文内写入。如果审计插入失败，数据库上下文回滚记忆变更。这使审计不是“尽力而为的日志”，而是正式数据变更的一部分。

### 关键文件与模块

- `backend/app/db.py`：Migration 3、4，以及迁移失败时回滚迁移标记的事务处理。
- `backend/app/memory/repositories.py`：统一指纹、幂等重放、审计写入、生命周期和删除处理。
- `backend/app/memory/models.py`：`MemoryChangeAction`、`MemoryActorType`、`MemoryChangeLog`。
- `backend/app/memory/service.py`：向上层暴露审计查询和带幂等参数的变更接口。
- `backend/tests/test_memory_history.py`：覆盖审计内容、生命周期、永久删除、审计失败回滚、幂等和迁移。

### 安全性与可靠性

- 记忆正文变更可通过 old/new 快照追溯。
- 永久删除审计不复制已删除记忆的正文，避免删除后在日志中保留不必要的敏感内容。
- 来源消息使用外键和角色校验保持可追溯性。
- 幂等键防止重试造成重复效果。
- 审计失败会让正式记忆变更回滚。
- 数据库迁移和迁移标记保持原子性。

### 成果、限制与下一版本关系

本版本提供了正式记忆变更的安全底座，但它解决的是“已经决定变更后如何记录和执行”，还没有解决“用户这句话是不是在纠正某条已有记忆”。因此后续需要先做纠正机制的架构审查，再实现意图分类、冲突检测和 Proposal。

---

## v1.0.6 — 纠正机制架构审查

### 版本信息

- 版本名称：纠正机制架构审查
- 内部阶段编号：v1.0.6
- Git 证据：没有找到独立实现 Commit；本节基于项目开发阶段记录和后续代码能够验证的架构约束
- 核心目标：在任何自动修改正式记忆之前，明确纠正机制的职责边界、确认点和安全约束。

### 开发背景与问题

普通的记忆提取适合处理“我喜欢 Rust”“我的目标是通过考试”这类新信息，但纠正场景不同：

- 用户可能明确说之前错了，也可能只是暂时尝试。
- 新内容可能与旧记忆冲突，也可能与旧记忆共存。
- 系统可能无法可靠判断用户是在更新、否定还是补充。
- 直接由模型覆盖旧记忆会丢失历史、来源和用户控制权。

因此，纠正机制不能成为聊天流程中的隐式覆盖操作。架构约束是：检测与应用分离，正式应用必须经过用户确认，应用必须经过审计和幂等保护。

### 设计关系

```text
用户消息
  -> CorrectionIntent：判断表达的变化意图
  -> ConflictDetector：只读比较候选与已有记忆
  -> Proposal：保存待确认的候选变更和证据
  -> 用户确认
  -> MemoryCorrectionService：原子应用正式记忆变更
```

这些概念职责不同：

- `CorrectionIntent` 解释用户表达，不执行数据库写入。
- `MemoryConflictDetector` 产生关系、证据、置信度和建议动作，不修改任何记忆。
- Proposal 保存候选变更和生命周期，不等于正式记忆。
- `MemoryCorrectionService` 只处理已确认、可再次校验的 Proposal。

### 为什么不能让模型直接覆盖正式记忆

直接覆盖会同时破坏可追溯性、用户控制、幂等性和冲突处理。模型输出可以帮助识别候选，但不能单独作为正式数据变更授权。该约束后来体现在：

- 纠正候选不直接持久化为 active 记忆。
- Proposal 与 `memory_items` 分表存储。
- Proposal 的 `accepted` 状态和正式应用状态分离。
- v1.1.0 的应用流程重新校验来源和目标快照。

### 设计与实现的边界

本版本是架构审查和约束形成阶段，不应被描述为已经完成完整纠正流程。后续 v1.0.7–v1.0.9 分别实现意图分类、只读冲突检测和 Proposal 存储；v1.1.0 才实现确认后的原子应用。

---

## v1.0.7 — CorrectionIntent 分类

### 版本信息

- 版本名称：`CorrectionIntent` 分类
- 内部阶段编号：v1.0.7
- Git 证据：当前代码和测试位于 `3ad566b` 所包含的原子 Proposal 纠正提交中，没有独立版本 Commit
- 核心目标：将“用户表达发生变化”拆成后续流程可以区别处理的确定性意图类别。

### 分类设计

`CorrectionIntent` 定义为：

| 分类 | 含义 | 后续处理倾向 |
|---|---|---|
| `none` | 没有检测到纠正或变化意图 | 普通明确事实可以进入正常提取路径 |
| `explicit_correction` | 用户明确表示之前说错、要求更正 | 可以进入冲突检测和纠正 Proposal 流程 |
| `long_term_change` | 表达长期变化，例如已经改成、从现在起 | 形成潜在冲突，通常需要确认 |
| `temporary_experiment` | 带有今天、暂时、试试等短期限定 | 倾向保留新旧信息共存，不替代长期记忆 |
| `ambiguous_change` | 好像、可能、感觉最近等不确定表达 | 需要复核，不能自动替代 |
| `coexisting_information` | 明确表达“也喜欢”“同时”等共存信息 | 保留双方，不视为冲突 |

`DeterministicMemoryExtractor.classify_intent()` 按规则和优先级执行分类。显式纠正优先于其他变化标记，避免一句“我之前说错了”被低优先级的临时或模糊词覆盖。

### 与正式记忆的边界

带有特殊意图的候选可以被保留给后续 Proposal 或冲突逻辑，但 `MemoryService.persist_candidate()` 不会把 `intent != none` 的候选直接写入 active 正式记忆。这是“候选可保存”和“正式记忆已确认”的重要区别。

### 关键文件与测试

- `backend/app/memory/models.py`：定义 `CorrectionIntent`。
- `backend/app/memory/extractor.py`：实现分类规则、优先级和特殊候选处理。
- `backend/app/memory/service.py`：阻止特殊意图候选绕过确认直接持久化。
- `backend/tests/test_memory_extractor.py`：覆盖各类意图、优先级、特殊候选不直接持久化和非 user 消息边界。

### 成果、限制与下一版本关系

本版本使系统可以区分“新事实”“明确纠正”“临时试验”和“不确定变化”，但分类本身不判断它具体冲突哪一条记忆。下一版本增加只读 `MemoryConflictDetector`。

---

## v1.0.8 — 只读冲突检测器

### 版本信息

- 版本名称：只读冲突检测器
- 内部阶段编号：v1.0.8
- Git 证据：`3ad566b` 中包含 `conflict_detector.py` 和对应测试
- 核心目标：比较候选记忆与已有 active 记忆，输出可解释的关系和建议，但不直接修改数据库。

### 核心设计与实现

`MemoryConflictDetector.detect()` 接受 `MemoryCandidate` 和已有 `MemoryItem` 集合，先过滤 inactive、删除和过期记忆，再按记忆类型、canonical key、主题词、表达槽位和否定方向分类。

检测结果 `MemoryConflictResult` 包含：

- 候选
- 旧记忆 ID
- `MemoryRelation`
- 证据说明
- 置信度
- 是否需要人工复核
- `RecommendedAction`

主要关系包括：

```text
conflict
potential_conflict
coexisting
unrelated
needs_review
```

示例决策：

- 明确纠正且主题和类型相关：`conflict`，建议提出纠正。
- 长期变化：`potential_conflict`，建议提出纠正但需要确认。
- 临时试验：`coexisting`，建议保留双方。
- 共存表达：`coexisting`，不替代旧记忆。
- 模糊表达：`needs_review`，要求确认。
- 类型不同或缺少可靠主题重合：不自动认定为同一纠正对象。

### 为什么采用只读设计

检测器的输入可能包含不确定性，且它尚未获得用户对具体变更的确认。如果检测器直接更新或删除记忆，任何误判都会变成不可逆的数据污染。只读设计把“判断”与“执行”分开，使检测结果可以进入 Proposal，也可以被测试、审查或丢弃。

### 关键文件与测试

- `backend/app/memory/conflict_detector.py`：关系枚举、建议动作、主题/槽位/否定方向比较和过期过滤。
- `backend/tests/test_memory_conflict.py`：覆盖明确纠正、多候选、长期变化、临时试验、模糊变化、共存信息、无关主题、失效/过期记忆和“检测器不写数据库”。

### 成果、限制与下一版本关系

本版本可以发现潜在冲突并给出可解释证据，但检测结果还不是持久化对象，无法记录用户后来是否接受或拒绝。下一版本把检测结果保存为独立 Proposal，并建立状态生命周期。

---

## v1.0.9 — Proposal 存储与生命周期

### 版本信息

- 版本名称：Proposal 存储与生命周期
- 内部阶段编号：v1.0.9
- Git 证据：`3ad566b` 中包含 Proposal 模型、仓储、服务、Migration 5 和测试
- 核心目标：把“待用户确认的记忆变更候选”作为独立对象保存，避免候选直接污染正式记忆。

### 数据模型与 Migration 5

Migration 5 新增：

- `memory_change_proposals`
- `memory_proposal_change_log`

Proposal 保存：

- Proposal 类型：`correction`、`update`、`invalidation`、`expiration`
- 目标记忆 ID
- 拟创建的记忆类型、正文和 canonical key
- 证据消息 ID
- `CorrectionIntent`
- 冲突关系、置信度和解释性 evidence
- 状态、幂等键、请求指纹和解决原因

Proposal 变更日志记录 Proposal 的创建、接受、拒绝、取消和过期，以及状态前后值、操作者和幂等信息。

### 生命周期

```text
pending
  ├─> accepted
  ├─> rejected
  ├─> cancelled
  └─> expired
```

每个 Proposal 只能从 `pending` 进入一个终态。重复执行同一个终态操作可以幂等返回；尝试从终态转换到其他状态会被拒绝。

### accepted 不等于正式应用

v1.0.9 的 `accept_proposal()` 只改变 Proposal 的确认状态，不创建或替换正式记忆。这个看似保守的行为是有意的：接受动作和正式写入动作尚未拥有共同的原子事务边界，如果在这里提前创建记忆，可能出现 Proposal 已接受但来源、旧记忆状态或审计记录不完整的情况。

### 关键文件与测试

- `backend/app/memory/models.py`：`ProposalType`、`ProposalStatus`、`ProposalCreate`、`MemoryChangeProposal` 和 `ProposalChangeLog`。
- `backend/app/memory/proposal_repositories.py`：Proposal 创建、查询、列表、状态转换、引用校验和审计。
- `backend/app/memory/proposal_service.py`：面向应用层的 Proposal 服务。
- `backend/app/db.py`：Migration 5 和索引。
- `backend/tests/test_memory_proposals.py`：覆盖 Proposal 生命周期、幂等、来源消息约束、删除后的目标引用、事务回滚和不进入 Retriever。

### 成果、限制与下一版本关系

本版本完成了“候选变更”和“正式记忆”的存储隔离，以及用户确认状态的持久化。但 `accepted` 后还没有安全地应用正式变更，因此下一版本需要引入独立的原子纠正服务。

---

## v1.1.0 — 用户确认后的原子记忆纠正服务

### 版本信息

- 版本名称：用户确认后的原子记忆纠正服务
- 内部阶段编号：v1.1.0
- 可确认 Commit：`3ad566b` — `feat(memory): implement atomic proposal correction`
- 当前实现测试结果：后端 `102 passed`
- 核心目标：将 accepted Proposal 安全地应用为正式记忆变更，同时保证原子性、幂等性、并发安全、来源真实性和完整审计。

### 开发背景与问题

v1.0.7–v1.0.9 已经能识别纠正意图、发现冲突并保存 Proposal，但还缺少最后的执行层。尤其是：

- `accepted` 只代表用户确认，不代表正式记忆已经改变。
- Proposal 仓储和 Memory 仓储原本各自管理事务，无法自然保证跨对象原子性。
- 目标记忆可能在 Proposal 创建或用户确认后被其他操作修改、替代或删除。
- 如果只写新记忆而没有同步写来源和审计，会形成不可解释的半成功状态。

v1.1.0 的任务是建立一个独立的应用编排服务，而不是把纠正逻辑塞进 `chat_service.py` 或让模型直接覆盖记忆。

### 新增核心模块

- `backend/app/memory/correction_service.py`
  - `MemoryCorrectionService`：应用 accepted Proposal。
  - `CorrectionApplicationResult`：返回 Proposal、受影响的记忆和替代记忆。
  - `ProposalApplicationError`：报告安全拒绝应用的业务错误。
- `backend/tests/test_memory_correction.py`
  - 覆盖状态门槛、正常替代、幂等、并发、目标快照冲突和事务失败重试。

该服务不接入聊天主链路，也没有新增前端或 HTTP 确认页面。调用方需要显式调用 `apply_proposal()`。

### Migration 6 与数据结构

Migration 6 已写入 `backend/app/db.py`，但没有执行真实数据库迁移。

`memory_change_proposals` 新增：

| 字段 | 用途 |
|---|---|
| `application_status` | 区分 `not_applied` 与 `applied`，避免把 accepted 当成已执行 |
| `target_memory_updated_at` | 保存 Proposal 创建时的目标记忆版本快照 |
| `applied_at` | 记录正式应用时间 |
| `applied_memory_id` | 记录实际被应用的目标记忆 |
| `replacement_memory_id` | 记录纠正时创建的替代记忆 |

`memory_change_log` 新增：

- `proposal_id`：将正式记忆变更审计直接关联到产生它的 Proposal。

新增 `memory_proposal_application_log`：

- 每个 Proposal 最多一条成功应用记录。
- 记录 applied 状态、受影响记忆、替代记忆、操作者、原因、幂等键、请求指纹和时间。
- Proposal ID 唯一约束和幂等键唯一索引防止重复应用记录。

真实数据库状态在本次开发完成后仍是 Migration 2；Migration 6 只在测试临时数据库中被初始化和验证。

### 原子纠正事务

入口为：

```python
MemoryCorrectionService.apply_proposal(
    proposal_id,
    reason=None,
    actor_type=MemoryActorType.USER,
    idempotency_key=None,
)
```

服务在同一个 SQLite 连接中执行 `BEGIN IMMEDIATE`，以写锁串行化并发应用。流程如下：

```mermaid
sequenceDiagram
    participant Caller as 调用方
    participant Service as MemoryCorrectionService
    participant DB as SQLite
    participant Memory as 正式记忆
    participant Audit as 审计记录

    Caller->>Service: apply_proposal(proposal_id)
    Service->>DB: BEGIN IMMEDIATE
    Service->>DB: 读取 Proposal
    Service->>DB: 校验 accepted/not_applied
    Service->>DB: 校验 user 来源和目标快照
    Service->>Memory: 创建替代记忆或更新目标状态
    Service->>DB: 写入 memory_sources
    Service->>Audit: 写入 memory_change_log
    Service->>DB: 更新 Proposal 应用结果
    Service->>Audit: 写入 application_log
    DB-->>Service: COMMIT
    Service-->>Caller: CorrectionApplicationResult
```

事务中包含的操作是一个整体：

1. 读取并锁定 Proposal 所在数据库的写事务。
2. 重新校验 Proposal、来源消息和目标记忆。
3. 按 Proposal 类型创建新记忆或更新旧记忆状态。
4. 写入 `memory_sources`。
5. 写入 `memory_change_log`。
6. 更新 `application_status`、应用时间和结果记忆 ID。
7. 写入 `memory_proposal_application_log`。
8. 任一步骤失败，整个事务回滚。

### 应用前二次校验

应用服务拒绝以下情况：

- Proposal 不存在。
- Proposal 状态不是 `accepted`。
- Proposal 的 `application_status` 不是 `not_applied`。
- 来源消息不存在。
- 来源消息的 `role` 不是 `user`。
- 带目标的旧 Proposal 没有目标版本快照。
- 目标记忆不存在。
- 目标记忆不是 `active`。
- 目标记忆的 `updated_at` 与 Proposal 创建时保存的 `target_memory_updated_at` 不一致。
- 目标记忆类型与 Proposal 拟创建类型不一致。
- 新 active 记忆的 canonical key 已经被其他 active 记忆占用。

目标版本快照是防止旧 Proposal 覆盖新状态的关键机制：如果 Proposal 创建之后目标记忆被修改，应用会安全拒绝，要求重新生成或重新确认 Proposal。

### 不同 Proposal 类型的处理

| Proposal 类型 | 应用结果 |
|---|---|
| `correction` | 创建新的 active 记忆；旧记忆变为 `superseded`，并设置 `superseded_by_id` |
| `update` | 当前实现沿用替代式生命周期，创建新的 active 记忆并将目标记忆标记为 `superseded` |
| `invalidation` | 目标记忆变为 `invalid`，不创建新记忆 |
| `expiration` | 目标记忆变为 `expired`，不创建新记忆 |

所有正式变更都遵守已有的 active canonical key 唯一约束和记忆状态生命周期。

### 来源真实性与隐私边界

新记忆的来源写入规则是：

- `source_role = user`
- `evidence_type = correction`
- `message_id` 指向数据库中实际存在的 user 消息
- `excerpt` 读取真实的 `messages.content`

Proposal 的 `proposed_content` 不被当作用户原话写入来源摘录。原始 `messages` 不会被删除。永久删除记忆的既有实现也不会把被删除正文复制进删除审计事件。

### 幂等、并发与审计

成功应用后，Proposal 会保持 `status = accepted`，同时变为 `application_status = applied`。重复调用会读取应用日志并返回第一次应用的结果，不会创建第二条正式记忆或第二条应用记录。

应用请求指纹包含 Proposal ID、原因和操作者类型。如果重试请求的指纹或幂等键与既有应用不一致，会抛出 `IdempotencyConflictError`，而不是猜测调用方意图。

`memory_change_log.proposal_id` 让新记忆创建、旧记忆替代、失效或过期事件都能反查到 Proposal；`memory_proposal_application_log` 则记录这个 Proposal 是否已经成功完成应用。

### 关键文件

- `backend/app/memory/correction_service.py`：跨 Proposal、Memory、Source 和 Audit 的单事务编排。
- `backend/app/memory/proposal_repositories.py`：创建 Proposal 时记录 `target_memory_updated_at`，并读取 Migration 6 字段。
- `backend/app/memory/models.py`：定义 `ProposalApplicationStatus` 和应用结果相关字段。
- `backend/app/db.py`：Migration 6、应用日志表和索引。
- `backend/app/memory/__init__.py`：导出纠正服务及相关类型。
- `backend/tests/test_memory_correction.py`：原子应用验收测试。

### 测试与验证

本次开发报告和当前仓库验证结果如下：

| 检查项 | 结果 | 说明 |
|---|---|---|
| 后端完整测试 | 通过，`102 passed` | 包含原有测试、Proposal 测试、冲突检测测试和纠正服务测试 |
| Python 编译检查 | 通过 | `python -m compileall -q backend` |
| Git 空白检查 | 通过 | `git diff --check` |
| 前端 TypeScript/Vite 构建 | 未执行 | 环境没有 `npm` 或 `npm.cmd`；本阶段未修改前端 |
| 真实数据库 Migration 6 | 未执行 | 真实数据库只读检查的最高迁移版本仍为 2 |
| 真实模型 API | 未调用 | 测试使用本地临时数据库和现有测试替身 |

纠正测试覆盖：

- `pending`、`rejected`、`cancelled`、`expired` Proposal 不能应用。
- `accepted` Proposal 可以正常创建替代记忆。
- 同一 Proposal 重复应用不会创建重复记忆。
- 并发应用只产生一个正式结果。
- 目标记忆被修改后应用会拒绝且不留下部分结果。
- 任意事务步骤失败会整体回滚，修复后可以重试。
- 新记忆可以追溯到真实 user 消息。
- Proposal 审计、记忆审计和应用记录都能关联。

### 成果、限制与后续关系

v1.1.0 完成了从“用户确认的 Proposal”到“正式记忆变更”的安全执行层，形成如下闭环：

```text
用户表达
  -> 确定性意图分类
  -> 只读冲突检测
  -> Proposal 保存
  -> 用户确认
  -> 原子正式应用
  -> 来源与审计追溯
```

当前仍有明确限制：

- 纠正服务没有接入 `chat_service.py`，聊天主链路不会自动应用 Proposal。
- 当前没有 Proposal HTTP API 和前端确认页面。
- Migration 6 尚未应用到真实数据库。
- 目标 Proposal 是单目标应用模型；多目标 Proposal 需要后续独立效果表或批处理设计。
- 应用失败会整体回滚，因此不会留下“失败应用”业务记录；当前系统记录的是可观察的异常和下一次安全重试条件。

---

## 数据库 Migration 演进总览

| Migration | 内容 | 真实数据库状态 |
|---|---|---|
| 1 | 会话、消息及消息索引 | 历史基础迁移；当前真实数据库至少包含基础结构 |
| 2 | `memory_items`、`memory_sources` 及记忆索引 | 当前真实数据库只读检查最高版本为 2 |
| 3 | `memory_change_log`、审计索引和操作类型约束 | 已写入代码；测试数据库可通过初始化执行 |
| 4 | `request_fingerprint` 和指纹索引 | 已写入代码；测试数据库可通过初始化执行 |
| 5 | Proposal 表、Proposal 审计表和索引 | 已写入代码；测试数据库可通过初始化执行 |
| 6 | Proposal 应用状态、目标快照、应用结果、`proposal_id` 和应用日志 | 已写入代码；未迁移真实数据库 |

`Database.initialize()` 会在每个迁移及其版本标记之间使用事务保护；测试覆盖了迁移失败时不留下半成品表和错误版本标记的场景。

## 当前已知限制与后续方向

根据当前代码和测试，以下能力尚未实现或未在本档案中假设已经实现：

- 模型辅助的语义记忆提取。
- 向量或语义记忆检索。
- 多用户认证和隔离。
- 后台任务队列、流式输出和多通道确认交互。
- Proposal 的 HTTP API、前端列表和确认页面。
- 多目标或批量记忆纠正。
- 真实生产数据库的 Migration 6 执行与备份/回滚演练。
- npm 不可用环境下的前端 TypeScript 和 Vite 构建验证。

后续开发建议继续保持以下边界：

1. 不让模型输出直接成为正式记忆变更。
2. 不把记忆参考资料当作人格指令。
3. 不绕过来源、状态、canonical key 和审计约束。
4. 对任何真实数据库迁移先做备份、演练和显式验收。
5. 为后续版本建立独立 Git Tag 或至少在 Commit message 中明确版本号，避免再次出现无法按版本精确拆分历史的问题。

## 历史完整性与本档案维护说明

- 本次整理仅新增 `CHANGELOG.md`，没有修改、删除或重写任何 Git Commit。
- 没有执行 commit、push、reset、rebase 或 clean。
- 没有修改业务代码、人格文件、模型配置、API Key、API URL 或 `.env`。
- 没有执行数据库 Migration，也没有修改真实数据库。
- 当前仓库的原始 Git 历史保持不变。
- 新版本应在发布时补充正式版本号、独立 Commit、Tag、迁移状态和真实测试报告，避免将开发阶段报告与 Git 历史混为一谈。
