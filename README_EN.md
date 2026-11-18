[简体中文](README.md) | **English**

# Soulseed

**A future where everyone can create a digital individual who keeps growing.**

*A World for Every Individual.*

**Current version: v2.9.1-beta.13 · Private Windows beta.** Soulseed is a local, single-user prototype in active development. The [product vision and decision record](docs/product_vision.md) describes the long-term direction.

## What is Soulseed?

Soulseed treats conversation as one part of a digital individual's life. A character can remember information with sources, develop interests and habits through experience, form a view, and revise it when new evidence appears. Records distinguish completed virtual life activities, plans, wishes, and interrupted work. Web research is backed by separate source evidence.

## Core capabilities

| Area | Implemented |
| --- | --- |
| Persona and conversation | A defined persona, bounded context, synchronous and asynchronous chat, and an OpenAI Chat Completions compatible adapter. Windows chat output is capped at 1024 tokens. |
| Long-term memory | Structured memories with sources, rule-based retrieval, conflict detection, clarification, confirmed corrections, and change history. |
| Autonomous life and growth | Activity planning, lifecycle, and state. Repeated experiences shape interests, and activities repeated across dates can form time-of-day habits. Both can influence activity choices and expression. |
| Independent opinions | The character can disagree with the user, carry views across conversations, and revise them using completed experiences or sourced research. Revisions retain their history and basis. |
| Autonomous exploration | A bounded agent can search, read real webpages, check their relevance, and save cited findings. It can derive new questions from saved experiences, interests, and findings, with a record of their source. |
| Exploration reliability | Conservative deduplication detects clear rewrites and keeps distinct questions on the same topic. Interrupted work retains its question, evidence, and request records and can be retried explicitly. Windows manual exploration and retries use strict request limits. |
| Autonomous contact | Recent conversations, saved life state, or shareable findings can create contact candidates. The character can choose silence. Approved messages go to an existing conversation, with native Windows notifications while the window is in the background. |
| Local Windows app | Tauri stores app data per Windows account, protects model configuration with DPAPI, and offers daily request limits and controls for autonomous features. |

## Da Feiyu

Da Feiyu (大肥鱼, “Big Fish”) is the first digital individual. She is playful, lively, curious, and childlike. Her [persona profile](backend/app/persona_profile.md) and [review scenarios](docs/persona_test_cases.md) guide checks of character continuity across conversations and experiences.

## Run and install

Windows 10/11 x64 is the primary client. The installer is distributed privately and is currently unsigned. Verify the accompanying SHA256 and follow the [Windows installation and model setup guide](docs/windows-install.md) for installation or upgrade. To build from source, see the [Windows build guide](docs/windows-build.md).

The browser is primarily a local development, debugging, and compatibility client. Install dependencies using the [setup guide](docs/setup_EN.md), then launch with the model disabled. See [Development runtime](docs/development_runtime.md) for configuration requirements.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-development.ps1 --model-off
```

## Architecture

**One digital individual core, multiple clients.** The Windows Tauri window and browser share the React/TypeScript interface and local FastAPI character core. Future WeChat and QQ clients should use the same core. Those channels are planned.

- `ChatService` uses `ExpressionContext` to assemble persona, memory, experiences, interests, habits, and opinions. Chat and autonomous contact share character data and expression rules.
- `LifeEngine` selects activities from saved state. `LifeScheduler` advances life and runs the optional background exploration schedule. Interests and habits use existing life records. Completing a research run does not directly raise interest weights.
- `ExplorationAgent` searches, reads, and checks evidence under tool and model limits. Qualified findings can support opinion revisions, follow-up questions, and discovery sharing. Contact uses shared candidate, review, budget, and delivery logic. Windows adds the native notification adapter.
- SQLite stores conversations, memories, life records, opinions, research evidence, and the request ledger. The backend uses Python, FastAPI, and HTTPX. The interface uses React, TypeScript, and Vite.

## Verification and limitations

| Level | Evidence |
| --- | --- |
| Automated tests | Isolated beta.13 tests cover question provenance, deduplication, recovery, and quotas, with regressions for chat, memory, life growth, habits, opinions, and autonomous contact. |
| Windows with simulations | beta.13 was installed over beta.12 with user data verified. Exploration, chat, and opinion continuity were exercised with isolated data and local mocks, including simulated webpages. Native notifications were checked separately with an injected test event. Earlier Windows betas recorded simulated autonomous contact and delivery to the original conversation. |
| Live models and network | Earlier versions recorded a small number of successful live manual and background research runs, including whale vocalization. See the [technical changelog](CHANGELOG.md). The beta.13 question selection, deduplication, and retry changes have yet to be revalidated with a live provider and real webpages. |
| Current boundaries | Deduplication may miss cross-language equivalence or substantial rewordings. Retrying creates a linked run that uses the current quota. Quotas count requests. Providers determine actual charges. Background behavior requires the app to be running and the relevant controls enabled. |

## Roadmap

- Improve memory accuracy, retrieval, and conflict handling.
- Develop traceable long-term relationship growth that can adapt over time.
- Improve question selection, source quality, deduplication, and exploration failure handling.
- Validate long-running operation, recovery, and expression quality with live models.
- Immersive digital worlds, external messaging channels, and a multi-character system are long-term directions. The 2.5D world is outside the current development scope.

## Development and contributions

Reproducible bug reports and focused changes are welcome. [Issues](https://github.com/KaoruMiyagawa/Soulseed/issues)

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_isolated.py tests
npm --prefix frontend test
```

Use an isolated database and report whether verification used a mock or a live provider. Keep credentials, user data, logs, backups, build outputs, and unapproved assets out of the repository.

Further reading: [life and growth](docs/life_growth.md) · [opinion continuity](docs/opinion_continuity.md) · [autonomous contact](docs/autonomous_social.md) · [exploration diagnostics](docs/exploration_action_diagnostics.md) · [changelog](CHANGELOG.md). Some project documentation is in Chinese.
