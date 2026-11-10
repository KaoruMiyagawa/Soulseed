# SQLite 模型请求预算内核（B0.1a）

`backend/app/model_budget.py` 提供独立 `ModelBudgetService`；迁移 21 新增 `model_budget_windows` 和 `model_budget_requests`。本阶段没有注册模型、API、后台任务或真实历史导入。当前聊天、探索、既有主动联系和 D3-A Mock 管道仍未使用该内核，不能宣称全局硬额度已生效。

宿主用可信配置构造服务：`global_limit=None` 保留未配置全局上限的含义；`source_limits` 的键为服务端业务来源，其额度按角色和来源计算。全局额度按所有角色及来源合计。同一 UTC 日冻结配置，配置不一致的执行器失败关闭；调整配置须在后续阶段明确设计，不能通过另一个服务实例变更当日上限。

所有操作只执行短 SQLite 事务，不调用网络。`reserve(request_id, character_id, source, autonomous_reservation_id=None)` 在同一 `Database.atomic()` 内检查门禁和所有适用额度并写唯一请求。重复 ID 返回已有状态与 `changed=False`，绑定角色、来源或 D2 关联不同则拒绝；旧 ID 跨日仍指向旧记录。`mark_started` 仅首次 `changed=True` 表示可进行首次外部尝试；调用方不得把重复返回当作重试许可。禁止在外围持有写事务时执行网络操作。

状态为 `reserved → started → completed|failed|unknown`，或者 `reserved → released`。相同目标的重复状态提交幂等，不允许从终态复活或在开始后释放。`complete(success=False)` 记录明确失败并保留消耗；超时调用 `mark_unknown`，额度同样保留。`release_unstarted` 仅允许持久状态仍为 reserved，此时协议保证所有调用方在任何外部尝试前必须先写 started。

`recover(character_id)` 在角色作用域内原子处理已过期租约：reserved 被释放并永久封住旧 ID；started 转 unknown 并保留原始日占用。完成、失败和未知请求不会自动恢复调用。`usage_snapshot` 只读，返回全局合计和当前角色按来源的占用，不含请求正文。账本不保存 Prompt、聊天、响应正文或密钥，也不表示实际供应商费用。

每个 UTC 日的历史兼容门禁默认关闭。`set_compatibility_gate(day, ready=...)` 是受信宿主将来使用的显式切换接口，本阶段只在临时测试中调用。它不构成历史已导入的证据，也不推断真实余额。reserve 及首次 mark_started 均检查该门禁；读取快照或重复 reserve 不会授权网络请求。B0.1b 必须先证明历史完整性再打开门禁。

将来关联 D2 时，传入实际自主 reservation ID，并使用该行的 `request_key` 作为统一请求 ID；服务验证角色、ID 和 reserved 状态，数据库唯一关联防止一条 D2 预留重复绑定。可复用同一个 Database.atomic 将 D2 领取和统一 reserve 合并；本内核不会再次更改 D2 的计数，两本账分别表达自主子维度与统一请求维度。B0.1b 需统一来源命名和预算权威、协调生命周期并防止旧路径重复扣账。

后续事实审核接收持久请求 ID、请求状态及业务关联；预算通过不表示事实审核通过、联系权限通过或供应商已收费。
