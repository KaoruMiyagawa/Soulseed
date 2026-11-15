import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const api = process.env.CHAT_C3_API_URL!;
const artifacts = process.env.CHAT_C3_ARTIFACTS!;
const submitPath = /\/api\/conversations\/[^/]+\/user-messages$/;
type Submission = { client_message_id: string; client_stream_id: string; client_sequence: number; content: string };

async function openChat(page: Page, mode = "隔离测试 · Mock 回复") {
  const pairing = await page.request.post(`${api}/auth/session`, {
    data: { secret: process.env.CHAT_C3_PAIRING_SECRET },
  });
  expect(pairing.ok()).toBe(true);
  await page.goto("/#chat");
  const created = page.waitForResponse(response =>
    response.request().method() === "POST" && response.url() === `${api}/conversations`,
  );
  await page.locator(".new-chat").click();
  const { id } = await (await created).json();
  await expect.poll(() => page.evaluate(conversation => Object.keys(sessionStorage).some(key =>
    key.startsWith("soulseed-active-chat:") && sessionStorage.getItem(key) === conversation,
  ), id)).toBe(true);
  await expect(page.getByText(mode)).toBeVisible();
}

function input(page: Page) { return page.getByRole("textbox", { name: "写给大肥鱼的消息" }); }
function users(page: Page) { return page.locator(".messages article.message.user"); }
function replies(page: Page) { return page.locator(".messages article.message.assistant"); }
async function send(page: Page, content: string) {
  await input(page).fill(content);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(input(page)).toHaveValue("");
}

