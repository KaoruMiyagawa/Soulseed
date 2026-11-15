# 2.9.0-C 主动表达草稿：Mock 内部接口

本阶段不注册 API、调度器、真实模型适配器或投递流程。`proactive_expression.py` 只接受显式创建的 `MockDraftGenerator` 和 `MockEvidenceReviewer`，不会读取 Development 模型配置或调用模型。生成失败、安全审查失败、证据失效和重复消息都返回无正文的 `rejected`；`SILENT`、`DEFER` 返回 `suppressed`，不会调用生成器。没有无证据的替代文案。

宿主先进行身份鉴权，服务端绑定角色并通过可信配置创建 `PersonaRegistry`；`PersonaProfile` 的角色 ID、人格描述和允许的风格代码分开配置，不由通用组件写死大肥鱼。当前 mock 默认选择角色配置的第一个风格，也可以显式选择该角色允许的其他风格；通用风格片段是受限的测试素材，不代表已经完成自由语言模型表达或自然度验收。

```python
with character_scope(character_id):
    snapshot = build_snapshot(database, character_id, now=clock())
    decision = dry_run(snapshot)
    candidate = next((c for c in generate_candidates(snapshot)
                      if c.dedup_key == decision.dedup_key), None)
    # 仅在隔离测试中构造 Mock；审核指纹应针对确切证据逐项准备。
    context = build_draft_context(database, character_id, candidate, decision, profile, now=clock())
    review = MockEvidenceReviewer(frozenset({context.options[0].fingerprint(character_id)}))
    registry = PersonaRegistry((profile,))
    result = DraftService(database, MockDraftGenerator(None), review, registry, clock=clock).generate(
        character_id, candidate, decision)
```

`DraftService.generate` 为每次生成重新构建 B 阶段只读快照，要求传入候选、证据 ID、角色和 `CONTACT` 与当下决策一致；Mock 返回后再次检查，证据变化或权限被撤销则拒绝草稿。`ExpressionContextBuilder` 组织有界、明确为参考数据的经历、当前活动、必要记忆、近期对话和边界；仅纳入与候选消息有明确来源关系的记忆。近期已投递消息以哈希用于精确重复过滤，未回复状态继续由 B 层防打扰规则阻止生成。所有数据仍须按服务端角色范围读取，身份/注册表不是向外开放的授权 API。

生成器返回绑定 `style` 和 `evidence_key` 的 `DraftPlan`。默认 Mock 可拼接该角色允许的预设表达片段；Mock 也可提出不同的完整文案，但文案必须包含服务端重新读取的确切结论或明确话题，并且证据指纹和完整草稿指纹都经独立的 Mock 审核显式批准。结果以 `DraftResult` 返回状态、草稿、证据 ID、来源 URL 或有限错误码。真实研究要求完成的来源关联与成功读取的相关网页；主题必须有真实对话锚点。未核验的手工经历、想象和愿望不能作为事实片段。`MockEvidenceReviewer` 默认拒绝，测试必须显式批准角色、内容和来源共同组成的指纹；它是测试替身，不是事实审核服务。

结构化计划、来源验证、复查和整稿审核比提示词单独约束更严格，但**不能证明**保存的研究结论一定被网页蕴含，也不能验证用户叙述是否真实；Mock 的人工预置审核与少量表达片段不能代表真实模型的审核或表达能力。关键词拒绝只是补充，不是事实验证器。进入下一阶段前，需要决定独立事实审核实现、可信角色配置的存取与审计、可用的表达多样性评估、额度/开关和预算预留、与现有 ContactService 的原子门禁及投递并发策略。不得直接将本阶段草稿接到正式主动消息表。

## B0.2 结构化事实审核

`FactReviewService` 通过迁移 23 的 `proactive_fact_reviews` 保存独立审核凭证。`ReviewDraft` 包含角色、候选、草稿 ID 和确切正文。服务从当前角色所拥有的数据库候选读取证据，在同一事务中重算证据版本。凭证保存审核 ID、三个绑定 ID、正文 SHA-256、证据 ID 与各记录的版本 SHA-256、策略版本、审核方式、结果、原因码、审核时间、有效期和人工审核者标识。正文、来源摘录和聊天内容不写入审核表。摘要用于版本绑定。

- `review_controlled(draft)` 只批准 `render_research_completion()` 的固定文本，且候选必须对应仍有效、已有相关来源的完成查阅记录。固定文本报告本地查阅状态并表达继续了解的愿望，未使用来源中的研究结论、角色想象或用户文本作为插值。`sourced`、URL 和已有 Mock 审核均不构成结论支持关系的证明。
- `review_free_text(draft)` 对有有效证据的合法自由文本签发 `REVIEW_REQUIRED`。证据缺失或文本不合约时为 `REJECTED`。模型自评、伪造的批准标记、与受控模板相同的模型正文均不产生自动批准。
- `decide_human(review_id, draft, operation_context=..., approve=...)` 通过宿主注入的 `HumanReviewAuthorizer` 验证当前操作上下文与角色授权。只在 `testing` 下接受该接口。应用的角色登录没有提供审查员权限，也没有注册人工批准 API。隔离测试使用独立的操作授权替身。

