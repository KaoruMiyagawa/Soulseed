import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { createServer } from "node:net";
import { setTimeout as sleep } from "node:timers/promises";
import { chromium, expect } from "@playwright/test";

const [exe, screenshot] = process.argv.slice(2);
if (!exe || !screenshot || process.platform !== "win32") throw new Error("exe and screenshot required");
const server = createServer();
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const debugPort = server.address().port;
await new Promise(resolve => server.close(resolve));
const child = spawn(exe, [], {
  env: { ...process.env, Path: `${process.env.SystemRoot}\\System32;${process.env.SystemRoot}`,
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${debugPort}` },
  stdio: "ignore",
});
let browser;
try {
  for (let attempt = 0; attempt < 80; attempt++) {
    try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`); break; }
    catch { await sleep(250); }
  }
  assert(browser, "Desktop WebView did not expose the temporary QA endpoint");
  const page = browser.contexts()[0].pages()[0];
  await expect(page.getByRole("heading", { name: /大肥鱼的.*生活世界/ })).toBeVisible();
  assert.equal(new URL(page.url()).host, "tauri.localhost");
  const call = (path) => page.evaluate(async path => {
    const response = await window.__TAURI__.core.invoke("desktop_request", { path, method: "GET", body: null });
    return { status: response.status, body: JSON.parse(response.body) };
  }, path);
  const health = await call("/health");
  assert.equal(health.status, 200);
  assert.equal(health.body.chat_reply_mode, "real");
  const status = await call("/desktop/runtime");
  assert.equal(status.status, 200);
  assert.equal(status.body.daily_limit, 20);
  assert.equal(status.body.autonomous_paused, true);
  await page.getByRole("button", { name: /模型设置/ }).click();
  await expect(page.getByText("私人内测运行状态")).toBeVisible();
  await expect(page.getByText(/主动联系：已关闭/)).toBeVisible();
  await page.screenshot({ path: screenshot });
  console.log("Installed beta.5 UI, original world page, runtime controls and health passed");
} finally {
  if (child.exitCode === null) {
    const escaped = String(child.pid);
    execFileSync("powershell.exe", ["-NoProfile", "-Command", `(Get-Process -Id ${escaped}).CloseMainWindow()`], { windowsHide: true });
    for (let attempt = 0; attempt < 40 && child.exitCode === null; attempt++) await sleep(200);
  }
  await browser?.close();
}
