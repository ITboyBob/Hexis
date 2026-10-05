import { expect, test } from "@playwright/test";
import { startVoiceServices, type VoiceServiceHandle } from "./fixtures/voice";

let services: VoiceServiceHandle | null = null;

test.beforeAll(async () => {
  services = await startVoiceServices();
});

test.afterAll(async () => {
  await services?.close();
  services = null;
});

test("Talk requests microphone, closes cleanly, and can reopen", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  await page.context().grantPermissions(["microphone"]);
  await page.goto("/chat");
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();

  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Stop Talk mode" })).toBeVisible();
  await expect(page.getByText("Listening")).toBeVisible();
  await expect(page.getByText("The microphone stops while Hexis thinks and speaks.")).toHaveCount(0);

  await page.getByRole("button", { name: "Stop Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();

  await page.getByRole("button", { name: "Start Talk mode" }).click();
  await expect(page.getByText("Listening")).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop Talk mode" })).toBeVisible();

  await page.screenshot({
    path: testInfo.outputPath("talk-entry.png"),
    fullPage: true,
  });

  await page.getByRole("button", { name: "Stop Talk mode" }).click();
  await expect(page.getByRole("button", { name: "Start Talk mode" })).toBeVisible();
});
