import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { once } from "node:events";
import { writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { DatabaseSync } from "node:sqlite";
import { join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { chromium, expect } from "@playwright/test";

const [binary, isolatedData, evidenceDirectory] = process.argv.slice(2).map(value => resolve(value));
assert(process.platform === "win32" && binary && isolatedData && evidenceDirectory);
const installedFixture = resolve(process.env.LOCALAPPDATA, "com.soulseed.desktop", "data");
assert(isolatedData.toLowerCase().includes("\\desktop-build\\qa\\") ||
  (process.env.SOULSEED_INSTALLED_QA === "1" && isolatedData.toLowerCase() === installedFixture.toLowerCase()));
const database = new DatabaseSync(join(isolatedData, "soulseed-desktop.db"));
assert.equal(database.prepare("SELECT COUNT(*) AS n FROM messages WHERE role='user'").get().n, 1);
assert.equal(database.prepare("SELECT COUNT(*) AS n FROM desktop_model_config").get().n, 0);
const [conversation] = database.prepare("SELECT id FROM conversations").all();
assert(conversation);

const requests = [];
let chatPayload;
let allowContact;
const contactRelease = new Promise(resolve => { allowContact = resolve; });
const mock = createServer(async (request, response) => {
  const parts = [];
  for await (const part of request) parts.push(part);
  const body = JSON.parse(Buffer.concat(parts));
  let signals;
  try { signals = JSON.parse(body.messages.at(-1).content); } catch {}
  const kind = signals?.choices ? "contact" : "chat";
  requests.push(kind);
  if (kind === "chat") chatPayload = body;
  if (kind === "contact") await contactRelease;
  const content = kind === "contact"
    ? JSON.stringify({ decision: "contact", choice: signals.choices[0].id, style: "curious",
      message: "今天想给你丢一颗快乐泡泡，希望它不会在半路上啪的一声破掉！", evidence_ids: signals.choices[0].evidence_ids,
      claims: [] })
    : "我接住啦，泡泡还在！";
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
    await expect(page.getByRole("heading", { name: /大肥鱼的.*生活世界/ })).toBeVisible();
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
  await expect(app.page.getByText(/今天想给你丢一颗快乐泡泡/)).toBeVisible();
  await app.page.getByLabel("写给大肥鱼的消息").fill("我接住了！");
  await app.page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(app.page.getByText("我接住啦，泡泡还在！")).toBeVisible();
  await app.page.screenshot({ path: join(evidenceDirectory, "contact-conversation.png") });
  assert.deepEqual(requests, ["contact", "chat"]);
  assert.equal(chatPayload.max_tokens, 1024);
  const chatContext = chatPayload.messages.map(item => item.content).join("\n");
  assert.match(chatContext, /童真、活泼、好奇、淘气/);
  assert.match(chatContext, /最近在听音乐/);
  assert.match(chatContext, /今天想给你丢一颗快乐泡泡/);
  await app.page.getByRole("button", { name: /模型设置/ }).click();
  await app.page.getByRole("button", { name: "一键暂停自主功能" }).click();
  await expect(app.page.getByText(/主动联系：已暂停/)).toBeVisible();
  await app.page.getByRole("button", { name: "取消", exact: true }).click();
  const closed = app;
  const oldPid = app.pid;
  console.log("Verified proactive message and reply in the same chat. Closing the window.");
  app = undefined;
  await closed.close();
  assert.throws(() => process.kill(oldPid, 0));
  app = await start();
  await app.page.getByRole("button", { name: "对话", exact: true }).click();
  await expect(app.page.getByText(/今天想给你丢一颗快乐泡泡/)).toHaveCount(1);
  assert.deepEqual(requests, ["contact", "chat"]);
  console.log("Old smalltalk, same-thread UI reply, exit and restart verified. Calls: contact=1, chat=1.");
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
