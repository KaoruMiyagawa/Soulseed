[返回首页](../README.md) · **简体中文** | [English](setup_EN.md)

# 安装、运行与维护

本指南面向本地开发与验证。日常 Development 使用固定的前端 5174、后端 8001 和独立持久化开发库。正式数据、受保护样本与事故保全副本不属于日常开发或测试输入。

## 环境与安装

- Python 3.11+。
- Node.js 20+ 与 npm；前端 `node:test` 用例直接导入 TypeScript，运行这些测试需 Node.js 22.18+ 或 24+ 的内置类型擦除支持，参见 [Node.js 官方说明](https://nodejs.org/api/typescript.html)。
- 模型服务凭据：当前默认使用 SiliconFlow 的 OpenAI-compatible Chat Completions 接口。

在仓库根目录运行 Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
npm --prefix frontend install
```

`Copy-Item` 仅用于首次配置；已有 `.env` 时保留原文件。`.env` 受 Git 忽略保护，真实密钥和配对口令不得写入源码、截图或前端 `VITE_` 变量。

## 模型与配置

编辑本机 `.env`，填写 `SOULSEED_MODEL_API_KEY`。当前代码默认值如下；模型名是配置值，不代表供应商永久提供该模型：

```dotenv
SOULSEED_MODEL_BASE_URL=https://api.siliconflow.cn/v1
SOULSEED_MODEL_API_KEY=
SOULSEED_MODEL_NAME=deepseek-ai/DeepSeek-V4.1-Flash
```

完整示例见 [`.env.example`](../.env.example)，字段定义见 [`config.py`](../backend/app/config.py)。模型适配接口不等于任意兼容服务均已验收；探索还依赖供应商对 JSON 输出协议的支持。

推荐的 Development 启动器只从 `.env` 读取上述三个模型字段，其余运行配置由启动器固定设置。不要假定 `.env` 中的数据库、配对或后台开关会被这个启动器继承。手动启动模式会读取根目录 `.env`；身份行为由后端环境决定。

`SOULSEED_SYSTEM_PROMPT` 是手动配置入口中的补充约束；默认 [`persona_profile.md`](../backend/app/persona_profile.md) 始终加载，补充提示不能替换核心人格。按需记忆上下文的字符和条目边界可由 `SOULSEED_MEMORY_CONTEXT_MAX_CHARS`、`SOULSEED_MEMORY_CONTEXT_MAX_ITEMS`、`SOULSEED_MEMORY_ITEM_MAX_CHARS` 配置；完整模型上下文还有独立预算。

## 推荐运行：固定 Development 启动器

```powershell
# 关闭本轮聊天、探索及联系的真实模型调用
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-development.ps1 --model-off
```

该执行策略参数只影响此 PowerShell 进程，不改变机器或用户的全局策略。启动器当前即使使用 `--model-off` 也要求 `.env` 中存在模型 Key；启动本身不会调用模型。

| 项目 | 启动器行为 |
| --- | --- |
| 前端 | `http://127.0.0.1:5174`，端口被占用时拒绝启动 |
| 后端 | `http://127.0.0.1:8001`，绑定本机回环地址 |
| 数据 | 固定为仓库内 `backend/data/soulseed-development.db`，重启后保留 |
| 身份 | Development 单本机用户模式，固定 `big_fish`；不读取本地配对口令 |
| 前端环境 | 只接收 API 地址和 Development 标志，不接收后端模型密钥 |

省略 `--model-off` 会启用模型，交互请求可能产生供应商费用。聊天默认最多每 UTC 日预留 20 次请求、每会话 100 次用户轮次、每次最多 1024 输出 Token。探索使用独立记录，默认每 UTC 日 8 次请求预留、每任务最多 4 次决策、每次最多 512 输出 Token。失败或中断的请求预留也计入额度；额度不是货币预算。

后台自主探索默认关闭。开启后仍须有合格的已保存兴趣、目标或待解决问题，并满足间隔、暂停、互斥和额度条件；后端持续运行时才会检查。关闭网页不停止后端，电脑关机或后端退出则不会继续探索。

探索成果的模型式主动分享也默认关闭。只有在明确接受额外模型调用后，才使用启动器的 `--contact-model-generation` 参数；它与 `--model-off` 不能同时使用。该开关不绕过联系权限、安静时段、冷却或去重。关闭它时，本地安全生成器会保留探索分享为 pending，不编造一条发现消息。

保持启动终端开启，按 **Ctrl+C** 停止启动器创建的子进程。不要强制结束未知进程。通过 `GET /api/health` 对照启动器输出的 `build_id`；`startup_id` 用于区分启动实例，HTTP 200 本身不能证明运行的是最新代码。源代码更新后需按现有流程重启，启动器没有开启自动 reload。

## 手动启动与本地配对

需要完整读取 `.env` 配置时，可在根目录用两个终端手动启动；不要与已运行的固定启动器同时使用。

```powershell
# 终端 1
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001
```

```powershell
# 终端 2
$env:VITE_API_URL = 'http://127.0.0.1:8001/api'
npm --prefix frontend run dev
```

**身份行为由后端环境决定。** 当前 `development` 环境始终使用固定本机用户和 `big_fish`，无论是否通过启动器启动，都不执行配对登录。`testing` 与 `production` 环境保留本地配对授权，不要把 Development 的行为当成生产认证方案。

需要验证配对时，可在本机交互终端显式使用现有临时测试启动器：

```powershell
.\.venv\Scripts\python.exe backend\scripts\start_paired_test.py
```

它在临时数据库上使用前端 5175、后端 18081，并只在交互终端显示本次随机口令；按 Ctrl+C 停止并清理，不能作为额外常驻开发环境。

非 Development 的已授权本地部署使用 `SOULSEED_LOCAL_PAIRING_SECRET`，可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成至少 32 字符的随机口令，并仅保存在本机配置中。口令未配置时数据接口保持锁定。配对使用 8 小时 HttpOnly、SameSite=Strict Cookie；不要混用 `localhost` 和 `127.0.0.1`。本地配对是本机用户认证，不是多用户注册或公网登录。详细的归属、角色范围和迁移前置要求见 [数据归属与本地身份验证](ownership_and_local_auth.md)。

## 数据库、备份与迁移

- 一个 SQLite 文件只允许一个后端进程。实例锁覆盖整个应用运行期；不要使用多个 Uvicorn worker、网络文件系统或多主机共享数据库。
- Development 可以自动执行开发库的版本化迁移。升级已有开发库前，先确认目标和版本兼容性，并取得可验证的一致性备份；测试只能使用临时库。
- production 模式必须显式指定数据库，不能自动创建正式库。有待迁移版本时，需要仓库外独立、字节一致的备份及精确的版本确认；存在 WAL/SHM/journal 时需先完成一致性处理。
- `SOULSEED_MIGRATION_BACKUP_PATH` 指向独立备份，`SOULSEED_MIGRATION_CONFIRMATION` 必须与实际版本转换一致，例如 `13->14` 只是格式示例，不能照抄作为当前升级指令。
- 受保护数据库还要求 `SOULSEED_ALLOW_PROTECTED_DATABASE_ACCESS=true`，且 `SOULSEED_PROTECTED_DATABASE_CONFIRMATION` 精确确认其规范化绝对路径。这是单独授权的维护机制，不是排障时应绕过的开关。不要访问或修改正式数据、受保护样本和事故备份来“试一下”。
- Migration 12 对无法可靠确认归属的旧记录保持隔离，不自动归属给默认角色。保留来源和原始记录，按专门维护流程人工核对。

历史归属工具 `backend/scripts/confirm_legacy_ownership.py` 只适用于已有明确授权及已核验分析副本的专项维护；不是安装步骤。`python backend/scripts/confirm_legacy_ownership.py --verify-only` 仅用于该流程中的只读汇总核验。该工具不会恢复数据库，不能指向任意真实库尝试；不要把它的本地确认清单或聊天正文提交到 Git。

## 隔离测试与构建

在根目录使用项目 Python：

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_isolated.py tests
```

也可在激活虚拟环境后使用 `python backend\scripts\run_isolated.py tests`。启动器创建系统临时目录和 SQLite 库，清除真实模型凭据，并跳过项目 `.env`。测试使用假模型和受控网络；直接运行未配置隔离环境的 `pytest` 会被拒绝。测试路径经符号链接/junction 解析后仍必须位于独立临时根目录。

定向运行的路径相对于 `backend/`：

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_isolated.py tests tests/test_exploration_agent.py tests/test_contact.py
```

前端检查在根目录运行，`node:test` 命令使用支持直接导入 TypeScript 的 Node 环境（22.18+ 或 24+）：

```powershell
node --test frontend/tests/chat_transport.test.mjs frontend/tests/exploration_quota.test.mjs frontend/tests/proactive_delivery.test.mjs
npm --prefix frontend run build
```

`build` 同时执行 TypeScript `--noEmit` 检查与 Vite 构建。确需手工测试隔离 API 时，可显式运行 `python backend/scripts/run_isolated.py api --port 18081`；这是临时测试实例，退出后清理自身临时目录，不替代 5174/8001 日常环境，也不应成为额外常驻服务。

隔离测试通过不代表真实网络、真实模型表达或无人操作后台运行已验收。已归档的真实案例与历史测试结果见 [CHANGELOG](../CHANGELOG.md)。

## 故障排查

| 现象 | 检查方向 |
| --- | --- |
| 找不到 `node` 或 `npm` | 确认两者均安装，并重新打开终端检查 `node --version`、`npm --version`。 |
| 模型 Key 未配置或无效 | 检查本机 `.env`、密钥所属供应商和模型权限；更改后重启。不要输出密钥进行诊断。 |
| 找不到模型或 HTTP 错误 | 检查完整模型 ID、兼容接口地址、供应商权限、余额与速率限制。 |
| 本地探索额度耗尽 | 区别于供应商 HTTP 429；查看探索页面的 UTC 重置时间。不可读取的额度记录会安全阻止请求。 |
| 页面连不上 API | 核对 8001、5174、`VITE_API_URL` 和浏览器 Origin；固定启动器只允许 `http://127.0.0.1:5174`。 |
| 配对失败 | 确认后端处于支持配对的 testing/production 环境、口令已配置且主机名一致；连续错误会触发限速。 |
| 端口或数据库实例锁冲突 | 找到既有启动终端正常停止服务；不要另选端口或删除锁来绕过实例约束。 |
| 代码看似没有更新 | 对照 `/api/health` 的 `build_id` 与本轮启动输出，正常重启后再验收。 |
| 探索网页不可用 | 查看供应商和读取错误类别；代理、DNS、TLS、正文过大和证据不足是不同状态。不要关闭 SSRF 或证书校验。 |
| 分享一直 pending | 检查分享是否值得发送、联系授权和静默规则；模型式联系生成关闭时，探索分享会保持待处理。 |

请求元数据保存在本机 `%LOCALAPPDATA%/Soulseed/development` 下，聊天与探索额度分别记录。日志用于区分决策尝试、请求预留、HTTP 尝试和供应商返回；无法凭本地日志确认供应商最终计费。不要公开原始日志、凭据或私人聊天。更多协议排障见 [探索行动诊断](exploration_action_diagnostics.md)，更精确的运行边界见 [Development runtime](development_runtime.md)。
