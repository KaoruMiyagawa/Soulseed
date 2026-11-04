# Soulseed 2.7.7-B: fictional A/B expression evaluation

## Comparison and scope

The old ordinary-chat prompt is reproduced from commit `ed171fb6fd15139356ac91eb4595b0eff12a5e41`: stable persona, the old memory reference, `LifeRepository.context`, optional pending-share text, and the same user/history messages. The new prompt uses the same persona and user/history messages with `ExpressionContext`. Both use the same frozen fictional SQLite fixture and the same model ID and API request options.

The old deployed proactive-contact generator was `SafeContactGenerator`, which did not call a model. Its text is an offline reference. For paid A/B contact comparison, the old side reproduces the *unused* old `AdapterContactGenerator` prompt; the new side uses the current opt-in adapter. This is a prompt comparison, not a claim that the old deployed system produced model-generated contact messages.

The evaluator does not execute `ChatService.send` or life scheduling. It reconstructs the two generation prompts from the pinned baseline and the current code so both sides see identical frozen facts. It separately exercises the current contact decision through `ContactService.tick` on independent clones of the fictional test database. Memory extraction, clarification, message persistence, and front-end delivery are outside this language-quality comparison and retain their existing regression tests.

## Cases

The checked-in `expression_scenarios.json` defines 14 standalone cases. `generation_failure` remains a local fault-injection case and makes no paid request. The `expression_ab_conversations.json` manifest adds five continuous conversations: daily chat (4 turns), affection/fatigue (4), experience truth (4), proactive contact (1), and technical chat (3). For future continuous comparison, each side maintains its own assistant history while receiving the same user turns and frozen fixture records. The pilot evaluates seven paired single-turn cases: food, affection, completed music, interrupted exploration, pending-share chat, technical help, and proactive contact.

The fictional fixture records completed music with no song name, planned cooking, interrupted exploration, a completed exploration of a shelf corner with a pending share, a wish to eat fish, and a separate second character's meal and memory. It contains no actual audio, external discovery, or meal completed by big_fish. The clock is fixed at `2026-11-02T05:00:00Z` (13:00 in Shanghai), and fixture IDs are deterministic. The dry run prints prompt sizes and hashes, not prompt bodies or credentials.

## Blind review

Per case, aliases X/Y are randomized and the execution order is randomized. `blind_results.json` stores the raw visible replies with aliases only. `mapping.json` is kept locally for unblinding after the human review. `scores.csv` has blank columns for naturalness, character voice, situational fit, factual grounding, continuity, template repetition, and technical accuracy, each on a 1–5 scale. Reviewers record objective flags separately: invented life facts, assumed relationship, unnecessary question, repeated template, ignored emotion, contact-boundary breach, and leaked internal text. A score must be based on actual reply text and evidence; the program never assigns subjective scores.

Identical old/new prompts are marked as a null contrast. For the current pilot, `miss_you` and `technical_help` are identical in the dry run. Any difference between two model samples for these cases is sampling noise, not a demonstrated effect of ExpressionContext.

## Safety and cost

`python backend/scripts/expression_ab_eval.py` is dry-run only: it creates a new database under a unique system Temp directory with `environment="testing"` and explicit `test_data_root`; all files are removed on normal completion. It does not load the project `.env`, import `app.main`, or read an existing database. A paid run needs **both** `--execute` and `--paid-requests-approved`, plus `SOULSEED_MODEL_API_KEY` in the process environment. The endpoint and model are pinned to `https://api.siliconflow.cn/v1` and `deepseek-ai/DeepSeek-V4.1-Flash`; a different `SOULSEED_MODEL_NAME` or `SOULSEED_MODEL_BASE_URL` stops the run.

The pilot has at most 14 requests, `max_tokens=768` per response, 6000 prompt characters per request, no automatic retries, and a conservative ¥1 estimated spend guard. It uses the provider's default sampling parameters for both sides, like the application adapter. Responses must report token usage and a consistent model identity. Any timeout, malformed result, model mismatch, or truncated answer stops further requests. The output cap includes any provider-counted reasoning tokens; a reply may be too short for evaluation, in which case it is marked as a failed run rather than assigned a quality score.

At the official standard-hours price of ¥2/M uncached input tokens and ¥8/M output tokens, 14 requests with 3000 input tokens and 768 output tokens each would cost about ¥0.17. At 6000 input tokens each, about ¥0.25. Character count is not a billing-token guarantee, and pricing or failed-request billing may change; check the official pricing page again before a paid run. A full 14-case plus five-conversation expansion is a separate decision after the pilot and is not enabled by this tool.

Paid results are stored only under the current user's `LOCALAPPDATA/Soulseed/expression-ab` directory, outside Git. The file set contains raw replies, blank scores, a separate alias mapping, and token usage; it never contains the API key. The returned text is generated from fictional records but still should be treated as test material.
