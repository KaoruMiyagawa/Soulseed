# 2.9.0-B 自主意图内核：内部 dry-run

核心决策接口位于 `backend/app/autonomous_intent.py`；D1 增加了默认关闭的后台 Shadow 调度，但仍没有注册公开 HTTP 路由、模型或投递入口。直接调用核心接口的调用方须先完成服务端身份授权并进入同一角色的 `character_scope`：

```python
from datetime import UTC, datetime
from app.autonomous_intent import build_snapshot, generate_candidates, dry_run
from app.ownership import character_scope

# database 必须由宿主应用安全配置；测试应使用隔离临时数据库。
with character_scope(character_id):
    snapshot = build_snapshot(database, character_id, now=datetime.now(UTC))
    candidates = generate_candidates(snapshot)
    decision = dry_run(snapshot)
```

快照读取单个角色的已保存完成事件、来源验证的 pending 研究分享、活动状态、偏好权重、带来源的有效记忆、近期对话及联系约束。数据库连接强制 `query_only` 并回滚只读事务；缺少角色、权限、时区或联系偏好时拒绝或保守沉默，不调用会初始化状态的 Repository 方法。一般手工保存的经历标记为“已保存、未独立核验”，不作为真实研究发现。记忆中的用户信息也不自动变成角色兴趣。

首期 `real_discovery` 仅使用已完成探索中引用成功读取、相关页面的 sourced 结论，并要求关联分享仍 pending；分享产生 24 小时后不再形成新候选。`shared_topic` 需要真实用户消息中的明确后续邀请、角色在同一会话中包含相同具体话题的肯定回复，并排除后续用户消息、已处理话题、拒绝和过期项。完全相同话题的既有联系意图会被保守去重；同义改写的语义去重尚未实现。兴趣候选接口当前恒返回空结果，不将活动偏好权重当作真实新兴趣。

`dry_run()` 返回 `Decision`：`result` 为 `CONTACT`、`DEFER` 或 `SILENT`，并包含候选类型、证据 ID、原因码、基本联系条件结果、去重键及可选重试时间。`CONTACT` 只表示对该时刻快照的评估通过，**不是发送授权**；未来接入时必须由 ContactService 在领取与投递时重新校验。调用方不应接受客户端自行构造的快照作为事实依据，也不应记录快照中的私人对话或记忆内容；决策结果本身不含正文。

规则比未来完整系统保守：缺少联系偏好/时区时沉默；需要等待安静时段、忙碌/暂停结束、用户消息、最近联系冷却或用户回复时暂缓。现有联系来源白名单、现有调度行为及投递事务均未改变。持久候选、模型预算、事实复核、语义话题状态、用户可见 shadow 遥测和实际发送均留待后续阶段。

## D1 后台 Shadow Mode

`Settings.autonomous_intent_shadow_enabled` 默认 `false`，周期为 `autonomous_intent_shadow_interval_minutes`（默认 60，范围 5–1440 分钟）。启用后，FastAPI lifespan 启动独立的 `ShadowModeScheduler`；它与 `ContactScheduler` 是不同任务，不调用其 `tick()`，也不接触 C 阶段草稿生成器。全局 `contact_enabled=false` 时诊断为 `SILENT/global_contact_disabled`。

每轮仅枚举最多 100 个 owned 角色，对每个角色单独进入 `character_scope`，运行只读快照、候选生成及确定性决策。SQLite 读取移到工作线程，避免同步查询占用事件循环。每角色异常转换成 `SILENT/shadow_evaluation_failed` 摘要并继续其他角色。诊断缓存在进程内，仅保留每个角色最新一项和有限的角色数；包含时间、决策、候选类型、最多 8 个证据 ID、原因码、去重键、重试时间及可选错误类型，不包含正文或记忆。D1 自身不写数据库；D2 增加的表不会被 Shadow 读写。诊断在进程退出后消失。

