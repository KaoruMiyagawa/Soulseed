import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { access } from "node:fs/promises";
import { createServer, get } from "node:http";
import { join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { setTimeout as sleep } from "node:timers/promises";
import { chromium, expect } from "@playwright/test";

const [path, mode, screenshot] = process.argv.slice(2);
if (process.platform !== "win32" || !path || !["seed", "verify", "crash"].includes(mode)) {
  throw new Error("Usage: node windows-desktop-smoke.mjs <installed Soulseed.exe> <seed|verify|crash> [screenshot.png]");
}

const testKey = "soulseed-smoke-key-test-only";
const message = "这是本机安装验收专用消息。";
const reply = "这条回复来自本机的模拟模型服务。";
const accountDir = join(process.env.LOCALAPPDATA, "com.soulseed.desktop", "data");
const databasePath = join(accountDir, "soulseed-desktop.db");
if (mode === "seed") {
  const existing = await access(databasePath).then(() => true, error => {
    if (error.code !== "ENOENT") throw error;
    return false;
  });
  assert(!existing, "Run seed mode on a disposable Windows account without Soulseed desktop data.");
}
const system32 = join(process.env.SystemRoot, "System32");
const powershell = join(system32, "WindowsPowerShell", "v1.0", "powershell.exe");
const ps = code => execFileSync(powershell, ["-NoProfile", "-NonInteractive", "-Command", code], {
  windowsHide: true, encoding: "utf8",
}).trim();

async function eventually(check, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  let lastError;
  do {
    try {
      const value = await check();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(250);
  } while (Date.now() < deadline);
  throw lastError ?? new Error("Windows smoke check timed out");
}

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

function status(url, headers) {
  return new Promise((resolve, reject) => {
    const request = get(url, { headers }, response => {
      response.resume();
      resolve(response.statusCode);
    });
    request.setTimeout(750, () => request.destroy(new Error("Loopback probe timed out")));
    request.on("error", reject);
  });
}

let attempts = 0;
const mock = createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const payload = JSON.parse(Buffer.concat(chunks));
  if (request.url !== "/v1/chat/completions" || request.headers.authorization !== `Bearer ${testKey}`
    || payload.model !== "smoke-model" || payload.max_tokens !== 1024) {
    response.writeHead(400).end();
    return;
  }
  attempts += 1;
  response.writeHead(200, { "Content-Type": "application/json" });
  response.end(JSON.stringify({ choices: [{ message: { content: reply }, finish_reason: "stop" }] }));
});

