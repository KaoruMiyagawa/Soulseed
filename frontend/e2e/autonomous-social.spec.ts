import { expect, test } from "@playwright/test";

const api = process.env.CHAT_C3_API_URL!;
const pairingSecret = process.env.CHAT_C3_PAIRING_SECRET!;
const conversationId = process.env.SOULSEED_QA_CONVERSATION_ID!;

test("role state reaches the shared contact flow and the user replies in the same browser chat", async ({ page }) => {
  test.setTimeout(120_000);
  const paired = await page.request.post(`${api}/auth/session`, { data: { secret: pairingSecret } });
  expect(paired.ok()).toBe(true);

  await page.goto("/#chat");
  const conversation = page.getByRole("navigation", { name: "会话列表" }).getByRole("button", {
    name: "自主社交浏览器验收",
  });
  await expect(conversation).toBeVisible();
  await conversation.click();

  const proactive = page.getByText("刚才看着今天的天空，我突然想给你送一颗快乐泡泡。你今天过得怎么样？");
  await expect(proactive).toBeVisible({ timeout: 60_000 });
  const messageRow = page.locator("article.message.assistant").filter({ hasText: "快乐泡泡" });
  await expect(messageRow).toHaveCount(1);

  const syncMode = page.getByRole("button", { name: "使用同步聊天" });
  if (await syncMode.isVisible()) await syncMode.click();
  await page.getByLabel("写给大肥鱼的消息").fill("我今天过得不错，你呢？");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("我今天过得很开心，谢谢你来找我聊！")).toBeVisible({ timeout: 45_000 });
  await expect(page.locator("article.message.user").filter({ hasText: "我今天过得不错，你呢？" })).toHaveCount(1);

  await page.screenshot({ path: `${process.env.CHAT_C3_ARTIFACTS}/browser-autonomous-social.png` });
  await page.reload();
  await expect(page.locator("article.message.assistant").filter({ hasText: "快乐泡泡" })).toHaveCount(1);
  await expect(page.locator("article.message.user").filter({ hasText: "我今天过得不错，你呢？" })).toHaveCount(1);
  expect(conversationId).toBeTruthy();
});
