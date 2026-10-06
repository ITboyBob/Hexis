import { expect, test } from "@playwright/test";

test("Talk shows the idle avatar during a pending microphone request and hides it immediately on close", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: () => new Promise<MediaStream>(() => undefined) },
    });
  });
  await page.goto("/chat");
  const start = page.getByRole("button", { name: "Start Talk mode" });
  await expect(start).toBeVisible();
  await start.click();

  const avatar = page.getByTestId("voice-avatar");
  const stop = page.getByRole("button", { name: "Stop Talk mode" });
  await expect(avatar).toBeVisible();
  await expect(page.getByRole("img", { name: "眼形 idle，活动 idle" })).toBeVisible();
  await expect(page.getByText("Listening", { exact: true })).toHaveCount(0);
  const avatarBox = await avatar.boundingBox();
  const stopBox = await stop.boundingBox();
  expect(avatarBox).not.toBeNull();
  expect(stopBox).not.toBeNull();
  expect(avatarBox!.y + avatarBox!.height).toBeLessThan(stopBox!.y);
  await page.screenshot({ path: testInfo.outputPath("voice-avatar-pending.png") });

  await stop.click();
  await expect(avatar).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("voice-avatar-closed.png") });
});