const mockPort = await listen(mock);
const reserve = createServer();
const debugPort = await listen(reserve);
await new Promise(resolve => reserve.close(resolve));
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toUpperCase() !== "PATH"));
env.Path = `${system32};${process.env.SystemRoot}`;
env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = `--remote-debugging-port=${debugPort}`;
// These inherited development settings must have zero effect on the desktop service.
env.SOULSEED_ENVIRONMENT = "testing";
env.SOULSEED_PORT = "8001";
env.SOULSEED_CHAT_ASYNC_MOCK_ENABLED = "true";
const child = spawn(resolve(path), [], { env, stdio: "ignore" });
let browser;
let sidecarPid;
try {
  await eventually(async () => await status(`http://127.0.0.1:${debugPort}/json/version`) === 200);
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
  const page = browser.contexts()[0].pages()[0];
  page.setDefaultTimeout(15_000);
  await expect(page.getByRole("heading", { name: /大肥鱼的.*生活世界/ })).toBeVisible();
  assert.equal(new URL(page.url()).host, "tauri.localhost");
  await expect(page.locator(".room-scene")).toBeVisible();
  assert(await page.locator(".room-scene").evaluate(img => img.complete && img.naturalWidth > 0));
  await expect(page.getByText("本地配对口令")).toHaveCount(0);
  if (screenshot) await page.screenshot({ path: screenshot });

  sidecarPid = Number(await eventually(() => ps(
    `(Get-CimInstance Win32_Process -Filter "ParentProcessId=${child.pid} AND Name='soulseed-backend.exe'").ProcessId`,
  )));
  const listener = JSON.parse(ps(`Get-NetTCPConnection -OwningProcess ${sidecarPid} -State Listen |
    Select-Object LocalAddress,LocalPort | ConvertTo-Json -Compress`));
  assert.equal(listener.LocalAddress, "127.0.0.1");
  assert(![5174, 8001].includes(listener.LocalPort));
  const direct = `http://127.0.0.1:${listener.LocalPort}/api/health`;
  assert.equal(await status(direct), 401);
  assert.equal(await status(direct, { Origin: "http://example.invalid" }), 403);
  const api = (path, method = "GET", body = null) => page.evaluate(async ({ path, method, body }) => {
    const response = await window.__TAURI__.core.invoke("desktop_request", {
      path, method, body: body === null ? null : JSON.stringify(body),
    });
    return { status: response.status, body: JSON.parse(response.body) };
  }, { path, method, body });
  const health = await api("/health");
  assert.equal(health.body.environment, "production");
  assert.equal(health.body.chat_reply_mode, "disabled");
  assert.equal((await api("/characters/big_fish/exploration-autonomy", "PUT", { enabled: true })).status, 403);
  assert.equal((await api("/characters/big_fish/contact/check", "POST")).status, 403);

  await page.getByRole("button", { name: "对话", exact: true }).click();
  if (mode === "seed") {
    assert.deepEqual((await api("/conversations")).body, []);
    using db = new DatabaseSync(databasePath, { readOnly: true });
    assert.equal(db.prepare("SELECT COUNT(*) AS count FROM memory_items").get().count, 0);
    await page.getByRole("button", { name: "新会话" }).click();
    await expect(page.getByText("请先在「模型设置」保存你自己的模型配置。")).toBeVisible();
    await page.getByLabel("写给大肥鱼的消息").fill(message);
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
    const [{ id }] = (await api("/conversations")).body;
    const messages = `/conversations/${id}/messages`;
    assert.equal((await api(messages, "POST", { content: message })).status, 409);
    assert.equal((await api(`/conversations/${id}/user-messages`, "POST", {
      content: message, client_message_id: crypto.randomUUID(), client_stream_id: crypto.randomUUID(), client_sequence: 1,
    })).status, 503);
    assert.deepEqual((await api(messages)).body.messages, []);

    await page.getByRole("button", { name: /模型设置/ }).click();
    await page.getByLabel("API 基础地址").fill(`http://127.0.0.1:${mockPort}/v1`);
    await page.getByLabel("模型 ID").fill("smoke-model");
    await page.getByLabel("API Key", { exact: true }).fill(testKey);
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect(page.getByText("同步聊天 · 异步回复未开放")).toBeVisible();
    assert.equal(attempts, 0);
    const { ciphertext } = db.prepare("SELECT ciphertext FROM desktop_model_config").get();
    assert(!Buffer.from(ciphertext).includes(Buffer.from(testKey)));
    assert(!JSON.stringify(await api("/desktop/model")).includes(testKey));
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByText(reply, { exact: true })).toBeVisible();
    assert.equal(attempts, 1);

    for (let i = 2; i <= 20; i++) {
      assert.equal((await api(messages, "POST", { content: `本机额度验收第 ${i} 次。` })).status, 200);
    }
    assert.equal((await api(messages, "POST", { content: "本机额度验收超额。" })).status, 429);
    assert.equal(attempts, 20);
    await page.getByRole("button", { name: "探索", exact: true }).click();
    await expect(page.getByText(/手动探索和后台自主探索.*暂未开放/)).toBeVisible();
    console.log("First launch, same UI, auth, no runtime dependencies on PATH, DPAPI, sync reply and 20-request cap passed.");
  } else {
    await expect(page.getByText(message, { exact: true })).toBeVisible();
    const configuration = (await api("/desktop/model")).body;
    assert.equal(configuration.configured, true);
    assert.equal(configuration.model, "smoke-model");
    assert.equal(new URL(configuration.base_url).hostname, "127.0.0.1");
    await api("/desktop/model", "PUT", {
      base_url: `http://127.0.0.1:${mockPort}/v1`, model: "smoke-model", api_key: testKey,
    });
    const [{ id }] = (await api("/conversations")).body;
    assert.equal((await api(`/conversations/${id}/messages`, "POST", { content: "重开后额度仍保留。" })).status, 429);
    assert.equal(attempts, 0);
    console.log("Existing conversation, DPAPI settings and strict budget survive restart/update.");
  }
} finally {
  if (child.exitCode === null) {
    const exited = once(child, "exit");
    if (mode === "crash") child.kill();
    else {
      const closed = ps(`(Get-Process -Id ${child.pid} -ErrorAction SilentlyContinue).CloseMainWindow()`);
      if (closed !== "True") child.kill();
      assert.equal(closed, "True");
    }
    await Promise.race([exited, sleep(8000).then(() => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill();
        throw new Error("Soulseed did not exit");
      }
    })]);
  }
  await browser?.close();
  await new Promise(resolve => mock.close(resolve));
  if (sidecarPid) {
    await eventually(() => ps(`if (Get-Process -Id ${sidecarPid} -ErrorAction SilentlyContinue) {
      'running'
    } else {
      'stopped'
    }
    exit 0`) === "stopped", 8000);
    console.log(`Sidecar exited after ${mode === "crash" ? "forced" : "normal"} host shutdown.`);
  }
}
