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
