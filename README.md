**简体中文** | [English](README_EN.md)

# Soulseed

**让每个人都能创造属于自己的、持续成长的数字个体。**

*A World for Every Individual.*

**当前版本：v2.9.1-beta.13 · Windows 私人内测。** Soulseed 是一个持续开发的本地原型，主要面向单用户。项目的长期方向见[产品愿景与决策档案](docs/product_vision.md)。

## Soulseed 是什么

Soulseed 把对话视为数字个体经历的一部分。角色可以记住有来源的信息，在生活活动中形成兴趣与习惯，对同一话题保持自己的看法，也能根据新证据调整。保存的记录区分实际执行的虚拟生活活动、计划、愿望和中断事项。真实网页探索使用独立的来源证据。

## 当前核心能力

| 能力 | 已实现 |
| --- | --- |
| 人格与对话 | 固定角色人格、有界上下文、同步与异步聊天、兼容 OpenAI Chat Completions 的模型适配。Windows 聊天输出上限为 1024 tokens。 |
| 长期记忆 | 带来源的结构化记忆、规则检索、冲突检测、自然澄清与确认后的纠正，保留变更历史。 |
| 自主生活与成长 | 活动计划、生命周期和状态管理。连续经历会改变兴趣倾向，跨日期的重复活动会形成时段习惯。二者都可影响活动选择和表达。 |
| 独立观点 | 角色可以形成与用户不同的看法，跨会话延续，并根据已完成经历或有来源的研究结果修订，保留版本与依据。 |
| 自主探索 | 有界 Agent 支持搜索、读取真实网页、检查正文相关性并保存带引用的结论。它可从已保存的经历、兴趣与研究发现派生新问题，并记录选题来源。 |
| 探索可靠性 | 保守去重区分明显改写与同主题下不同问题。中断保留题目、证据和请求记录，可显式重试。Windows 手动探索和重试使用严格请求额度。 |
| 自主社交 | 可根据近期聊天、角色的生活状态或可分享探索结果产生联系候选。角色可以选择沉默，合格消息进入原会话。Windows 支持后台窗口的原生通知。 |
| Windows 本地运行 | Tauri 安装版按 Windows 用户保存数据，模型配置由 DPAPI 加密，并提供每日请求额度和自主功能开关。 |

## 大肥鱼

大肥鱼是第一个数字个体。她童真、活泼、好奇，也有些淘气。她的[人格资料](backend/app/persona_profile.md)与[验收场景](docs/persona_test_cases.md)用于验证角色在不同对话和经历中的连续性。

## 软件运行与安装

Windows 10/11 x64 是目前主要使用端。安装包供私人内测，由可信渠道提供。请核对随包的 SHA256，再按 [Windows 安装与模型配置](docs/windows-install.md)升级或安装。安装器目前未签名。源码构建见 [Windows 构建说明](docs/windows-build.md)。

网页主要用于本机开发、调试和兼容访问。可按[配置与运行指南](docs/setup.md)安装依赖，再用以下命令以关闭模型的方式启动。开发环境的配置要求见 [Development 运行说明](docs/development_runtime.md)。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-development.ps1 --model-off
```

## 技术架构

**一个数字个体核心，多个交互入口。** Windows 的 Tauri 窗口与浏览器复用 React/TypeScript 界面和本机 FastAPI 角色核心。未来的微信、QQ 等入口也应复用这个核心，目前尚未接入。

- `ChatService` 通过 `ExpressionContext` 组织人格、长期记忆、生活经历、兴趣、习惯和观点。聊天和自主联系共享角色数据与表达边界。
- `LifeEngine` 根据保存的状态选择活动，`LifeScheduler` 推进生活并承载可选的后台探索调度。兴趣和习惯读取已有生活记录，完成一次探索不会直接提高兴趣权重。
- `ExplorationAgent` 在工具和模型额度内执行搜索、读取和证据审核。合格结果可为观点修订、后续选题和探索分享提供依据。自主联系统一处理候选、审核、预算和投递，Windows 为原生通知提供适配。
- SQLite 保存会话、记忆、生活、观点、探索证据和请求账本。后端使用 Python、FastAPI 和 HTTPX，前端使用 React、TypeScript 和 Vite。

## 验证与限制

| 验证层次 | 已知结果 |
| --- | --- |
| 自动测试 | beta.13 的隔离测试覆盖选题与来源、去重、恢复与额度，以及聊天、记忆、生活成长、习惯、观点和自主联系回归。 |
| Windows 实机与模拟 | beta.13 已从 beta.12 覆盖安装并校验用户数据。以隔离数据和本机模拟验证探索、聊天与观点延续，网页由测试替身提供。原生通知以注入的测试事件单独验证。此前 Windows 内测以模拟模型记录过自主联系与原会话投递。 |
| 真实模型与网络 | 历史版本有少量真实手动与后台探索成功案例，例如鲸歌研究。见[技术版本档案](CHANGELOG.md)。beta.13 的新选题、去重和重试尚未在真实服务商与真实网络上重验。 |
| 当前边界 | 去重可能漏掉跨语言或大幅改写的等义问题。重试会创建关联的新任务并重新消耗额度。请求额度按次数计算，实际计费以服务商为准。后台行为要求应用运行并启用相应开关。 |

## 路线图

- 提升长期记忆的准确性、召回和冲突处理。
- 探索可追溯、可调整的长期关系成长。
- 继续改善探索选题、来源质量、去重和失败处理。
- 验证长时间运行、异常恢复和真实模型的表达质量。
- 沉浸式数字世界、外部消息渠道和多角色系统属于长期方向。2.5D 世界暂不进入当前开发任务。

## 开发与贡献

欢迎提交可复现的问题和范围明确的改动。[Issues](https://github.com/KaoruMiyagawa/Soulseed/issues)

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_isolated.py tests
npm --prefix frontend test
```

请用隔离数据库验证，说明使用的是模拟模型还是真实服务商。密钥、用户数据、日志、备份、构建产物和未获授权的素材应保留在仓库之外。

继续阅读：[生活成长](docs/life_growth.md) · [观点连续性](docs/opinion_continuity.md) · [自主社交](docs/autonomous_social.md) · [探索诊断](docs/exploration_action_diagnostics.md) · [版本记录](CHANGELOG.md)。