一个草稿 ID 只对应一条凭证。`REVIEW_REQUIRED` 只能由受授权的人工决定进入 `APPROVED` 或 `REJECTED`。终态不可重写，重复操作不会延长有效期。有效期最多从签发起五分钟，且不能超过候选期限。更改正文、证据版本、角色、候选、草稿或策略版本后，凭证校验会失败。已过期或已变更的草稿需要新草稿 ID 和新审核。签发和人工决定各自拥有独立事务。

`can_auto_handoff` 表示记录的资格。交接前用 `verify_approval_in_transaction()` 在交接事务中重读持久凭证、角色、候选及当前证据，并核验正文与有效期。通过后仍受 D2/D3-A 与 ContactService 门禁约束。该服务目前无法判断网页是否蕴含研究结论、自由转述是否保留限定词，或用户叙述、因果、数字、比较、计划与想象是否被正确表达。这些表达需要人工逐条核验或拒绝。

## B1a 独立草稿实验

在项目根目录执行以下任一命令。每条命令输出一份本地 JSON 报告。

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_draft_experiment.py
.\.venv\Scripts\python.exe backend\scripts\run_draft_experiment.py --case research --mock natural
.\.venv\Scripts\python.exe backend\scripts\run_draft_experiment.py --case research --controlled
.\.venv\Scripts\python.exe backend\scripts\run_draft_experiment.py --case shared_topic --mock repeat
```

Python 宿主可通过 `open_draft_experiment(case, mock_transport=...)` 创建一次性实验，在作用域内调用 `await experiment.generate()` 或 `experiment.render_controlled()`。可选样本为 `research`、`curiosity`、`shared_topic` 和 `insufficient_evidence`。所有样本均为合成材料，角色人格只从服务端人格目录读取。未指定 `--mock` 时不构造适配器。实验不接受外部数据库、聊天、人格、环境配置或任意传输输入。

每次运行创建自己的临时 SQLite。它只有角色身份及预算相关表。共享预算外键所需的 D2 表占位禁止插入。消息、候选、记忆、探索和事实审核凭证表均不存在。实验不实例化 ContactService、D3-A 或调度器。它用两份新建的空样本日志验证隔离账本的初始门禁。每个 `run_id` 固定一个 `request_id` 和一个 `draft_experiment` 额度位，预算服务在 MockTransport 运行前写入 `started`。超时或网络异常保守计额为 `unknown`，同一 ID 无法再次执行。报告保留预算结果，退出实验作用域后清除临时数据库。这是单次实验预算。

实验复用 B0.2 的文本分类规则。模型只返回 `draft` 和样本 `evidence_ids`，引用 ID 经过白名单检查，模型附加的自评不参与审核。合法自由文本为 `REVIEW_REQUIRED`。无有效证据、无效引用、明显催促或亲历话术、完全重复的样本文案会被拒绝。规则不能识别任意隐含断言或判断研究结论是否受来源支持。`unsupported_facts: null` 表示无法判定。`APPROVED` 仅适用于服务端固定渲染且明确标为合成样本的查阅演示。报告始终带有 `review_scope: experiment_only` 和 `not_delivered: true`，不签发任何交接凭证。

### 一次真实草稿试验的前置条件

未来需要单独的仅草稿执行器和可信操作授权。授权应绑定一次性 `run_id`、角色、输入快照、允许的服务商端点和模型、调用上限及失效时间。执行器须在访问凭据或网络前校验并消费授权，持久记录请求预留和 `started`，确保未知结果不会重试。真实试验还需可恢复的独立账本及预算验收、受限的出站端点、可对外发送的最小样本和独立的凭据来源。真实模型的输出仍进入实验报告，且自由文本保持待审或拒绝。B1a 的共享适配器仍只接受 `testing` 和 `MockTransport`。

将来把真实自主草稿生成接到 D2 时，应以同一 `request_key` 和 reservation ID 关联两套预算。D2 领取和统一预留需原子提交，两边的 `started` 需在复查权限后原子推进，结算、未知结果和租约恢复也需一致处理。B1a 独立实验没有 D2 reservation，不能作为该联合协议的验收依据。应用级切换仍须满足 [历史日志和旧写入屏障](model_budget_cutover.md#真实切换前置条件)。

## B1b 一次性 HTTPS 草稿实验

**B1b-C 阻断状态：** 模型侧关闭思考能力已有证据，但 SiliconFlow 对指定参数的实际执行兼容性尚未验证。[DeepSeek 模型模板](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/chat_template.jinja) 在未显式设置 `thinking_mode` 时，将 `enable_thinking=false` 映射到 `chat`；这不证明 SiliconFlow 将请求参数传递给该模板或在其部署中关闭思考。[SiliconFlow API 文档](https://docs.siliconflow.cn/docs/api/chat-completions-post)列出该参数适用于大多数推理模型，但未具体确认此模型。关闭思考后的额外推理 token 和实际费用也无法由模板确认。`--real-run` 当前在读取凭据、账本或网络之前拒绝执行；以下步骤仅供将来解除阻断后的审查。实验请求配置固定模型，增加 `enable_thinking=false`，超时为 30 秒，崩溃恢复等待 60 秒。授权绑定新的请求配置版本，旧授权不再匹配。不得将 Mock 验证当作真实模型参数兼容性的证明。

默认入口仍为离线实验，`--mock` 使用原有 MockTransport。`--plan-real` 只生成未签名的授权字段并初始化专用账本。真实入口是 `--real-run RUN_ID`，从当前用户目录下的 `.soulseed-draft-experiments/grants/<run_id>.json` 读取授权。CLI 的参数只选择已经签名的运行 ID，不能覆盖授权中的角色、样本、端点或模型。`--inspect-run RUN_ID` 只检查或恢复账本状态。

首次实际试验须经独立审查与明确授权，按以下顺序操作。

1. 在独立的操作环境配置 `SOULSEED_DRAFT_EXPERIMENT_ENDPOINT` 和 `SOULSEED_DRAFT_EXPERIMENT_MODEL`。当前代码允许的完整端点为 `https://api.siliconflow.cn/v1/chat/completions`。由操作方确认选定模型支持本实验的 JSON 对象输出及 `max_tokens` 参数，并审查将发送的合成样本与人格文字。
2. 执行 `python backend/scripts/run_draft_experiment.py --plan-real --case research` 取得未签名字段。授权绑定 HTTPS 用途、账本 ID、运行 ID、角色、样本类型、实际提示内容的 SHA-256、人格版本、端点、模型、请求和输出上限以及 UTC 签发与到期时间。默认授权窗口十分钟，验证器最多接受十五分钟。
3. 核对未签名计划后，在可信离线操作终端设置独立的 `SOULSEED_DRAFT_EXPERIMENT_GRANT_KEY_HEX`（随机 32 字节的 64 位十六进制值），然后执行：

   ```powershell
   .\.venv\Scripts\python.exe backend\scripts\sign_draft_experiment.py <plan.json 的路径> --confirm <run_id>
   ```

   命令还会要求再次键入完整 run_id。签发器不访问网络、不读取 API key，也不执行实验；签名前会用当前服务端人格、允许的端点与模型重新计算提示摘要，并核对账本 ID、请求上限、角色、样本和有效期。它只在 `grants/<run_id>.json` 不存在时独占写入 HMAC-SHA256 授权。不要把签发密钥写入命令行参数。测试签名使用独立 Mock 用途，真实入口会拒绝。
