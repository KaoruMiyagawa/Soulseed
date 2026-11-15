# 统一模型预算：调用路径与切换

B0.1a 新增独立 SQLite 预算内核，接口见 [model_budget_kernel.md](model_budget_kernel.md)。B0.1b 将该内核接入共享适配器，先在 MockTransport 下验证。B0.1c 加入历史导入器。C6 允许经明确停机和历史核对后，把 Development 的全部模型入口切换到同一账本；应用默认仍使用 Legacy，真实异步聊天和严格模式默认关闭。

| 入口 | 调用路径 | 当前额度 | 时间与请求标识 | 统一账本覆盖 |
| --- | --- | --- | --- | --- |
| 普通聊天及结构化聊天 | `ChatService` → `OpenAICompatibleAdapter` | `model_daily_request_limit` 可选；通用 JSONL | UTC 日；配置日志时生成请求 UUID | 未覆盖 |
| 既有模型式主动联系 | `AdapterContactGenerator` → 共享模型适配器 | 同普通聊天的通用配置 | 同上 | 未覆盖 |
| 探索 Agent | `reply_action` → 单独构造的模型适配器 | 探索专项限制，默认每日 8；独立探索 JSONL；复制设置时覆盖通用限制 | UTC 日；每次请求 UUID，附 run ID 和步骤 | 未覆盖通用额度 |
| 新自主联系 | `AutonomousContactPipeline` → Mock 草稿 | D2 SQLite 子额度及测试用 Mock 通用额度 | UTC 日；D2 request key | 没有真实模型入口 |
| Shadow | 只读候选与决策 | 无模型请求 | 不适用 | 不适用 |

## 严格模式调用契约

隔离测试仍使用 `testing` 数据库与 `httpx.MockTransport`。Development 严格模式要求配置、预算数据库均为 `development`、显式打开 `SOULSEED_MODEL_BUDGET_STRICT_ENABLED`，并且当日历史切换记录处于 `strict`。模型适配器在每个请求前预留同一 SQLite 账本额度；C5 Worker 的请求 ID 使用持久 `attempt_id`，普通同步聊天、已启用的联系及探索请求使用各自服务端生成的 UUID。启用后所有模型入口均走该账本，Legacy JSONL 写入路径被关闭。

`reply`、`reply_structured`、`reply_action` 接受仅由服务端构造的 `budget_context=ModelBudgetContext(request_id, character_id, source, utc_day)`。`source` 用于选择服务端策略，额度值由注入的 `ModelBudgetService` 决定。`utc_day` 必须与预算时钟一致，角色必须与 `character_scope` 一致。调用方应从持久业务身份派生全局唯一 ID。聊天可用已保存的用户消息 ID 加生成阶段，探索可用运行 ID 加持久步骤序号，联系可用意图 ID 加持久尝试标识。聊天的结构化调用和普通回退属于两次预算请求，结果未知的调用不得自动回退或换 ID。业务方尚未传播这些 ID。

首次预留成功后，HTTP 请求钩子在 transport 执行前持久提交 `started`。重复 ID 和重复 `started` 均无法执行 transport。适配器拒绝在调用方已有预算写事务时发送请求。严格路径最多执行一次 Mock HTTP 尝试，跳转和自定义客户端重试均不启用。HTTP 2xx 记 `completed`，其他已知 HTTP 响应记 `failed`，响应体的业务校验随后执行。超时、取消或收不到完整响应时记 `unknown`，额度继续占用。发送前失败且账本仍为 `reserved` 时记 `released`，旧 ID 仍被封住。写入失败留下的过期记录由 `recover(character_id)` 处理。SQLite 是权威，JSONL 审计可能缺行，审计 I/O 失败不触发重发。

## 历史记录和切换

通用日志的 `started` 记录适配器生成的 UUID、`day`、`at` 和模型等诊断元数据。`finished` 带同一 ID 和 HTTP 或网络结果。探索专项日志还记录 run ID 和步骤，可能追加 `http_started` 和 `action_validation`。每个 ID 只计一次预留。`started` 是本地占额，`http_started` 仅表示进入 HTTP 尝试，两者均无法证明供应商收取费用。`finished` 的 2xx 归为 `completed`，其他 HTTP 状态归为 `failed`。只有预留、超时或网络失败均保守归为 `unknown`。这些状态都占额度。

通用日志没有可靠的角色或聊天/联系来源，探索日志也没有可靠的角色字段。受信宿主必须从独立的完整请求清单提供角色及来源归属。根据 JSONL 本身生成清单无法证明记录完整。未配置通用日志、缺少清单、归属不明、孤立结果、跨文件 ID 冲突、缺字段和截断都阻止门禁开放。相同文件中，同一 ID 的相同预算事件去重，结果或身份冲突拒绝。开始时间转 UTC 核验 `day`，跨日结果仍计入原预留日。

```python
cutover = ModelBudgetCutover(
    service,
    files={"general": general_jsonl, "exploration": exploration_jsonl},
    inventory={"request-id": LegacyIdentity("big_fish", "chat")},
)
cutover.begin_drain()
cutover.seal()
report = cutover.import_history()
```

