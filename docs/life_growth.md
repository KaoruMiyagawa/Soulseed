# Life growth

The first growth pass reuses `life_preferences` and `life_preference_changes`; it adds no habit table or scheduler. When LifeEngine completes a saved activity with a recorded positive or negative feeling, it nudges that activity's existing weight. The same weight remains the input to the existing activity chooser.

`LifeRepository.preference_growth()` is a read-only, character-scoped view over completed events and their `completed_feeling` weight changes. A single event is never presented as a lasting interest. Repeated changes are summarized as forming, established, waning, recovering, cooling, or mixed. The summary retains recent direction, so a character can change over time. The current mood remains separate and temporary.

ChatService and the shared proactive expression context receive this summary. Autonomous social decisions receive the same role-owned summary alongside mood, activity, user memories, and interaction context. User memories are not used to infer the character's own interests. The persona's stable traits remain in the existing persona configuration; growth adds experience-based tendencies without replacing them.

The summary is bounded to recent saved preference changes and is re-derived after restart.

## 生活习惯

`LifeRepository.habits()` 从角色最近最多 1,024 条已完成的生活事件中推导时段习惯。使用事件发生时间和角色当前时区，按当地日期去重。每天同一时段做多次同一种活动只计一天。时段为早上 05:00–12:00、下午 12:00–18:00、晚上 18:00–23:00、深夜 23:00–05:00，结束时间归入下一时段。

每种可由生活引擎安排的活动，按时段查看最近 8 个有相关记录的日期。同一时段做了其他活动，或同一种活动改在另一个时段做，都可作为变化依据。至少 3 个匹配日且占比过半或等于一半时视为正在形成，达到 5 个匹配日且占比至少 62.5% 时视为稳定。已稳定的模式跌出门槛、且前面的观察窗口仍有稳定记录时视为减弱。无记录的日期不参与计算。重启从原始记录重新推导。

稳定习惯只在相应时段给 `LifeEngine.choose()` 加最多 0.24 分，减弱习惯最多 0.06 分，按匹配比例计算。已有的兴趣权重、近期重复惩罚和计划优先级仍参与各自的决策。推导直接读取 `life_events`，无需迁移或额外存储。

普通聊天和共享主动表达的 `ExpressionContext` 同时提供兴趣和习惯，最多传入 4 条习惯。模型可以在相关话题中提及稳定习惯，不能据此声称某次活动已经发生或必然会发生。
