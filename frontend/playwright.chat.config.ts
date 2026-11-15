import { defineConfig } from "@playwright/test";
import path from "node:path";

if (!process.env.CHAT_C3_WEB_URL || !process.env.CHAT_C3_ARTIFACTS) {
  throw new Error("请使用 python backend/scripts/preview_chat_c3.py --test 启动隔离测试");
}

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  timeout: 45000,
  outputDir: path.join(process.env.CHAT_C3_ARTIFACTS, "playwright"),
  use: {
    baseURL: process.env.CHAT_C3_WEB_URL,
    channel: process.env.CHAT_C3_BROWSER_CHANNEL || "msedge",
    viewport: { width: 1440, height: 940 },
    screenshot: "only-on-failure",
  },
});
