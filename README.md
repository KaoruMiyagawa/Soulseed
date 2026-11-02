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

首次升级到 Migration 12 前，请先阅读 [数据归属与本地配对说明](docs/ownership_and_local_auth.md)：
旧会话归属需要明确确认，未确认数据会被保留并隔离。
在本机 `.env` 设置至少 32 字符的随机 `SOULSEED_LOCAL_PAIRING_SECRET`，网页首次进入时输入该口令配对。development 默认使用独立的 `backend/data/soulseed-development.db`；受保护的既有数据需要同时启用 `SOULSEED_ALLOW_PROTECTED_DATABASE_ACCESS=true` 并将 `SOULSEED_PROTECTED_DATABASE_CONFIRMATION` 设置为该数据库规范化后的绝对路径。访问已受保护数据库时，任何待执行迁移仍必须有仓库外字节一致备份及精确版本确认。
口令不应放入前端配置；未配置时数据接口保持锁定。

在项目根目录打开两个终端：

事故后历史会话的本地只读归属确认工具使用 `python backend\scripts\confirm_legacy_ownership.py` 启动；先运行 `--verify-only` 可只输出快照哈希和聚合数量。工具固定读取已核验的分析副本，交互内容仅在本机终端按需显示；确认清单保存于当前用户本地受限目录，不包含聊天正文，也不执行数据库恢复。

```powershell
# 终端 1：后端 API（127.0.0.1:8001）
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001

# 终端 2：前端（127.0.0.1:5174）
Set-Location frontend
npm run dev
```

访问 http://127.0.0.1:5174。使用各终端的 Ctrl+C 停止服务。后端明确绑定回环地址，不开放公网访问。

一个 SQLite 数据库只允许一个后端进程使用。第二个服务进程会因本机实例锁冲突而拒绝启动；不要用多个 Uvicorn worker、网络文件系统或多台主机共享数据库。锁文件位于数据库旁，服务退出后由操作系统释放锁。可通过已授权的 `GET /api/characters/{character_id}/life` 检查活动 `effective_status`、中断原因与时间。

## 测试

```powershell
.\.venv\Scripts\Activate.ps1
python backend\scripts\run_isolated.py tests
Set-Location frontend
npm run build
```

后端自动化测试必须通过隔离启动器运行。它为每次运行创建独立临时目录和 SQLite 数据库，测试环境不会读取项目 `.env`；直接运行 `pytest` 会失败即拒绝启动。手工运行隔离 API 可使用 `python backend\scripts\run_isolated.py api --port 18081`，退出后启动器会清理自己创建的临时目录。测试数据库路径必须是系统临时目录下的绝对路径，并且经符号链接/junction 解析后仍位于该目录。

## 故障排查

- `npm 不是内部或外部命令`：安装 Node.js LTS，或在 Node 安装后重新打开终端；确认 `node --version` 和 `npm --version` 都可用。
- `模型 API 密钥未配置`：确认根目录 `.env` 中有 `SOULSEED_MODEL_API_KEY`，并重启后端。
- `硅基流动 API Key 无效`：确认密钥属于当前硅基流动账号，且账号有目标模型权限；不要把密钥放进前端变量或源码。
- `硅基流动请求受到限流或额度限制`：检查硅基流动余额、模型配额和速率限制。
- `硅基流动找不到模型`：确认 `SOULSEED_MODEL_NAME` 使用完整模型 ID；当前默认值为 `deepseek-ai/DeepSeek-V4.1-Flash`。
- `模型服务返回 HTTP ...`：检查 `SOULSEED_MODEL_BASE_URL`、模型名称、网络和供应商配额。
- 页面无法连接：确认后端在 8001 端口运行，前端请求基地址为 `http://127.0.0.1:8001/api`，且页面来源为 `http://127.0.0.1:5174` 或 `http://localhost:5174`。
- 数据库迁移：development 环境会自动执行版本化迁移。production 环境不会自动建库；有待执行迁移时必须提供仓库外的完整独立备份路径以及精确版本确认（如 `13->14`）。测试只能使用上述隔离启动器。

## 当前未实现与接口预留

- 未实现：模型辅助记忆提取、语义/向量记忆检索、记忆摘要、跨分类认知、身份状态机、自主探索调度、后台任务队列、流式输出、多用户认证、角色资产管理和其他交互通道。
- 已预留：独立 `ModelAdapter` 接口、独立 SQLite 仓储、聊天服务编排层；后续可增加记忆服务、任务调度器和 WebSocket/SSE，而不把它们塞入 HTTP 路由。
- 本阶段没有伪造搜索结果或后台任务状态，也没有引入多 Agent 框架或分布式基础设施。
- 当前模型配置：硅基流动 OpenAI-compatible Chat Completions；模型 ID 为 `deepseek-ai/DeepSeek-V4.1-Flash`。
- 当前只注入最小身份和输出安全提示；完整的大肥鱼人格资料尚未存在于仓库。可通过 `SOULSEED_SYSTEM_PROMPT` 注入已确认的人格资料，不建议在没有确认设定时自行扩写。
- 阶段二人格资料位于 `backend/app/persona_profile.md`，人工回归场景位于 `docs/persona_test_cases.md`。默认人格资料始终加载；`SOULSEED_SYSTEM_PROMPT` 只能作为补充约束追加，不能替换核心人格。
- 阶段 2C 已加入确定性按需记忆检索与有限上下文组装：记忆只作为独立参考资料注入，不覆盖人格，也不会默认加载全部记忆。预算可通过 `SOULSEED_MEMORY_CONTEXT_MAX_CHARS`、`SOULSEED_MEMORY_CONTEXT_MAX_ITEMS` 和 `SOULSEED_MEMORY_ITEM_MAX_CHARS` 配置。

