import test from "node:test";
import assert from "node:assert/strict";
import { confirmDeliveryAndSync } from "../src/proactiveDelivery.ts";

const ready = { id: "proactive-1", content: "一件真实保存的经历", status: "ready" };

test("delivery acknowledgement failure can retry the same message ID", async () => {
  let attempts = 0;
  let inserted = false;
  const request = async (path) => {
    if (path.endsWith("/delivered")) {
      attempts += 1;
      if (attempts === 1) throw new Error("ack response lost");
      inserted = true;
      return { ...ready, status: "delivered" };
    }
    return { id: "conversation-1", messages: inserted ? [{ id: ready.id, role: "assistant", content: ready.content }] : [] };
  };

  await assert.rejects(confirmDeliveryAndSync(ready, "conversation-1", "/contact", request), /ack response lost/);
  const retry = await confirmDeliveryAndSync(ready, "conversation-1", "/contact", request);
  assert.equal(attempts, 2);
  assert.equal(retry.delivered.id, ready.id);
  assert.equal(retry.conversation.messages.length, 1);
  assert.equal(retry.conversation.messages[0].id, ready.id);
});

test("history refresh failure retries idempotent delivery before syncing again", async () => {
  let ackCount = 0;
  let historyCount = 0;
  const serverMessages = new Map();
  const request = async (path, options) => {
    if (path.endsWith("/delivered")) {
      ackCount += 1;
      const { conversation_id } = JSON.parse(options.body);
      serverMessages.set(ready.id, { id: ready.id, conversation_id, role: "assistant", content: ready.content });
      return { ...ready, status: "delivered" };
    }
    historyCount += 1;
    if (historyCount === 1) throw new Error("history unavailable");
    return { id: "conversation-1", messages: [...serverMessages.values()] };
  };

  await assert.rejects(confirmDeliveryAndSync(ready, "conversation-1", "/contact", request), /history unavailable/);
  const retry = await confirmDeliveryAndSync(ready, "conversation-1", "/contact", request);
  assert.equal(ackCount, 2);
  assert.equal(historyCount, 2);
  assert.equal(serverMessages.size, 1);
  assert.equal(retry.conversation.messages.length, 1);
  assert.equal(retry.conversation.messages[0].id, ready.id);
});
