import test from "node:test";
import assert from "node:assert/strict";
import { ChatQueue, mergeMessages } from "../src/chatQueue.ts";

function storage(t) {
  const values = new Map();
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const originalLocks = Object.getOwnPropertyDescriptor(navigator, "locks");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
  } });
  Object.defineProperty(navigator, "locks", { configurable: true, value: {
    request: async (_name, options, callback) => (callback ?? options)({}),
  } });
  t.after(() => {
    if (originalStorage) Object.defineProperty(globalThis, "localStorage", originalStorage);
    else delete globalThis.localStorage;
    if (originalLocks) Object.defineProperty(navigator, "locks", originalLocks);
    else delete navigator.locks;
  });
}

test("queue restores stable order with isolation by backend, user, character and conversation", async t => {
  storage(t);
  const q = new ChatQueue("api-a", "user-a", "fish", "conversation-a");
  const first = await q.enqueue("first");
  const restored = new ChatQueue("api-a", "user-a", "fish", "conversation-a");
  const second = await restored.enqueue("second");
  assert.deepEqual(q.snapshot(), [first, second]);
  assert.equal(first.client_stream_id, second.client_stream_id);
  assert.deepEqual([first.client_sequence, second.client_sequence], [1, 2]);
  for (const scope of [
    ["api-b", "user-a", "fish", "conversation-a"],
    ["api-a", "user-b", "fish", "conversation-a"],
    ["api-a", "user-a", "other", "conversation-a"],
    ["api-a", "user-a", "fish", "conversation-b"],
  ]) {
    assert.deepEqual(new ChatQueue(...scope).snapshot(), []);
  }
});

test("conflicting acknowledgement leaves the original head and blocks all later sends", async t => {
  storage(t);
  const q = new ChatQueue("api-a", "user-a", "fish", "conversation-a");
  const first = await q.enqueue("first");
  const second = await q.enqueue("second");
  const signal = new AbortController().signal;
  const attempts = [];
  const confirmed = [];
  const request = async (_path, options) => {
    attempts.push(JSON.parse(options.body));
    return { ...first, save_status: "saved", message_id: "server", role: "user", server_sequence: 1,
      conversation_id: "conversation-a", content: "wrong" };
  };
  assert.equal(await q.sendHead(request, signal, () => {}, record => confirmed.push(record)), false);
  assert.equal(await q.sendHead(request, signal, () => {}, record => confirmed.push(record)), false);
  assert.deepEqual(attempts.map(item => item.client_message_id), [first.client_message_id]);
  assert.equal(confirmed.length, 0);
  assert.deepEqual(q.snapshot().map(item => item.client_message_id), [first.client_message_id, second.client_message_id]);
  assert.equal(q.snapshot()[0].failure.permanent, true);
});

test("a delayed save acknowledgement preserves a completed status and one authoritative row", () => {
  const user = { id: "m1", role: "user", content: "one", role_status: "completed", server_sequence: 1 };
  const reply = { id: "m2", role: "assistant", content: "reply", user_message_id: user.id, server_sequence: 2 };
  const merged = mergeMessages([reply, user], [{ ...user, role_status: "waiting" }, reply], true);
  assert.deepEqual(merged.map(item => item.id), ["m1", "m2"]);
  assert.equal(merged[0].role_status, "completed");
  assert.equal(merged[1].user_message_id, user.id);
});

test("closed admission reconciles a lost acknowledgement with GET only", async t => {
  storage(t);
  const queue = new ChatQueue("api", "user", "fish", "conversation");
  const first = await queue.enqueue("saved before shutdown");
  const second = await queue.enqueue("unsent after shutdown");
  const signal = new AbortController().signal;
  await queue.sendHead(async (_path, options) => {
    if (options.method === "POST") throw new Error("response lost");
    throw Object.assign(new Error("not yet found"), { status: 404 });
  }, signal, () => {}, () => assert.fail("no acknowledgement yet"));

  const queries = [];
  const confirmations = [];
  const read = async (path, options) => {
    assert.equal(options.method, undefined);
    queries.push(path);
    return { ...first, message_id: "saved-id", conversation_id: "conversation",
      role: "user", save_status: "saved", server_sequence: 1 };
  };
  assert.equal(await queue.sendHead(read, signal, () => {}, item => confirmations.push(item), () => false), true);
  assert.equal(await queue.sendHead(read, signal, () => {}, () => assert.fail("unsent"), () => false), false);
  assert.equal(queries.length, 1);
  assert.equal(confirmations[0].message_id, "saved-id");
  assert.deepEqual(queue.snapshot(), [second]);
});
