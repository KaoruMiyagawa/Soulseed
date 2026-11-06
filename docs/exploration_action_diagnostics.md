# Exploration action protocol and the first failed whale exploration

## Read-only incident evidence (2026-11-06)

Development run `04961905-7dca-4056-b522-e7bc61779a9a` failed with
`invalid_model_action` at decision step 1. Its model call counter is 1, tool
call counter is 0, and there are no exploration evidence rows.

The matching request window in `exploration-model-requests.jsonl` is
08:53:35.047385–08:53:41.315988 UTC; request ID is
`9772d858-bb1c-4430-8bd2-e640e5fff860`.

| Recorded property | Value |
| --- | --- |
| Model | deepseek-ai/DeepSeek-V4.1-Flash |
| HTTP result | 200 |
| finish_reason | stop |
| Input / completion / total tokens | 360 / 101 / 461 |
| Reasoning tokens (within completion usage) | 64 |
| Final content / reasoning lengths | 110 / 302 characters |
| Output cap | 512 tokens |
| Structured request | false |

The separate earlier run `e546874a-9bcf-4994-9268-9381a15edc41` was cancelled
by the user: one model call, one tool-call attempt, no saved tool result. Its
search attempt must not be attributed to the later failed run.

The failed run reached the content JSON/action validation layer, before any
tool execution. The response body and validator branch were not retained.
Consequently Markdown wrapping, extra fields, incorrect field types, and
invalid JSON cannot be distinguished retrospectively. Token truncation is not
supported by the recorded `stop` finish reason and 101/512 token usage.
Do not present a synthetic response as a recovered incident response.

## Protocol change

Exploration uses the existing Chat Completions adapter with
`response_format={"type":"json_object"}` through `reply_action`. Ordinary
chat and memory clarification still use their existing methods. The action
schemas and bounds are stated explicitly in the prompt and enforced locally.
This protocol consumes `message.content`, not native `message.tool_calls`.
Unexpected native tool calls are rejected rather than executed or silently
discarded. JSON mode support is documented by SiliconFlow, but this exact
model/configuration has not been exercised again against the paid service.

The cap remains 512 output tokens per decision, at most 4 decisions, and the
existing separate daily exploration budget. There is no automatic repair
request, retry, model switch, or fallback to unconstrained text.

## Diagnostics for future runs

Started/finished model audit entries include `exploration_run_id` and
`exploration_step`; `action_validation` entries use the same request ID.
They record safe metadata only: HTTP status, finish reason, token usage,
content length/type, fence presence, known field types, unknown field count,
and validation subreason. Unknown field names, action values, response
content, reasoning content, credentials and user text are not persisted.

Subreasons include `invalid_json`, `missing_field`, `invalid_arguments`,
`duplicate_field`, `truncated_output`, `unexpected_tool_calls`, and
`unexpected_finish_reason`. Unknown actions retain the `disallowed_tool`
error and are audited as `unknown_action` without retaining their name.
Task errors store a bounded `invalid_model_action:<reason>` in the existing
error column; no schema migration or historical row update is required.
The UI maps these codes to short explanations and never displays raw output.

Tests use an isolated temporary database and HTTPX MockTransport. The incident
fixture contains observed metadata only; all test response bodies are explicitly
synthetic. A new manual model invocation is still required for live acceptance
after restarting the existing development launcher (Uvicorn has no reload flag).

Protocol reference: https://api-docs.siliconflow.cn/docs/userguide/guides/json-mode
