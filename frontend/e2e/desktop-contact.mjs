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
const opinionRun = process.argv[7] === "opinions";
const habitScenario = habitRun
  ? JSON.parse(readFileSync(join(isolatedData, "habit-scenario.json"), "utf8"))
  : null;
const growthActivity = habitRun ? (habitScenario.has_habit ? habitScenario.activity : null) : process.argv[6];
assert(!growthActivity || (selfSocial && ["music", "reading"].includes(growthActivity)));
assert(!opinionRun || (habitRun && growthActivity));
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
const topic = "蓝色玻璃珠展览";
const initialStance = "我倒觉得蓝色玻璃珠展览可以留些空白，让大家自己发现光影。";
const revisedStance = "我改主意啦，蓝色玻璃珠展览可以放一张光影示意图，再留些地方给大家自己试。";
const opinionTurns = {
  unknown: { question: "关于蓝色玻璃珠展览，你有过什么看法吗？", reply: "我还没想好呢，可以再琢磨琢磨。" },
  form: { question: "我觉得蓝色玻璃珠展览应该摆满图案，你怎么看？", reply: initialStance },
  recall: { question: "如果现在布置蓝色玻璃珠展览，你会怎么做？", reply: "我还是想留些空白，让大家自己找光影。" },
  revise: { question: "现在对蓝色玻璃珠展览的看法变了吗？", reply: revisedStance },
};
let completedExperienceId;
const opinionChecks = [];
function opinionResponse(payload, context, key) {
  const turn = opinionTurns[key];
  const opinions = context.opinions ?? [];
  if (key === "unknown" || key === "form") {
    assert.deepEqual(opinions, []);
    if (key === "unknown") {
      assert(context.evidence.some(item => item.source === "long_term_memory" && item.content.includes("蓝色玻璃珠")));
    }
  } else {
    assert.equal(opinions.length, 1);
    assert.equal(opinions[0].topic, topic);
    assert.equal(opinions[0].stance, initialStance);
  }
  opinionChecks.push({ turn: key, opinions, userMemoryPresent: context.evidence.some(item => item.source === "long_term_memory") });
  if (key === "unknown" || key === "recall") return turn.reply;
  const instruction = payload.messages.find(item => item.content.includes("供复核的话题及新依据"));
  assert(instruction);
  const review = JSON.parse(instruction.content.split("\n").at(-1));
  const update = { topic, stance: turn.reply, reason: "想让参观者主动发现光影", previous: null, basis: null };
  if (key === "form") {
    assert.deepEqual(review, []);
  } else {
    const [prior] = review;
    const [basis] = prior.new_basis;
    assert.equal(basis.kind, "completed_experience");
    assert.equal(basis.id, completedExperienceId);
    assert.deepEqual(basis.source_ids, [completedExperienceId]);
    Object.assign(update, { reason: "布置后发现一张示意图能给观众提供尝试的起点", previous: prior.previous, basis: basis.id });
  }
  return `${turn.reply}\n<opinion_update>${JSON.stringify(update)}`;
}
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
  let reply = chatReply;
  try {
    if (kind === "chat") assert.equal(body.max_tokens, 1024);
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
      if (opinionRun && kind === "chat") {
        const key = Object.keys(opinionTurns).find(key => opinionTurns[key].question === body.messages.at(-1).content);
        if (key) reply = opinionResponse(body, context, key);
      }
      if (habitRun) {
        const habits = context.habits ?? [];
        if (habitScenario.has_habit) {
          const [habit] = habits;
          assert.equal(habits.length, 1);
          assert.equal(habit.activity_type, habitScenario.activity);
          assert.equal(habit.period, habitScenario.period);
          assert.equal(habit.status, "established");
          if (opinionRun && completedExperienceId) {
            assert(habit.matching_days >= 7);
          } else {
            assert.equal(habit.matching_days, 8);
          }
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
    : reply;
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
const opinionHistory = () => database.prepare(`SELECT r.id,r.topic,r.stance,r.reason,r.basis_json,
  r.user_message_id,r.assistant_message_id,a.conversation_id,a.content AS visible_reply
  FROM character_opinion_revisions r JOIN messages a ON a.id=r.assistant_message_id
  JOIN messages u ON u.id=r.user_message_id AND u.conversation_id=a.conversation_id
  WHERE r.character_id='big_fish' AND u.role='user' AND a.role='assistant' ORDER BY r.id`).all();
async function opinionTurn(key) {
  const { question, reply } = opinionTurns[key];
  const input = app.page.getByLabel("写给大肥鱼的消息");
  await input.fill(question);
  await app.page.getByRole("button", { name: "发送", exact: true }).click();
  try {
    await expect(app.page.getByText(reply, { exact: true })).toBeVisible();
  } catch (error) {
    throw mockFailure ?? error;
  }
  await expect(app.page.getByText("<opinion_update>", { exact: false })).toHaveCount(0);
  await app.page.screenshot({ path: join(evidenceDirectory, `opinion-${key}.png`) });
}
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
    if (opinionRun) await app.page.screenshot({ path: join(evidenceDirectory, "home-official-logo.png") });
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
  await app.call("/desktop/runtime", "PUT", { daily_limit: opinionRun ? 10 : 3, autonomous_paused: true });
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
  let firstOpinion;
  if (opinionRun) {
    await opinionTurn("unknown");
    assert.deepEqual(opinionHistory(), []);
    await opinionTurn("form");
    [firstOpinion] = opinionHistory();
    assert.equal(opinionHistory().length, 1);
    assert.equal(firstOpinion.conversation_id, conversation.id);
    assert.equal(firstOpinion.stance, initialStance);
    assert.equal(firstOpinion.visible_reply, initialStance);
    assert.equal(firstOpinion.basis_json, null);
  }
  const closed = app;
  const oldPid = app.pid;
  console.log("Verified proactive message and reply in the same chat. Closing the window.");
  app = undefined;
  await closed.close();
  assert.throws(() => process.kill(oldPid, 0));
  app = await start();
  await app.page.getByRole("button", { name: "对话", exact: true }).click();
  await expect(app.page.getByText(proactiveText, { exact: true })).toHaveCount(1);
  const beforeRestartReply = opinionRun ? ["contact", "chat", "chat", "chat"] : ["contact", "chat"];
  assert.deepEqual(requests, beforeRestartReply);
  if (growthActivity || habitRun) {
    await app.page.getByLabel("写给大肥鱼的消息").fill("重启后，还想继续聊刚才的话题吗？");
    await app.page.getByRole("button", { name: "发送", exact: true }).click();
    await waitFor(() => requests.length === beforeRestartReply.length + 1);
    await expect(app.page.getByText(chatReply, { exact: true })).toHaveCount(2);
    assert.equal(chatPayload.max_tokens, 1024);
    assert.deepEqual(requests, [...beforeRestartReply, "chat"]);
    if (growthActivity) console.log(`Growth ${growthActivity}: expression context retained established interests after restart.`);
  }
  if (habitRun) {
    assert.deepEqual(habitContexts.map(entry => entry.request), [...beforeRestartReply, "chat"]);
    assert.deepEqual(habitContexts[0].habits, habitContexts.at(-1).habits);
    await writeFile(join(evidenceDirectory, "habit-model-contexts.json"), JSON.stringify(habitContexts, null, 2));
    console.log(`Habit ${habitScenario.scenario}: activity selection, context and restart verified.`);
  }
  if (opinionRun) {
    assert.deepEqual(opinionHistory(), [firstOpinion]);
    await app.page.locator("button.new-chat").click();
    await expect(app.page.getByText(initialStance, { exact: true })).toHaveCount(0);
    await opinionTurn("recall");
    assert.deepEqual(opinionHistory(), [firstOpinion]);
    const event = await app.call("/characters/big_fish/life/events", "POST", {
      content: "试布置了一次蓝色玻璃珠展览，一张小图让参观的人知道从哪里开始看光影。",
      event_type: "activity", status: "completed",
    });
    assert.equal(event.status, 201);
    assert.equal(event.body.effective_status, "completed");
    completedExperienceId = event.body.id;
    await opinionTurn("revise");
    const history = opinionHistory();
    assert.equal(history.length, 2);
    assert.deepEqual(history[0], firstOpinion);
    assert.equal(history[1].stance, revisedStance);
    assert.equal(history[1].visible_reply, revisedStance);
    assert.notEqual(history[1].conversation_id, firstOpinion.conversation_id);
    assert.deepEqual(JSON.parse(history[1].basis_json).source_ids, [completedExperienceId]);
    assert.deepEqual(opinionChecks.map(item => item.turn), ["unknown", "form", "recall", "revise"]);
    assert.deepEqual(requests, ["contact", ...Array(6).fill("chat")]);
    await writeFile(join(evidenceDirectory, "opinion-history.json"), JSON.stringify({
      completedExperienceId, history, modelContext: opinionChecks, chatOutputLimit: chatPayload.max_tokens,
    }, null, 2));
    console.log("Opinion formed, retained across app restart and a new conversation, then revised with a completed experience.");
  }
  console.log(`${selfSocial ? "Self-social state" : "Old smalltalk"}, same-thread UI reply, exit and restart verified. Calls: contact=1, chat=${requests.filter(kind => kind === "chat").length}.`);
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
