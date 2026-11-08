# Fixed local development runtime

See the [setup guide](setup_EN.md) ([简体中文](setup.md)) for installation,
pairing-mode differences, database protection, and tests. Run this from the
repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-development.ps1
```

This bypass applies only to that PowerShell process; it does not change the
machine or user execution policy. Open <http://127.0.0.1:5174/>. The backend
binds only to <http://127.0.0.1:8001/>. Keep the terminal open and press
Ctrl+C to stop only the two child processes created by this launcher. The
launcher refuses occupied ports and never selects a different port.

The launcher sets `SOULSEED_ENVIRONMENT=development` and pins the database to
`backend/data/soulseed-development.db` within the repository before importing
the app. This database persists across restarts and is not used by tests.
Tests continue through `backend/scripts/run_isolated.py` with temporary
databases. Protected databases and preserved incident backups remain separate
and are not development inputs.

Development mode is unauthenticated for a single local user and is limited to
the fixed `big_fish` character. The backend accepts requests only over
loopback and CORS allows only `http://127.0.0.1:5174`. Other character IDs are
rejected by the existing ownership scope. Testing and Production continue to
use their existing authentication requirements. The old local pairing file,
if present, is ignored and not read by this launcher.

Only `SOULSEED_MODEL_API_KEY`, `SOULSEED_MODEL_BASE_URL`, and
`SOULSEED_MODEL_NAME` are loaded from the ignored project `.env`. The
development database path overrides any database setting in `.env` or the
parent shell. Model settings go to the backend process only; the Vite process
receives the API address and a development-mode flag, not backend secrets.
Startup does not call the model.

Chat model use is enabled by default. Pass `--model-off` to the launcher to
disable paid calls for a run. Each request has a 1024 output-token cap; the
backend reserves up to 20 requests per UTC day and 100 user turns per
conversation. Failed or interrupted requests count toward the daily limit.
Request metadata and provider token usage are logged to
`%LOCALAPPDATA%\Soulseed\development\model-requests.jsonl`; prompts,
responses, and credentials are not logged. These request limits are not a
currency cap, so check SiliconFlow billing for actual costs.

Background autonomous exploration starts disabled. Its UI switch is separate
from contact permission. Enabled exploration is checked by the existing
LifeScheduler while the backend runs; closing the browser does not stop it.
The default interval is at least 24 hours, with 48-hour and 168-hour options.
Exploration has a separate eight-reservation UTC daily ledger, at most four
model decisions per task, and a 512 output-token limit per decision. An
unreadable or exhausted ledger blocks new tasks safely.

Model-generated proactive contact also starts disabled. The launcher accepts
`--contact-model-generation` only with models enabled. This may incur model
usage and still obeys contact rules. Without it, evidence-backed research
shares stay pending; the safe local generator does not create a canned
discovery message.

The launcher prints a `build_id` that can be compared with `GET /api/health`.
`startup_id` identifies one process lifetime. Restart through the launcher
after code changes; this command does not use Uvicorn reload. Frontend and
backend ports remain fixed; an occupied port is reported without stopping
another service.