test("five optimistic sends, continuous flight, strict commits, independent replies and reload", async ({ page }) => {
  await openChat(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const posted: Submission[] = [];
  const legacy: string[] = [];
  page.on("request", request => {
    if (request.method() === "POST" && /\/api\/conversations\/[^/]+\/messages$/.test(request.url())) legacy.push(request.url());
  });
  await page.route(submitPath, async route => {
    if (route.request().method() !== "POST") return route.continue();
    const data = route.request().postDataJSON() as Submission;
    posted.push(data);
    if (data.client_sequence === 1) await held;
    await route.continue();
  });
  await page.evaluate(() => {
    const observer = new MutationObserver(() => {
      const overlay = document.querySelector(".chat-flight");
      if (!overlay) return;
      for (const animation of [...overlay.getAnimations(), ...document.querySelector(".message.user .chat-bubble")!.getAnimations()]) {
        animation.pause();
        animation.currentTime = 90;
      }
      observer.disconnect();
    });
    observer.observe(document.body, { childList: true });
  });
  const contents = ["今天的海面很安静。", "窗边有一点风。", "我听见远处的潮声。", "把这几句留在这里。", "晚些再来聊。"];
  try {
    await send(page, contents[0]);
    const row = users(page).first();
    await expect(row.getByText("发送中 · 未确认保存")).toBeVisible();
    await expect(input(page)).toBeFocused();
    await expect(replies(page)).toHaveCount(0);
    await expect.poll(() => posted.length).toBe(1);
    const original = await row.elementHandle();
    const flight = page.locator(".chat-flight");
    await expect(flight).toHaveCount(1);
    const positions = await flight.evaluate(overlay => {
      const motion = overlay.getAnimations()[0];
      motion.currentTime = 0;
      const start = overlay.getBoundingClientRect();
      motion.currentTime = 145;
      const middle = overlay.getBoundingClientRect();
      motion.currentTime = 285;
      const end = overlay.getBoundingClientRect();
      motion.currentTime = 90;
      return { duration: motion.effect!.getTiming().duration, start: start.top, middle: middle.top, end: end.top };
    });
    expect(positions.duration).toBe(290);
    expect(positions.start).toBeGreaterThan(positions.middle);
    expect(positions.middle).toBeGreaterThan(positions.end);
    await page.screenshot({ path: path.join(artifacts, "send-motion.png"), caret: "hide" });
    await flight.evaluate(overlay => overlay.getAnimations().forEach(animation => animation.finish()));
    await expect(flight).toHaveCount(0);
    for (const content of contents.slice(1)) await send(page, content);
    await expect(users(page)).toHaveCount(5);
    await expect(users(page).nth(1).getByText("排队中 · 等待前一条确认")).toBeVisible();
    expect(posted.map(item => item.client_sequence)).toEqual([1]);
    expect(legacy).toEqual([]);
    release();
    await expect.poll(() => posted.map(item => item.client_sequence)).toEqual([1, 2, 3, 4, 5]);
    await expect(users(page).first()).toHaveAttribute("data-message-id", /.+/);
    expect(await row.evaluate((node, before) => node.isSameNode(before), original)).toBe(true);
    await expect(replies(page)).toHaveCount(5, { timeout: 20000 });
    expect(await users(page).locator(".chat-bubble").allTextContents()).toEqual(contents);
    const userIds = await users(page).evaluateAll(rows => rows.map(row => row.getAttribute("data-message-id")));
    expect(await replies(page).evaluateAll(rows => rows.map(row => row.getAttribute("data-reply-to")))).toEqual(userIds);
    await expect(page.getByText("回复完成", { exact: true })).toHaveCount(5);
    await page.reload();
    await expect(users(page)).toHaveCount(5);
    await expect(replies(page)).toHaveCount(5);
    await expect(page.getByText("回复完成", { exact: true })).toHaveCount(5);
    expect(await users(page).locator(".chat-bubble").allTextContents()).toEqual(contents);
    expect(legacy).toEqual([]);
    await page.getByRole("region", { name: "聊天记录" }).evaluate(list => {
      const firstReply = list.querySelector(".message.assistant")!;
      list.scrollTop += firstReply.getBoundingClientRect().top - list.getBoundingClientRect().top - list.clientHeight * 0.48;
    });
    await page.screenshot({ path: path.join(artifacts, "desktop-chat.png"), caret: "hide" });
  } finally {
    release();
  }
});

test("lost confirmation is looked up with the original client message id", async ({ page }) => {
  await openChat(page);
  const posted: Submission[] = [];
  const queries: string[] = [];
  page.on("request", request => {
    if (request.method() === "GET" && /\/user-messages\/[^/]+$/.test(request.url())) queries.push(request.url());
  });
  await page.route(submitPath, async route => {
    posted.push(route.request().postDataJSON());
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    await route.abort("connectionreset");
  });
  await send(page, "这条确认会在网络里丢失。");
  await expect(users(page).first()).toHaveAttribute("data-message-id", /.+/);
  await expect(replies(page)).toHaveCount(1);
  expect(posted).toHaveLength(1);
  expect(queries.some(url => url.endsWith(posted[0].client_message_id))).toBe(true);
  await expect(users(page)).toHaveCount(1);
});

test("status still reconciles when health reports the worker is disabled", async ({ page }) => {
  // Exercise a paused-worker banner with a final reply arriving from the real test API.
  let paused = false;
  await page.route("**/api/health", async route => {
    if (!paused) return route.continue();
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), chat_reply_mode: "disabled" } });
  });
  await page.route(/\/api\/conversations\/[^/]+\/messages\?/, async route => {
    if (!paused) return route.continue();
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), async_chat_accepting: false } });
  });
  await openChat(page);
  await send(page, "停机前接纳的一条消息。");
  await expect(users(page)).toHaveCount(1);
  await expect(users(page).first()).toHaveAttribute("data-message-id", /.+/);
  paused = true;
  await expect(page.getByText("异步回复当前不可用。")).toBeVisible();
  await input(page).fill("关闭期间保留这条草稿");
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
  await expect(replies(page)).toHaveCount(1, { timeout: 12000 });
  await expect(users(page).first().getByText("回复完成")).toBeVisible({ timeout: 12000 });
});

