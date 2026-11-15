# Soulseed 2.9.0-D3-A · 隔离端到端联系链路

本阶段仅提供显式构造、严格 Mock 的集成服务，不接入 `main.py`、后台调度器或真实模型。配置 `autonomous_contact_generation_enabled` 和 `autonomous_contact_handoff_enabled` 均默认关闭。D1 Shadow 不调用此模块，也不读取或写入 D2/D3 候选状态。

## 数据流

1. B 内核给出 `CONTACT` 决策和证据候选。
2. D2 仓库重新构建候选并持久化稳定去重键。
3. 通用预算闸门先预留；之后 D2 在 SQLite 写事务内原子领取候选并预留角色每日请求/消息子预算。
4. `mark_request_started` 再读角色、权限、候选、来源、冷却及预算，成功后才允许调用生成器。
5. C `DraftService` 用服务端角色档案、证据约束上下文、Mock 生成器和显式批准指纹审核草稿。只有完整 Mock collaborator 类型会被 D3-A pipeline 接受。
6. 草稿与候选证据 ID、预算 reservation ID、内容完整性摘要及短过期时间保存在 `autonomous_contact_drafts`。该摘要仅用于检测存储损坏，不能证明事实真实。
7. 再次检查证据后写入草稿。随后第二个 SQLite 事务复查租约、角色归属、最新用户授权、安静/忙碌/暂停、冷却、未读消息、重复主题、来源、每日预算、草稿审核标记和期限。
8. 同一事务调用 `ContactService.create_intent_in_transaction()`，创建允许来源的 `ready` 联系意图与 `proactive_messages` ready 记录，完成 D2 候选并确认消息名额。任何异常整体回滚。
9. 后续站内展示、投递和已读继续完全由 `ContactService.inbox()`、`deliver()`、`mark_read()` 决定。投递时再检查当前授权、角色会话和意图期限。

研究发现只映射到 `life_share` 并关联真实分享意图；共同话题只映射到 `conversation_followup`，并使用候选中的真实用户消息 ID。所有其他来源安全拒绝。未使用 `smalltalk`、`reminder` 来伪装来源。

## 事务及恢复

`ContactService.create_intent()` 保留原有调用方式，现改由 `Database.atomic()` 驱动；共享的 `create_intent_in_transaction()` 不会再次执行 `BEGIN IMMEDIATE`，并要求调用方已处于事务中。D3-A 的候选完成、意图、消息名额、已审核 Mock 草稿消费和站内 ready 消息处在同一个交接事务里。

预算预留先于草稿生成。模型请求启动后异常或重启按结果未知保守扣额，不能自动重试。草稿保存后、交接前发生重启，租约恢复会释放消息名额、取消草稿并将已结算尝试标记失败；候选不会再次发起生成。已过期草稿在恢复时取消。不能确认审核、证据或权限时都失败关闭。

## 通用模型预算阻断项

现有通用模型预算由 JSONL 记录实现，不具备可与自主子预算在 SQLite 事务中原子预留的接口，也不能证明 JSONL 与 SQLite 双账本的跨故障一致性。本阶段的 `MockGeneralBudgetGate` 仅用于模拟“可用/不足”分支，不能用于真实模型。D3-B 真实生成必须等待统一的事务化预算 authority 或一个可证明崩溃恢复及保守扣额的 reservation 协议；不能用当前 JSONL 检查结果替代原子预留。

## 事实审核边界

当前 C 审核器是显式指纹批准的测试替身。D3-A 保留原有隔离 Mock 路径。B0.2 可在隔离构造时传入与管道共享数据库的 `fact_reviews`，要求读取独立事实审核凭证。草稿已有结构化审核记录时，缺少验证器也会阻断交接。`mock_approved` 无法通过该门禁，待审、拒绝、过期或绑定失效的凭证也无法通过。管道使用已持久化的草稿正文进行核验，审核验证与最新授权、运行开关、预算及 ContactService 意图创建共享同一个事务。详细契约见 [主动表达草稿](proactive_expression_drafts.md#b02-结构化事实审核)。

受控渲染和人工决定当前没有接入真实生成或消息投递。B1 的真实模型草稿试验仍需要受控模型适配器、可证明的统一预算与 D2 协议、明确的运行授权和隔离草稿出口。未接入真实人工权限时，模型自由文本保持待审或拒绝。

## 故障与并发契约

- 同一角色同一来源依赖数据库唯一约束，领取依赖写事务串行化；竞争执行器最多一个取得租约。
- 事务中途异常回滚 ContactService 意图、ready 消息、D2 completed 状态和消息额度确认。
- 模型返回后偏好改变、安静时段、暂停/忙碌、生效拒绝、证据撤销、租约失效、重复联系或候选过期均阻断交接。
- Shadow 继续只读运行，不调用本 pipeline、不创建候选或预算记录。
- 本模块目前没有注册 API 或调度循环；只有隔离测试显式构造它，且必须启用两个独立开关。
