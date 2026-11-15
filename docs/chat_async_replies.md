# Chat-C2 异步角色回复

异步 Worker 默认关闭。需要在一次性测试库里体验 Mock 回复时，可以运行 `python backend/scripts/run_isolated.py api --chat-async-mock`。该选项只在 `testing` 环境生效。两个 Worker 轮询 SQLite，并通过同一个 ChatService 处理已保存的用户消息。

## 客户端读取

- `POST /api/conversations/{id}/user-messages` 沿用 C1 的发送契约。成功返回用户消息 ID 和 `server_sequence`，客户端可以立即继续发送。
- Worker 未运行、正在排空或会话未获准时，新提交返回 `503 async_chat_unavailable`，已有提交按原 ID 重放与读取。增量读取中的 `async_chat_accepting` 表明该会话当前是否接收新提交。
- `GET /api/conversations/{id}/user-messages/{client_message_id}` 返回 `role_status`、`reply`、`reply_error_code`、`reply_started_at`、`model_started_at` 和 `reply_lease_until`。完成时 `reply` 含 `id`、`user_message_id`、正文和服务器序号。其余状态的 `reply` 为 `null`。
- `GET /api/conversations/{id}/messages?after_sequence=0&limit=100` 返回 `messages`、`next_after_sequence` 和 `has_more`。刷新后可从 0 重建，随后用返回的游标增量拉取。用 `id` 合并重复读取，用 `user_message_id` 关联迟于后续用户消息保存的回复。
- 增量游标追踪新增消息。已经拉取的用户消息需要按 `client_message_id` 轮询状态，即使增量结果为空。可以用退避间隔并在页面隐藏时暂停轮询。
- 同步 `POST /api/conversations/{id}/messages` 在会话已有处理中的同步请求或未解决的异步任务时返回 `409 conversation_reply_pending`。等待同步请求期间仍可提交 C1 消息。

## 执行与恢复

| 条件 | 状态 |
| --- | --- |
| C1 提交成功 | `waiting` |
| Worker 通过 SQLite 原子认领 | `processing`，记录令牌和租约截止时间 |
| 租约到期，业务逻辑尚未开始 | 回到 `waiting`，允许其他 Worker 认领 |
| 模型调用前发生明确的本地失败 | `failed`，允许会话后续任务继续 |
| 业务开始后租约到期，或模型开始后中断、保存失败 | `unknown`，暂停该会话后续回复 |
| 角色消息、来源关系与状态更新一起提交 | `completed` |

Worker 按会话内用户消息的服务器序号处理，保存回复时检查当前租约。事务只覆盖本地读写。调用模型前先记录 `model_started_at`，因此该时间表示调用可能已发出。Structured Mock 返回明确的格式错误时，ChatService 可以走原有降级路径。连接中断或超时属于未确认结果。

排查 `unknown` 时，通过状态接口取服务器 `message_id`、错误码和执行时间。用该 ID 检查消息的 `user_message_id`、记忆来源、澄清触发或答复记录和已提交的应用日志。先确认旧执行已经结束，再判断哪些副作用已提交。C2 提供只读诊断，待人工处置的任务保持 `unknown`，客户端重发 C1 请求只会取得原消息状态。后续如果引入人工处置，应核对已有副作用并保留审计记录，避免重新运行整个 ChatService。

普通记忆和澄清沿用现有来源与幂等键。C2 在进入副作用逻辑前记录执行开始，崩溃后仅重领尚未进入该边界的任务。启动时，澄清恢复可以核对已有应用日志，未完成的异步澄清交由 C2 状态与人工诊断管理。真实模型接入仍需审核提供方结果核对、`unknown` 处置及停机策略。

## C5 调用记录与排空

迁移 26 为适配器的每次调用生成 `{user_message_id}:{ordinal}`，区分 `structured` 和 `plain`。调用记录区分准备、本地拒绝、已越过 HTTP 发送边界、收到完整响应和结果未知。收到响应后记录 HTTP 状态与可用的供应商请求标识，最终回复的关联与回复落库同事务提交。发送边界只能证明应用尝试发送。若连接中断，调用记录不代表供应商收到、执行或计费。

配置独立的 `SOULSEED_CHAT_OPERATOR_SECRET` 后，管理接口要求 `Authorization: Bearer …`，同时沿用所在环境的身份与角色归属检查。

- `POST /api/admin/chat/drain` 关闭接收和认领，给在途任务最多 `SOULSEED_CHAT_ASYNC_DRAIN_SECONDS` 秒完成，默认 10 秒。之后取消未完成任务并收回提交权。已保存但未开始的任务保留待下次启动领取，结果不明的任务留在 `unknown`。
- `GET /api/admin/chat/replies/{user_message_id}` 查询尝试、回复、阻塞来源和相关记忆、提案、澄清记录。输出使用 ID、状态和时间，省略聊天正文和模型请求内容。
- `POST /api/admin/chat/replies/{user_message_id}/resolution` 接收最新 `snapshot_token`、`decision`、`evidence_reference` 和 `provider_checked`。可能已发送但缺少完整响应时，需先由管理员核对外部证据。证据引用应指向独立记录。快照过期、有活跃租约、提案未核对或与实际回复不符均会拒绝处置。
- `confirm_saved_reply` 只确认数据库中已有且关联正确的回复。`close_without_reply` 记录结束理由并标记失败，保留已提交的副作用与尝试日志。已核对的应用记录保留为已应用，未应用的中断澄清可以关闭为延期。处置与审计同事务提交。

真实开关 `SOULSEED_CHAT_ASYNC_REAL_ENABLED` 默认关闭。Development 试运行必须先完成 [模型预算历史切换](model_budget_cutover.md)，再由启动器传入一个明确会话 ID。切换成功后，同步聊天、联系人和探索也一并使用严格 SQLite 账本。指定会话的 Worker 固定单并发；`SOULSEED_CHAT_ASYNC_MAX_MESSAGES`、`SOULSEED_CHAT_ASYNC_MAX_ATTEMPTS`、`SOULSEED_CHAT_ASYNC_MAX_OUTPUT_TOKENS` 分别限制会话任务、单轮尝试和单次输出。门禁关闭或账本不可用时，消息保存接口会在写入前拒绝。输入继续受 ChatService 的上下文预算限制。