4. 确认授权文件所在目录和文件 ACL 只允许授权操作者访问，再由用户单独设置 `SOULSEED_DRAFT_EXPERIMENT_GRANT_KEY_HEX` 与 `SOULSEED_DRAFT_EXPERIMENT_API_KEY`，并明确执行：

   ```powershell
   .\.venv\Scripts\python.exe backend\scripts\run_draft_experiment.py --real-run <run_id>
   ```

   签发命令不会自动执行这条命令。凭据不会从应用 `.env` 或普通模型配置加载。执行器在授权验证及持久预留之后才读取模型凭据。代码会拒绝重解析点、符号链接和多硬链接文件。在 Unix 上要求当前用户拥有且无组或其他用户权限。

账本位于上述专用目录的 `ledger.sqlite3`，只保存实验身份、请求元数据和摘要。SQLite 原子预留时即消耗运行 ID，在 HTTP 请求钩子里持久写入 `started` 后才能发送。已预留的授权无法重放，即使凭据缺失使请求记为 `released`。超时、取消和无法确认的网络结果记为 `unknown`。崩溃后留存的 `started` 可在 60 秒后通过 `--inspect-run` 恢复为 `unknown`。重建账本会产生不同的账本 ID，原授权不再匹配。运行授权期间须保留原账本，禁止恢复较早的账本副本。

每份授权最多触发一次 HTTPS POST，请求 JSON 上限为 8 KiB，发送的 `max_tokens` 为 256，响应读取上限为 16 KiB，HTTP 等待上限为 30 秒。禁用自动重试、重定向和环境代理。SiliconFlow 文档列出 `response_format: {"type":"json_object"}`；其 JSON 模式说明称目前平台提供的大语言模型支持此格式，同时要求处理 JSON 截断。官方 Chat Completions 文档说明，推理模型的 `max_tokens` 可能不包含思维链 token，因此 256 不能作为总生成 token 或费用上限；实际计费、推理 token 以及单价必须另行核对。超时可能令已发请求进入 `unknown` 并消耗唯一名额；不得自动重试。若要求硬金额上限，须由服务商侧额度或专用账户限制提供。

B1b 复用 B0.2 分类和合成样本，输出只进入本地实验报告。自由文本为待审或拒绝。虚构亲历、催促和重复字段是有限信号，自然度、机械化程度和无法确认的事实由人工评估。实验账本只包含两张实验元数据表，且执行器不调用 ContactService、D3-A、候选或探索服务。独立额度不与 D2 联合记账，也无法授权自动联系。共享适配器的 `testing` 和 `MockTransport` 限制保持有效。
