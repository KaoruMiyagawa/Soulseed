import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { once } from "node:events";
import { readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { DatabaseSync } from "node:sqlite";
import { join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { chromium, expect } from "@playwright/test";

const [binary, isolatedData, evidenceDirectory] = process.argv.slice(2, 5).map(value => resolve(value));
const selfSocial = process.argv[5] === "self";
const habitRun = process.argv[6] === "habit";
const habitScenario = habitRun
  ? JSON.parse(readFileSync(join(isolatedData, "habit-scenario.json"), "utf8"))
  : null;
const growthActivity = habitRun ? (habitScenario.has_habit ? habitScenario.activity : null) : process.argv[6];
assert(!growthActivity || (selfSocial && ["music", "reading"].includes(growthActivity)));
assert(process.platform === "win32" && binary && isolatedData && evidenceDirectory);
const installedFixture = resolve(process.env.LOCALAPPDATA, "com.soulseed.desktop", "data");
assert(isolatedData.toLowerCase().includes("\\desktop-build\\qa\\") ||
  (process.env.SOULSEED_INSTALLED_QA === "1" && isolatedData.toLowerCase() === installedFixture.toLowerCase()));
const database = new DatabaseSync(join(isolatedData, "soulseed-desktop.db"));
assert.equal(database.prepare("SELECT COUNT(*) AS n FROM messages WHERE role='user'").get().n, 1);
if (selfSocial) {
  const historical = database.prepare("SELECT created_at FROM messages WHERE role='user'").get();
  assert(Date.now() - Date.parse(historical.created_at) > 90 * 24 * 60 * 60 * 1000,
    "self-social QA must not have a recent user interaction");
}
assert.equal(database.prepare("SELECT COUNT(*) AS n FROM desktop_model_config").get().n, 0);
const [conversation] = database.prepare("SELECT id FROM conversations").all();
assert(conversation);

const requests = [];
const habitContexts = [];
let chatPayload;
let mockFailure;
const interestText = growthActivity === "music" ? "音乐" : "读书";
const proactiveText = growthActivity
  ? `好想和你聊聊${interestText}呀，光想想就有点雀跃！`
  : "今天想给你丢一颗快乐泡泡，希望它不会在半路上啪的一声破掉！";
const chatReply = habitRun
  ? (habitScenario.has_habit ? `我${habitScenario.period}常想听你聊聊${interestText}。` : "目前还没有形成固定的时段习惯。")
  : (growthActivity ? `比起别的，我更想聊聊${interestText}！` : "我接住啦，泡泡还在！");
function checkGrowth(growth) {
  assert.equal(growth.length, 1);
  assert.equal(growth[0].activity_type, growthActivity);
  assert.equal(growth[0].tendency, "established");
  assert.equal(growth[0].negative_experiences, 0);
  assert(growth[0].positive_experiences >= 20);
}
let allowContact;
const contactRelease = new Promise(resolve => { allowContact = resolve; });
const mock = createServer(async (request, response) => {
  const parts = [];
  for await (const part of request) parts.push(part);
  const body = JSON.parse(Buffer.concat(parts));
  let signals;
  try { signals = JSON.parse(body.messages.at(-1).content); } catch {}
  const kind = signals?.choices ? "contact" : "chat";
  try {
    if (kind === "contact" && selfSocial) {
      assert.equal(signals.candidate_type, "self_social");
      assert(signals.memories.some(item => item.includes("蓝色玻璃珠")));
    }
    if (growthActivity || habitRun) {
      const expression = kind === "chat" && body.messages.find(item =>
        item.role === "system" && item.content.includes("<expression_context"));
      const context = kind === "contact" ? signals
        : (expression ? JSON.parse(expression.content.split("\n").find(line => line.startsWith("{"))) : {});
      if (growthActivity) checkGrowth(context.personal_growth);
      if (habitRun) {
        const habits = context.habits ?? [];
        if (habitScenario.has_habit) {
          const [habit] = habits;
          assert.equal(habits.length, 1);
          assert.equal(habit.activity_type, habitScenario.activity);
          assert.equal(habit.period, habitScenario.period);
          assert.equal(habit.status, "established");
          assert.equal(habit.matching_days, 8);
        } else {
          assert.deepEqual(habits, []);
          assert.deepEqual(context.personal_growth ?? [], []);
        }
        habitContexts.push({ request: kind, habits });
      }
    }
  } catch (error) {
    mockFailure = error;
    response.writeHead(400).end();
    return;
  }
  requests.push(kind);
  if (kind === "chat") chatPayload = body;
  if (kind === "contact") await contactRelease;
  const content = kind === "contact"
    ? JSON.stringify({ decision: "contact", choice: signals.choices[0].id, style: "curious",
      message: proactiveText, evidence_ids: signals.choices[0].evidence_ids,
      claims: [] })
    : chatReply;
  response.setHeader("Content-Type", "application/json");
  response.end(JSON.stringify({ choices: [{ message: { content }, finish_reason: "stop" }] }));
});
mock.listen(0, "127.0.0.1");
await once(mock, "listening");
const mockPort = mock.address().port;

const toastDb = new DatabaseSync(join(process.env.LOCALAPPDATA, "Microsoft/Windows/Notifications/wpndatabase.db"), { readOnly: true });
const toastRows = tag => toastDb.prepare(`SELECT n.Tag, n.Payload, h.PrimaryId FROM Notification n
  JOIN NotificationHandler h ON h.RecordId=n.HandlerId WHERE h.PrimaryId=? AND n.Tag=?`).all("com.soulseed.desktop", tag);
async function waitFor(read, ms = 25_000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    try { const value = await read(); if (value) return value; } catch {}
    if (mockFailure) throw mockFailure;
    await sleep(250);
  }
  throw new Error("Desktop contact QA timed out");
}

async function start() {
  const temp = createServer();
  temp.listen(0, "127.0.0.1");
  await once(temp, "listening");
  const port = temp.address().port;
  await new Promise(resolve => temp.close(resolve));
  const child = spawn(binary, [], { env: {
    ...process.env,
    WEBVIEW2_USER_DATA_FOLDER: join(isolatedData, "WebView"),
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port}`,
  }, stdio: "ignore" });
  let browser;
  const close = async () => {
    if (child.exitCode === null) {
      execFileSync("powershell.exe", ["-NoProfile", "-File", fileURLToPath(new URL("./contact-window.ps1", import.meta.url)),
        "-ProcessId", String(child.pid), "-Command", "Close"], { windowsHide: true, timeout: 15000 });
    }
    await browser?.close().catch(() => {});
  };
  try {
    browser = await waitFor(() => chromium.connectOverCDP(`http://127.0.0.1:${port}`));
    const page = browser.contexts()[0].pages()[0];
    await expect(page.getByRole("heading", { name: /大肥鱼的.*生活世界/ })).toBeVisible({ timeout: 30_000 });
    const call = (path, method = "GET", body = null) => page.evaluate(async ({ path, method, body }) => {
      const result = await window.__TAURI__.core.invoke("desktop_request", {
        path, method, body: body === null ? null : JSON.stringify(body),
      });
      return { status: result.status, body: JSON.parse(result.body) };
    }, { path, method, body });
    const pid = Number(execFileSync("powershell.exe", ["-NoProfile", "-Command",
      `(Get-CimInstance Win32_Process -Filter "ParentProcessId=${child.pid} AND Name='soulseed-backend.exe'").ProcessId`],
    { encoding: "utf8", windowsHide: true }).trim());
    assert(pid);
    return { close, page, call, pid, child };
  } catch (error) {
    await close();
    throw error;
  }
}

let app;
try {
  app = await start();
  if (habitRun) {
    const life = await app.call("/characters/big_fish/life");
    assert.equal(life.status, 200);
    assert.equal(life.body.state.timezone_id, habitScenario.zone_id);
    const active = life.body.state.current_activity;
    assert.equal(active.event_type, habitScenario.expected);
    if (habitScenario.plan_id) {
      assert.equal(active.id, habitScenario.plan_id);
      assert.equal(active.generation_method, "manual");
    } else {
      assert.match(active.selection_reason, new RegExp(`slot=${habitScenario.slot};`));
      assert.equal(active.generation_method, "rule_engine");
      if (habitScenario.scenario === "repeat") {
        assert.match(active.selection_reason, /recent=\['reading'/);
      }
      if (["evening", "morning"].includes(habitScenario.scenario)) {
        assert.equal(habitScenario.baseline_margin, 0.12);
        assert.match(active.selection_reason, /habit_bonus=0\.240/);
      } else {
        assert.doesNotMatch(active.selection_reason, /habit_bonus=/);
      }
    }
    const logo = app.page.locator("img.brand-mark");
    await expect(logo).toBeVisible();
    await expect.poll(() => logo.evaluate(img => img.naturalWidth)).toBeGreaterThan(0);
    await writeFile(join(evidenceDirectory, "activity-selection.json"), JSON.stringify({
      ...habitScenario, selected: active.event_type, selectionReason: active.selection_reason,
    }, null, 2));
  } else if (growthActivity) {
    const active = await waitFor(() => database.prepare(
      "SELECT event_type, selection_reason FROM life_events WHERE generation_method='rule_engine' AND activity_status='active'"
    ).get());
    assert.equal(active.event_type, growthActivity);
    assert.match(active.selection_reason, /^preference=2\.00;/);
  }
  assert.equal((await app.call("/desktop/runtime")).body.contact_enabled, false);
  assert.equal((await app.call("/desktop/model", "PUT", {
    base_url: `http://127.0.0.1:${mockPort}/v1`, model: "qa", api_key: "qa-only-no-secret",
  })).status, 200);
  await app.call("/desktop/runtime", "PUT", { daily_limit: 3, autonomous_paused: true });
  await app.page.reload();
  await app.page.getByRole("button", { name: /模型设置/ }).click();
  await app.page.getByRole("button", { name: "启用主动联系" }).click();
  await app.page.getByRole("button", { name: "恢复自主功能" }).click();
  await expect(app.page.getByText(/主动联系：已启用/)).toBeVisible();
  await app.page.getByRole("button", { name: "取消", exact: true }).click();
  await app.page.getByRole("button", { name: "对话", exact: true }).click();
  await waitFor(() => requests.includes("contact"), 75_000);
  assert.equal(database.prepare("SELECT COUNT(*) AS n FROM messages WHERE role='user'").get().n, 1);
  execFileSync("powershell.exe", ["-NoProfile", "-File", fileURLToPath(new URL("./contact-window.ps1", import.meta.url)),
    "-ProcessId", String(app.child.pid), "-Command", "Minimize"], { windowsHide: true });
  allowContact();
  const delivered = await waitFor(() => database.prepare("SELECT id,conversation_id FROM proactive_messages WHERE status='delivered'").get());
  assert.equal(delivered.conversation_id, conversation.id);
  const tag = delivered.id.replaceAll("-", "").slice(0, 16);
  let toast;
  try {
    toast = await waitFor(() => toastRows(tag)[0]);
  } catch (error) {
    console.error("Native toast did not appear in the Windows notification database:", tag);
  }
  if (toast) {
    assert.equal(toast.PrimaryId, "com.soulseed.desktop");
    assert.match(Buffer.from(toast.Payload).toString("utf8"), /大肥鱼发来了一条新消息/);
    assert.equal(toastRows(tag).length, 1);
    await writeFile(join(evidenceDirectory, "contact-windows-toast.json"), JSON.stringify({
      appUserModelId: toast.PrimaryId,
      tag,
      messageId: delivered.id,
      windowState: "minimized",
      payload: Buffer.from(toast.Payload).toString("utf8"),
    }, null, 2));
  }
  execFileSync("powershell.exe", ["-NoProfile", "-File", fileURLToPath(new URL("./contact-window.ps1", import.meta.url)),
    "-ProcessId", String(app.child.pid), "-Command", "Restore"], { windowsHide: true });
  await expect(app.page.getByText(proactiveText, { exact: true })).toBeVisible();
  const replyInput = habitRun ? "我喜欢蓝色玻璃珠手链。你平时有什么生活习惯？" : "我接住了，也喜欢蓝色玻璃珠手链！";
  await app.page.getByLabel("写给大肥鱼的消息").fill(replyInput);
  await app.page.getByRole("button", { name: "发送", exact: true }).click();
  if (growthActivity || habitRun) await waitFor(() => requests.length === 2);
  await expect(app.page.getByText(chatReply, { exact: true })).toBeVisible();
  await app.page.screenshot({ path: join(evidenceDirectory, "contact-conversation.png") });
  assert.deepEqual(requests, ["contact", "chat"]);
  assert.equal(chatPayload.max_tokens, 1024);
  const chatContext = chatPayload.messages.map(item => item.content).join("\n");
  assert.match(chatContext, /童真、活泼、好奇、淘气/);
  if (!selfSocial) assert.match(chatContext, /最近在听音乐/);
  assert(chatContext.includes(proactiveText));
  if (selfSocial) assert.match(chatContext, /蓝色玻璃珠/);
  await app.page.getByRole("button", { name: /模型设置/ }).click();
  await app.page.getByRole("button", { name: "一键暂停自主功能" }).click();
  await expect(app.page.getByText(/主动联系：已暂停/)).toBeVisible();
  await app.page.getByRole("button", { name: "取消", exact: true }).click();
  await app.page.getByRole("button", { name: "手帐", exact: true }).click();
  await expect(app.page.getByRole("heading", { name: "生活手帐" })).toBeVisible();
  await app.page.getByRole("button", { name: "计划", exact: true }).click();
  await expect(app.page.getByRole("heading", { name: "今日计划" })).toBeVisible();
  if (habitScenario?.plan_id) {
    await expect(app.page.getByRole("heading", { name: "先整理小角落（验收计划）", exact: true })).toBeVisible();
  }
  await app.page.getByRole("button", { name: "探索", exact: true }).click();
  await expect(app.page.getByRole("heading", { name: "探索与发现" })).toBeVisible();
  await app.page.getByRole("button", { name: "她的世界", exact: true }).click();
  await expect(app.page.getByRole("heading", { name: /大肥鱼的.*生活世界/ })).toBeVisible();
  await app.page.getByRole("button", { name: "对话", exact: true }).click();
  const closed = app;
  const oldPid = app.pid;
  console.log("Verified proactive message and reply in the same chat. Closing the window.");
  app = undefined;
  await closed.close();
  assert.throws(() => process.kill(oldPid, 0));
  app = await start();
  await app.page.getByRole("button", { name: "对话", exact: true }).click();
  await expect(app.page.getByText(proactiveText, { exact: true })).toHaveCount(1);
  assert.deepEqual(requests, ["contact", "chat"]);
  if (growthActivity || habitRun) {
    await app.page.getByLabel("写给大肥鱼的消息").fill("重启后，还想继续聊刚才的话题吗？");
    await app.page.getByRole("button", { name: "发送", exact: true }).click();
    await waitFor(() => requests.length === 3);
    await expect(app.page.getByText(chatReply, { exact: true })).toHaveCount(2);
    assert.equal(chatPayload.max_tokens, 1024);
    assert.deepEqual(requests, ["contact", "chat", "chat"]);
    if (growthActivity) console.log(`Growth ${growthActivity}: expression context retained established interests after restart.`);
  }
  if (habitRun) {
    assert.deepEqual(habitContexts.map(entry => entry.request), ["contact", "chat", "chat"]);
    assert.deepEqual(habitContexts[0].habits, habitContexts[2].habits);
    await writeFile(join(evidenceDirectory, "habit-model-contexts.json"), JSON.stringify(habitContexts, null, 2));
    console.log(`Habit ${habitScenario.scenario}: activity selection, context and restart verified.`);
  }
  console.log(`${selfSocial ? "Self-social state" : "Old smalltalk"}, same-thread UI reply, exit and restart verified. Calls: contact=1, chat=${growthActivity || habitRun ? 2 : 1}.`);
  assert(toast, "Windows toast did not appear");
  assert.equal(toastRows(tag).length, 1);
  console.log("Windows AUMID toast verified.");
} finally {
  allowContact();
  try { await app?.close(); }
  finally {
    await new Promise(resolve => mock.close(resolve));
    toastDb.close();
    database.close();
  }
}