在测试中，`service` 使用临时 SQLite，日志位于测试根目录。Development 使用固定数据库，并要求 `Database` 已通过该固定路径的受保护访问校验；日志必须是仓库外的 JSONL。每个新版 Legacy 调用通过 `with cutover.legacy_request():` 持有数据库登记，覆盖预留、HTTP 请求、结果审计的完整周期。切换器按以下顺序推进。

1. `legacy → draining` 原子停止新登记。已登记写入者可以完成。活动登记未清空时无法封存，进程丢失后的登记不会因为时间到期而自行消失。
2. `draining → sealed` 在没有活动登记时记录文件集合、相对路径、字节数、SHA-256、请求清单摘要和 UTC 截止时间。空文件也必须显式存在并纳入清单。
3. `sealed → strict` 比对封存内容、匹配完整清单、校验角色和 UTC 日期，然后在同一 SQLite 事务内导入预算元数据并开启当日门禁。提交前再次比对封存内容。报告提供处理、唯一请求、重复、未确认、未知结果和写入数量，以及失败原因和开门资格。重复导入会校验已保存的历史行，新增计额为零。
4. 导入中断回滚写入和门禁。重启后可以重试同一份封存输入。已开放后的显式复核发现冲突，会在同一事务内撤销门禁并进入 `invalidated`。`sealed`、`strict` 和 `invalidated` 都禁止重新登记 Legacy 写入。封存日期变化或材料损坏需要重新审查，模拟器没有自动重置或跨日开放操作。

迁移 22 保留账本占用，并撤销旧布尔门禁。门禁开启必须由协调器在验证事务内授权，普通调用 `set_compatibility_gate(day, ready=True)` 会失败。初次导入遇到已有统一账本请求时拒绝，避免将未核对的两套消费记录相加或覆盖。

Development 切换使用 [cutover_development_model_budget.py](../backend/scripts/cutover_development_model_budget.py)。它要求调用者提供完整、独立核验的请求清单和仓库外的字节一致数据库备份，并明确确认固定 Development 数据库和所有 Legacy 写入者都已停止。脚本先关闭旧写入登记，再封存两份 JSONL、校验清单、导入额度并打开当天门禁；任何缺项、未知身份、未知结果材料错误、在途写入、额度政策不一致或 UTC 日期变化都会保持关闭。脚本不会从 JSONL 自行推断完整身份清单。运行前必须完成数据库备份和旧进程停机；如果脚本在 `draining` 或 `sealed` 阶段失败，Legacy 会继续被挡住，需先诊断并按人工流程恢复，不能直接重开。

项目启动器的默认路径不变。历史切换成功、每日额度策略核对一致后，才可用 `--async-chat-real-conversation <会话ID>` 请求一个 Development 会话的单 Worker。该开关同时把同步聊天、联系人和探索适配器切换到统一 SQLite 账本。会话消息上限、每条消息模型尝试上限及每次输出 Token 上限继续由 C5 配置控制；全局每日请求上限是 28（普通调用 20、探索 8）。严格账本关闭或数据库异常时，请求在 HTTP 发送前被拒绝。

停用异步 Worker 时，使用启动器 `--strict-model-budget` 保留严格预算，让原同步 `/messages` 继续使用共享账本。不要恢复 Legacy 模式或删除/改写切换记录。若要重新回到 Legacy，必须另行执行对切换后 SQLite 请求与旧配额连续性的审计及正式反向切换；当前没有自动反向切换。

## 真实切换前置条件

屏障登记只约束遵守协议的执行器。现有 `ChatService`、`AdapterContactGenerator`、探索 Agent 和旧二进制没有登记，持有日志句柄的旧进程仍可直接写入。字节复核能发现已发生的修改，无法证明复核完成后没有旧写入。真实切换须先安排受控停机，并独立确认所有旧进程、后台任务和在途请求均已退出，相关句柄已关闭，启动器和回滚入口均无法重新启动绕过屏障的版本。还需有覆盖普通聊天、结构化聊天、既有联系与探索的独立请求清单和归属证据。历史范围无法证明时保持关闭。回滚需要协调关闭新入口、核对切换后的 SQLite 记账并验证旧额度连续性，不能直接恢复旧写入。

`ModelBudgetContext` 保留 `autonomous_reservation_id`。未来使用 D2 行的 `request_key` 作为统一请求 ID，并传入实际 reservation ID。当前 D2 领取、开始和恢复分别在业务状态机中执行，统一账本的外键绑定只验证预留身份，无法原子推进两边的开始状态。严格适配器拒绝 `autonomous` 或带 D2 关联的请求。接入时应共享同一个 `Database.atomic()`，将 D2 领取与统一预留合并，并在另一个短事务中完成最新资格复查和两边的 `started` 提交。结算、释放和租约恢复也应联合更新。网络调用只能发生在开始事务提交之后。

事实审核阶段需要接收预算协调器返回的持久请求 ID 和状态；审核结果需另行绑定角色、草稿摘要、证据内容版本和过期时间。预算通过不构成审核或发送授权。
