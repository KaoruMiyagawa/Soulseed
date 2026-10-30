# Soulseed V1.0-A

Soulseed 是一个本地运行的、具有独立身份与长期记忆基础的数字个体系统。网页是交互入口，不是系统本身。本版本保留“大肥鱼”这一既有角色名与视觉方向；当前仓库初始状态未包含更详细的角色资产，后续可在不改动核心接口的前提下接入。

## 环境要求

- Python 3.11+
- Node.js 20+ 与 npm（当前环境检查到 Node 24，但未发现 npm）
- 硅基流动（SiliconFlow）API Key

## 安装与配置

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Copy-Item .env.example .env
Set-Location frontend
npm install
Set-Location ..
```

编辑 `.env`，至少填写 `SOULSEED_MODEL_API_KEY`。当前默认配置为硅基流动兼容接口和 `deepseek-ai/DeepSeek-V4.1-Flash`。`.env` 已被 Git 忽略，密钥不会提交。

硅基流动配置变量：

```text
SOULSEED_MODEL_BASE_URL=https://api.siliconflow.cn/v1
SOULSEED_MODEL_API_KEY=你的硅基流动密钥
SOULSEED_MODEL_NAME=deepseek-ai/DeepSeek-V4.1-Flash
```

## 启动与停止

在项目根目录打开两个终端：

```powershell
# 终端 1：后端 API（127.0.0.1:8001）
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001

# 终端 2：前端（127.0.0.1:5174）
Set-Location frontend
npm run dev
```

访问 http://127.0.0.1:5174。使用各终端的 Ctrl+C 停止服务。后端明确绑定回环地址，不开放公网访问。

## 测试

```powershell
.\.venv\Scripts\Activate.ps1
pytest -q backend\tests
Set-Location frontend
npm run build
```

## 故障排查

- `npm 不是内部或外部命令`：安装 Node.js LTS，或在 Node 安装后重新打开终端；确认 `node --version` 和 `npm --version` 都可用。
- `模型 API 密钥未配置`：确认根目录 `.env` 中有 `SOULSEED_MODEL_API_KEY`，并重启后端。
- `硅基流动 API Key 无效`：确认密钥属于当前硅基流动账号，且账号有目标模型权限；不要把密钥放进前端变量或源码。
- `硅基流动请求受到限流或额度限制`：检查硅基流动余额、模型配额和速率限制。
- `硅基流动找不到模型`：确认 `SOULSEED_MODEL_NAME` 使用完整模型 ID；当前默认值为 `deepseek-ai/DeepSeek-V4.1-Flash`。
- `模型服务返回 HTTP ...`：检查 `SOULSEED_MODEL_BASE_URL`、模型名称、网络和供应商配额。
- 页面无法连接：确认后端在 8001 端口运行，前端请求基地址为 `http://127.0.0.1:8001/api`，且页面来源为 `http://127.0.0.1:5174` 或 `http://localhost:5174`。
- 数据库迁移：首次启动会在 `backend/data/soulseed.db` 建库并执行版本化迁移；测试使用临时数据库。

## 当前未实现与接口预留

- 未实现：模型辅助记忆提取、语义/向量记忆检索、记忆摘要、跨分类认知、身份状态机、自主探索调度、后台任务队列、流式输出、多用户认证、角色资产管理和其他交互通道。
- 已预留：独立 `ModelAdapter` 接口、独立 SQLite 仓储、聊天服务编排层；后续可增加记忆服务、任务调度器和 WebSocket/SSE，而不把它们塞入 HTTP 路由。
- 本阶段没有伪造搜索结果或后台任务状态，也没有引入多 Agent 框架或分布式基础设施。
- 当前模型配置：硅基流动 OpenAI-compatible Chat Completions；模型 ID 为 `deepseek-ai/DeepSeek-V4.1-Flash`。
- 当前只注入最小身份和输出安全提示；完整的大肥鱼人格资料尚未存在于仓库。可通过 `SOULSEED_SYSTEM_PROMPT` 注入已确认的人格资料，不建议在没有确认设定时自行扩写。
- 阶段二人格资料位于 `backend/app/persona_profile.md`，人工回归场景位于 `docs/persona_test_cases.md`。默认人格资料始终加载；`SOULSEED_SYSTEM_PROMPT` 只能作为补充约束追加，不能替换核心人格。
- 阶段 2C 已加入确定性按需记忆检索与有限上下文组装：记忆只作为独立参考资料注入，不覆盖人格，也不会默认加载全部记忆。预算可通过 `SOULSEED_MEMORY_CONTEXT_MAX_CHARS`、`SOULSEED_MEMORY_CONTEXT_MAX_ITEMS` 和 `SOULSEED_MEMORY_ITEM_MAX_CHARS` 配置。

