import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { createServer } from "node:net";
import { setTimeout as sleep } from "node:timers/promises";
import { chromium, expect } from "@playwright/test";
const [exe, screenshot] = process.argv.slice(2);
const server = createServer();
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;
await new Promise(resolve => server.close(resolve));
const child = spawn(exe, [], { env: { ...process.env, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port}` }, stdio: "ignore" });
let browser;
try {
  for (let i = 0; i < 100; i++) {
    try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`); break; } catch { await sleep(200); }
  }
  assert(browser);
  const page = browser.contexts()[0].pages()[0];
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await expect(page.getByRole("heading", { name: /大肥鱼的.*生活世界/ })).toBeVisible();
  const response = await page.evaluate(async () => {
    const r = await window.__TAURI__.core.invoke("desktop_request", { path: "/characters/big_fish/exploration-autonomy", method: "GET", body: null });
    const body = JSON.parse(r.body);
    return { status: r.status, quota: body.quota };
  });
  assert.equal(response.status, 200);
  assert(Number.isFinite(Date.parse(response.quota.resets_at)));
  for (let i = 0; i < 3; i++) {
    await page.getByRole("button", { name: "探索", exact: true }).click();
    await expect(page.getByRole("heading", { name: "探索与发现" })).toBeVisible();
    await expect(page.getByText(/下次重置/)).toBeVisible();
    await page.getByRole("button", { name: "对话", exact: true }).click();
    await expect(page.getByRole("heading", { name: "会话" })).toBeVisible();
  }
  for (const [button, heading] of [["手帐", "生活手帐"], ["计划", "今日计划"], ["她的世界", /大肥鱼的.*生活世界/]]) {
    await page.getByRole("button", { name: button, exact: true }).click();
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
  }
  await page.getByRole("button", { name: "探索", exact: true }).click();
  await page.screenshot({ path: screenshot });
  execFileSync("powershell.exe", ["-NoProfile", "-Command", `
    $sidecar = Get-CimInstance Win32_Process -Filter "ParentProcessId=${child.pid} AND Name='soulseed-backend.exe'"
    if (-not $sidecar) { throw 'QA sidecar missing' }
    Stop-Process -Id $sidecar.ProcessId
  `], { windowsHide: true });
  await page.getByRole("button", { name: "对话", exact: true }).click();
  await page.getByRole("button", { name: "探索", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("探索状态读取失败");
  await expect(page.getByRole("heading", { name: "探索与发现" })).toBeVisible();
  assert.deepEqual(errors, []);
  console.log("Installed candidate: exploration, paused state, navigation and reset timestamp passed");
} finally {
  if (child.exitCode === null) execFileSync("powershell.exe", ["-NoProfile", "-File", fileURLToPath(new URL("./contact-window.ps1", import.meta.url)), "-ProcessId", String(child.pid), "-Command", "Close"], { windowsHide: true });
  await browser?.close().catch(() => {});
}
