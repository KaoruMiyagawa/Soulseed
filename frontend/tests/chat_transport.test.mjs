import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, apiErrorMessage, createApiRequest } from "../src/apiRequest.ts";

test("a lost chat response is unknown, sends once and logs no private payload", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; throw new Error("private transport error"); });
  const timings = [];
  const request = createApiRequest("http://127.0.0.1:9/api", () => {}, (...v) => timings.push(v));
  await assert.rejects(request("/conversations/fake/messages", {
    method: "POST", body: JSON.stringify({ content: "private fictional text" }),
  }), /无法确认服务端/);
  assert.equal(calls, 1);
  assert.equal(JSON.stringify(timings).includes("private"), false);
  assert.equal(timings[0][1].outcome, "network_failure");
});

test("saved backend timeout remains distinct from unsaved budget failure", async (t) => {
  const replies = [
    new Response(JSON.stringify({ detail: "模型超时", message_saved: true }), { status: 504 }),
    new Response(JSON.stringify({ detail: "输入过长", message_saved: false }), { status: 413 }),
  ];
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; return replies.shift(); });
  const request = createApiRequest("http://127.0.0.1:9/api", () => {}, () => {});
  await assert.rejects(request("/conversations/fake/messages", { method: "POST" }),
    e => e instanceof ApiError && e.status === 504 && e.messageSaved);
  await assert.rejects(request("/conversations/fake/messages", { method: "POST" }),
    e => e instanceof ApiError && e.status === 413 && !e.messageSaved);
  assert.equal(calls, 2);
});

test("session expiry is signalled even if an intermediary returned a non-JSON body", async (t) => {
  let expired = 0;
  t.mock.method(globalThis, "fetch", async () => new Response("not JSON", { status: 401 }));
  const request = createApiRequest("http://127.0.0.1:9/api", () => expired++, () => {});
  await assert.rejects(request("/conversations/fake/messages", { method: "POST" }));
  assert.equal(expired, 1);
});

test("structured API errors are rendered as safe text, never object coercions", async (t) => {
  assert.equal(apiErrorMessage({ detail: { code: "no_saved_interest", extra: { token: "secret" } } }, 409), "请求失败（409）");
  assert.equal(apiErrorMessage({ detail: { message: "没有可用选题依据" } }, 409), "没有可用选题依据");
  assert.equal(apiErrorMessage({ detail: [{ msg: "主题不能为空", input: "private" }] }, 422), "主题不能为空");

  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ detail: { code: "internal_error", secret: "do-not-display" } }), { status: 500 }));
  const request = createApiRequest("http://127.0.0.1:9/api", () => {}, () => {});
  await assert.rejects(request("/explorations"), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.message, "请求失败（500）");
    assert.equal(error.message.includes("object Object"), false);
    assert.equal(error.message.includes("do-not-display"), false);
    return true;
  });
});
