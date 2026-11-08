[Back to home](../README_EN.md) · [简体中文](setup.md) | **English**

# Setup, operation, and maintenance

This guide covers local development and validation. Daily Development uses frontend port 5174, backend port 8001, and a separate persistent Development database. Formal data, protected samples, and preserved incident backups are outside this workflow.

## Requirements and installation

- Python 3.11+.
- Node.js 20+ and npm. Frontend `node:test` cases import TypeScript directly and need built-in type stripping from Node.js 22.18+ or 24+; see the [official Node.js documentation](https://nodejs.org/api/typescript.html).
- Model-provider credentials. The configured default is SiliconFlow's OpenAI-compatible Chat Completions endpoint.

Run Windows PowerShell from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
npm --prefix frontend install
```

Copy the example only during initial setup; preserve an existing `.env`. Git ignores that file. Never put credentials or pairing secrets in source, screenshots, or frontend `VITE_` variables.

## Model configuration

Set `SOULSEED_MODEL_API_KEY` in your local `.env`. Current code defaults are shown below; a configured model name does not guarantee continued provider availability:

```dotenv
SOULSEED_MODEL_BASE_URL=https://api.siliconflow.cn/v1
SOULSEED_MODEL_API_KEY=
SOULSEED_MODEL_NAME=deepseek-ai/DeepSeek-V4.1-Flash
```

See [`.env.example`](../.env.example) and [`config.py`](../backend/app/config.py). The adapter interface does not imply that every compatible provider has been validated. Exploration also depends on JSON-output protocol support.

The recommended Development launcher reads only these three model fields from `.env`; it fixes the other runtime settings itself. Do not assume database paths, pairing secrets, or background flags in `.env` are inherited by that launcher. Manual startup reads the root `.env`; authentication depends on the backend environment.

`SOULSEED_SYSTEM_PROMPT` adds constraints in manually configured startup. The default [`persona_profile.md`](../backend/app/persona_profile.md) always loads and cannot be replaced by this supplement. Memory context bounds are configurable through `SOULSEED_MEMORY_CONTEXT_MAX_CHARS`, `SOULSEED_MEMORY_CONTEXT_MAX_ITEMS`, and `SOULSEED_MEMORY_ITEM_MAX_CHARS`; the overall model context has separate budgets.

## Recommended: fixed Development launcher

```powershell
# Disable live model requests for chat, exploration, and contact during this run
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-development.ps1 --model-off
```

The execution-policy argument affects only this PowerShell process, not machine or user policy. The launcher currently requires a configured model key even with `--model-off`. Startup itself makes no model request.

| Item | Launcher behavior |
| --- | --- |
| Frontend | `http://127.0.0.1:5174`; occupied ports cause startup to fail |
| Backend | `http://127.0.0.1:8001`; loopback binding only |
| Data | Pinned to `backend/data/soulseed-development.db` in the repository; persists across restarts |
| Identity | One local user and `big_fish`; no pairing secret is read |
| Frontend environment | Receives the API address and Development flag, not backend model credentials |

Omitting `--model-off` enables model use and may incur provider charges. Chat reserves up to 20 requests per UTC day and 100 user turns per conversation, with a 1024 output-token cap per request. Exploration uses a separate ledger: by default, eight reservations per UTC day, four decisions per task, and 512 output tokens per decision. Failed/interrupted reservations count too. These limits are not monetary spending caps.

Background autonomous exploration starts disabled. Once enabled, it still requires a supported saved interest, goal, or unresolved question, plus interval, pause, concurrency, and quota checks. It runs only while the backend is alive. Closing the webpage does not stop the backend; shutting down the computer or backend stops exploration.

Model-generated discovery contact also starts disabled. Use `--contact-model-generation` only after explicitly accepting additional model usage; it cannot be combined with `--model-off`. This does not bypass contact permissions, quiet hours, cooldown, or deduplication. When it is off, the local safe generator keeps research shares pending instead of inventing a discovery message.

Keep the terminal open. **Ctrl+C** stops the launcher’s own child processes. Do not forcibly stop unknown processes. Compare the `build_id` from `GET /api/health` with the launch output; `startup_id` identifies the running instance. HTTP 200 alone does not prove the code is current. Restart through the existing workflow after code changes; the launcher does not enable automatic reload.

## Manual startup and local pairing

For startup that reads the full `.env`, use two terminals from the root. Do not run it alongside the fixed launcher:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001
```

```powershell
# Terminal 2
$env:VITE_API_URL = 'http://127.0.0.1:8001/api'
npm --prefix frontend run dev
```

**The backend environment determines authentication.** Current `development` mode always uses the fixed local user and `big_fish`, regardless of how it starts, and bypasses pairing login. `testing` and `production` retain local pairing authorization. Development behavior is not a production authentication design.

To check pairing in a disposable environment, explicitly run the existing test launcher in an interactive local terminal:

```powershell
.\.venv\Scripts\python.exe backend\scripts\start_paired_test.py
```

It uses a temporary database, frontend port 5175, and backend port 18081. A random pairing secret appears only in the terminal. Ctrl+C stops and cleans up this test pair; do not keep it as an additional permanent development environment.

An authorized non-Development local installation uses `SOULSEED_LOCAL_PAIRING_SECRET`. Generate a random value of at least 32 characters with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and store it only in local configuration. Data endpoints remain locked when the secret is missing. Pairing uses an eight-hour HttpOnly, SameSite=Strict cookie; keep `localhost` and `127.0.0.1` consistent. This authenticates a local installation, not a multi-user registration or public login service. See [ownership and local authentication](ownership_and_local_auth.md) for the detailed design and migration prerequisites; that historical document is in Chinese.

## Databases, backups, and migrations

- One SQLite file supports one backend process. The instance lock covers the application lifetime. Do not use multiple Uvicorn workers, network filesystems, or multiple hosts against the same file.
- Development can apply versioned migrations automatically. Before upgrading an existing Development database, confirm the target and compatibility, then obtain a verifiable consistent backup. Tests use disposable databases only.
- Production requires an explicit database path and cannot create a formal database automatically. Pending migrations require an independent, byte-identical backup outside the repository and an exact version-transition confirmation. Resolve WAL/SHM/journal files through a consistent-backup procedure first.
- `SOULSEED_MIGRATION_BACKUP_PATH` names the backup. `SOULSEED_MIGRATION_CONFIRMATION` must match the actual transition: `13->14` is a format example, not a current upgrade command.
- Protected data additionally requires `SOULSEED_ALLOW_PROTECTED_DATABASE_ACCESS=true` and `SOULSEED_PROTECTED_DATABASE_CONFIRMATION` matching the normalized absolute target path. This is a separately authorized maintenance mechanism, not a troubleshooting shortcut. Do not experiment with formal databases, protected samples, or incident backups.
- Migration 12 quarantines legacy records whose ownership cannot be reliably confirmed; it does not assign them to the default character. Preserve sources and original records and use a dedicated ownership-review procedure.

`backend/scripts/confirm_legacy_ownership.py` is a specialist tool for an explicitly authorized, verified analysis copy, not an installation step. `python backend/scripts/confirm_legacy_ownership.py --verify-only` performs read-only aggregate verification within that procedure. The tool does not restore a database and must not be tried against arbitrary live data. Do not commit its local confirmation records or conversation text.

## Isolated tests and builds

From the root, use the project Python:

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_isolated.py tests
```

After activating the virtual environment, `python backend\scripts\run_isolated.py tests` is equivalent. The launcher creates a system temporary directory and SQLite database, removes real model credentials, and skips project `.env` loading. Tests use fake models and controlled networking. Unconfigured direct `pytest` execution is rejected. Paths must stay inside the independent temporary root after symlink/junction resolution.

Targeted test paths are relative to `backend/`:

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_isolated.py tests tests/test_exploration_agent.py tests/test_contact.py
```

Run frontend checks from the repository root. The `node:test` command requires direct TypeScript import support from Node.js 22.18+ or 24+:

```powershell
node --test frontend/tests/chat_transport.test.mjs frontend/tests/exploration_quota.test.mjs frontend/tests/proactive_delivery.test.mjs
npm --prefix frontend run build
```

`build` runs TypeScript `--noEmit` checking and Vite bundling. For an explicitly requested manual API check, `python backend/scripts/run_isolated.py api --port 18081` starts a temporary instance and removes its data on exit. It does not replace daily Development on 5174/8001 and should not become a permanent extra service.

Passing isolated tests does not establish live network quality, model expression, or unattended operation. Archived live examples and historical results are in [CHANGELOG](../CHANGELOG.md), currently in Chinese.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `node` or `npm` not found | Install both, reopen the terminal, and check `node --version` and `npm --version`. |
| Missing or invalid model key | Check local `.env`, provider identity, and model permissions; restart after changes. Do not print the key for diagnosis. |
| Model not found or HTTP error | Check the full model ID, compatible endpoint, provider permissions, balance, and rate limits. |
| Local exploration quota exhausted | Distinguish it from provider HTTP 429 and check the UTC reset time in the UI. An unreadable ledger safely blocks requests. |
| UI cannot reach the API | Check 8001, 5174, `VITE_API_URL`, and browser Origin. The fixed launcher allows only `http://127.0.0.1:5174`. |
| Pairing fails | Check the environment, secret, and matching hostnames; repeated failures are throttled. |
| Port or database instance conflict | Stop the existing service normally from its own terminal. Do not change ports or remove a lock to bypass ownership. |
| Code appears unchanged | Compare `/api/health` `build_id` with startup output, then restart normally before acceptance. |
| A research page is unavailable | Inspect provider and reader errors. Proxy, DNS, TLS, oversized content, and insufficient evidence are different outcomes. Keep SSRF and certificate checks enabled. |
| A share stays pending | Check share-worthiness, contact permission, and quiet-hour rules. Research shares stay pending while model-based contact generation is disabled. |

Request metadata lives under local `%LOCALAPPDATA%/Soulseed/development`, with separate chat and exploration ledgers. Diagnostics distinguish decisions, reservations, HTTP attempts, and provider responses; local logs cannot confirm final provider billing. Do not publish raw logs, credentials, or private conversations. See [exploration diagnostics](exploration_action_diagnostics.md) and [Development runtime](development_runtime.md) for details.