test("failed send survives reload and safe retry keeps id, stream and sequence", async ({ page }) => {
  await openChat(page);
  const posted: Submission[] = [];
  let allow = false;
  await page.route(submitPath, async route => {
    posted.push(route.request().postDataJSON());
    if (!allow) return route.fulfill({ status: 503, json: { detail: "测试连接故障" } });
    await route.continue();
  });
  await send(page, "一条需要安全重试的消息。");
  await expect(users(page).first().getByText("发送失败 · 未确认保存")).toBeVisible();
  const failedId = posted[0].client_message_id;
  await page.reload();
  await expect(users(page)).toHaveCount(1);
  await expect(users(page).first().getByText("发送失败 · 未确认保存")).toBeVisible();
  allow = true;
  await page.getByRole("button", { name: "重试发送" }).click();
  await expect(replies(page)).toHaveCount(1);
  expect(posted.length).toBeGreaterThan(1);
  expect(posted.every(item => item.client_message_id === failedId && item.client_sequence === 1
    && item.client_stream_id === posted[0].client_stream_id)).toBe(true);
  await expect(users(page)).toHaveCount(1);
});

test("offline compose and reduced motion with IME and long messages on mobile", async ({ page, context }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 430, height: 900 });
  await openChat(page);
  const posted: Submission[] = [];
  page.on("request", request => {
    if (request.method() === "POST" && submitPath.test(request.url())) posted.push(request.postDataJSON());
  });
  await context.setOffline(true);
  const long = "潮水沿着窗边慢慢退去。".repeat(120);
  await input(page).fill(long);
  await input(page).dispatchEvent("compositionstart");
  await input(page).dispatchEvent("keydown", { key: "Enter", ctrlKey: true, isComposing: true });
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(users(page)).toHaveCount(0);
  await expect(input(page)).toHaveValue(long);
  await input(page).dispatchEvent("compositionend");
  await page.getByRole("button", { name: "发送", exact: true }).evaluate(button => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(users(page)).toHaveCount(1);
  await expect(users(page).first().getByText("等待联网 · 未确认保存")).toBeVisible();
  await expect(page.locator(".chat-flight")).toHaveCount(0);
  expect(posted).toEqual([]);
  const scroll = page.getByRole("region", { name: "聊天记录" });
  await expect.poll(() => scroll.evaluate(list => list.scrollTop)).toBeGreaterThan(100);
  await scroll.evaluate(list => { list.scrollTop = 0; });
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await context.setOffline(false);
  await expect(replies(page)).toHaveCount(1);
  expect(posted).toHaveLength(1);
  expect(posted[0].content).toBe(long);
  expect(await replies(page).locator(".chat-bubble").evaluate(bubble => getComputedStyle(bubble).animationName)).toBe("none");
  expect(await scroll.evaluate(list => list.scrollTop)).toBeLessThan(3);
  expect(await scroll.evaluate(list => list.scrollHeight > list.clientHeight && list.scrollWidth <= list.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await scroll.evaluate(list => { list.scrollTop = list.scrollHeight; });
  await page.screenshot({ path: path.join(artifacts, "mobile-chat.png"), caret: "hide" });
});

test("two tabs serialize one persisted client stream and stop reads after leaving chat", async ({ page, context }) => {
  await openChat(page);
  const other = await context.newPage();
  await other.goto("/#chat");
  await expect(other.getByText("隔离测试 · Mock 回复")).toBeVisible();
  const posted: Submission[] = [];
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await context.route(submitPath, async route => {
    const body = route.request().postDataJSON() as Submission;
    posted.push(body);
    if (body.client_sequence === 1) await held;
    await route.continue();
  });
  try {
    await send(page, "第一个标签页发出。");
    await expect.poll(() => posted.length).toBe(1);
    await send(other, "第二个标签页排队。");
    await expect(other.getByText("排队中 · 等待前一条确认")).toBeVisible();
    expect(posted).toHaveLength(1);
    release();
    await expect(replies(other)).toHaveCount(2, { timeout: 12000 });
    expect(posted.map(item => item.client_sequence)).toEqual([1, 2]);
    expect(new Set(posted.map(item => item.client_stream_id)).size).toBe(1);
    const reads: string[] = [];
    page.on("request", request => {
      if (request.method() === "GET" && /\/api\/conversations\/[^/]+\/(messages\?|user-messages\/)/.test(request.url())) {
        reads.push(request.url());
      }
    });
    await page.getByRole("button", { name: "她的世界", exact: true }).click();
    await expect(input(page)).toHaveCount(0);
    const count = reads.length;
    await new Promise(resolve => setTimeout(resolve, 2750));
    expect(reads).toHaveLength(count);
  } finally {
    release();
    await other.close();
  }
});
