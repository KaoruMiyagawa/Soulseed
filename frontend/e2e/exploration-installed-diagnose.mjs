import { fileURLToPath } from "node:url";
import { spawn, execFileSync } from "node:child_process";
import { createServer } from "node:net";
import { setTimeout as sleep } from "node:timers/promises";
import { chromium } from "@playwright/test";

const server = createServer();
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;
await new Promise(resolve => server.close(resolve));
const child = spawn(process.argv[2], [], { env: {
  ...process.env, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port}`,
}, stdio: "ignore" });
let browser;
try {
  for (let i = 0; i < 100; i++) {
    try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`); break; }
    catch { await sleep(200); }
  }
  if (!browser) throw Error("WebView debugging unavailable");
  const page = browser.contexts()[0].pages()[0];
  page.on("pageerror", error => console.log("PAGE_ERROR", error.stack));
  await page.getByRole("heading", { name: /大肥鱼的.*生活世界/ }).waitFor();
  const api = await page.evaluate(async () => {
    const result = await window.__TAURI__.core.invoke("desktop_request", {
      path: "/characters/big_fish/exploration-autonomy", method: "GET", body: null,
    });
    const body = JSON.parse(result.body);
    return { status: result.status, quota: body.quota, hasCurrent: !!body.current, hasLatest: !!body.latest };
  });
  console.log("EXPLORATION_API", JSON.stringify(api));
  await page.getByRole("button", { name: "探索", exact: true }).click();
  await sleep(1500);
  console.log("EXPLORATION_HEADING", await page.getByRole("heading", { name: "探索与发现" }).isVisible());
  await page.screenshot({ path: process.argv[3] });
} finally {
  if (child.exitCode === null) {
    execFileSync("powershell.exe", ["-NoProfile", "-File", fileURLToPath(new URL("./contact-window.ps1", import.meta.url)),
      "-ProcessId", String(child.pid), "-Command", "Close"], { windowsHide: true });
  }
  await browser?.close().catch(() => {});
}