Shadow 的 `CONTACT` 仍只是决策观测值，不授予调用 `ContactService` 的权限。实现刻意未复用 ContactScheduler 时钟，以隔离异常和生命周期；关闭 Shadow 开关时 lifespan 不创建任务。独立 Shadow 检查可用 `SOULSEED_AUTONOMOUS_INTENT_SHADOW_ENABLED=true` 显式启用，测试应仍通过隔离启动器运行。当前机制不提供重启后的诊断恢复、跨进程互斥或语义重复聚合，这些属于 D2 评估范围。

## D2 持久候选与安全预算基础

数据库迁移 19 新增 `autonomous_contact_candidates` 与 `autonomous_contact_reservations`。候选的 `id` 是 B 的角色范围稳定去重键；数据库同时约束 `(character_id,candidate_type,source_key)` 唯一。`evidence_json` 只保存最多 8 个 `{kind,id}` 引用，不复制消息、记忆或研究正文。写入前重新从只读快照重建 B 候选，写事务内再次验证角色归属、事件/分享/探索状态和消息来源。

候选状态为 `pending → claimed → deferred|completed|expired|cancelled|failed|outcome_unknown`，`deferred` 可在 `retry_after` 到期后重新领取。领取使用 `BEGIN IMMEDIATE`、租约 token 和唯一预留记录；SQLite ownership 触发器要求当前角色 scope 与 owned 角色匹配。D2 尚未启动候选周期任务。

每次领取在同一事务预留自主模型请求额度与普通消息额度。额度分别默认为每角色每 **UTC 自然日** 2 次请求预留和 1 个消息名额；角色时区变化不会移动重置边界。`reserved` 计入上限；只有 `request_status='reserved'` 且尚未标记请求开始时才允许释放。标记开始后，超时或进程在结果落库前退出会转为 `outcome_unknown`，请求额度保持消耗，候选不可自动重试。明确成功记录为 `settled`；成功结果未持久化时租约恢复将失败关闭，不重新花费额度。已有模型 JSONL 台账仍服务原有调用方；未来自主调用需要写入 JSONL 作审计，但扣额权威为 SQLite 子账本。

独立开关 `autonomous_contact_generation_enabled` 默认关闭。请求限额、消息限额、候选租约时长和主题去重时间可由服务端 Settings 设置。`AutonomousIntentStateRepository.from_settings()` 仅构造状态服务，不启动调度器或模型。API 调用方必须绑定可信角色 scope；缺少权限、联系偏好、时区或有效证据时不能领取。

角色人格的权威来源是服务端代码管理的 catalog；首个条目从 `persona_profile.md` 的“核心气质”章节读取，风格集合及显式版本号在 catalog 中由服务端配置。`resolve_trusted_persona()` 只接收角色 ID，校验当前 scope 和 owned 状态，不读取候选或聊天来构造人格。当前未登记的角色失败关闭。

`recheck(candidate_id, character_id, token, stage=...)` 支持 `pre_generation`、`post_generation` 和 `before_contact` 三个节点，每次都在最新写事务里重新校验归属、用户授权、安静时段、忙碌/暂停、冷却、回复状态、候选来源有效性、等价联系意图及预算日。实际模型入口必须调用 `mark_request_started()`；它在同一事务重复权限复查并把额度改为 `started`，单独调用 `recheck(pre_generation)` 不构成授权。`before_contact` 只是复查结果；**D2 没有 ContactService 适配、没有创建联系意图、没有创建消息**。D3 必须把最终复查、候选预留消费和现有 ContactService 意图创建放入同一 SQLite 事务，并以 `deliver()` 作为实际投递权威。

主题去重将 NFKC、大小写折叠、空白和标点标准化后作精确指纹，并保守比较近期角色候选与联系主题。它不能识别同义改写。事实来源仍用结构化引用复核；来源存在或文本哈希都不能证明模型整稿在语义上忠于证据。模型生成器尚未接入，未知事实、审核拒绝或复查失败均须停止。
