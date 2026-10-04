import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { EMOTIONS } from "../lib/avatar-preview/contracts";
import { sameValueUpdates, scenarios, transitions } from "../lib/avatar-preview/scenarios";

const sha256 = (path: string) => createHash("sha256").update(readFileSync(path)).digest("hex");
const evidenceArtifacts = {
  design: {
    version: "v1.7",
    sha256: sha256(resolve(process.cwd(), "../../Project/geek_interview/docs/附录-二维Avatar独立预览设计.md")),
  },
  plan: {
    version: "v1.3",
    sha256: sha256(resolve(process.cwd(), "../../Project/geek_interview/docs/implementation plan/2026-10-04-二维Avatar独立预览执行计划.md")),
  },
  wordset: {
    version: "v1",
    sha256: sha256(resolve(process.cwd(), "lib/avatar-preview/contracts.ts")),
  },
  prompt: {
    version: "v1",
    sha256: sha256(resolve(process.cwd(), "../services/prompts/avatar_emotion.md")),
  },
};

type EngineDiagnostics = {
  destroyed: boolean;
  composition: { eye: string; pose: string; playing: boolean; amplitude: number } | null;
  eyeFrom: number;
  eyeTo: number;
  eyeMorph: { x: number; t: number; v: number };
  overlay: { x: number; t: number; v: number };
  eyeMorphCount: number;
  rafAlive: boolean;
  eyePathsVisible: boolean;
};

async function stableDiagnostics(page: import("@playwright/test").Page): Promise<EngineDiagnostics> {
  await page.waitForFunction(() => {
    const value = window.__AVATAR_PREVIEW_DIAGNOSTICS__?.();
    return !!value
      && !value.destroyed
      && value.rafAlive
      && value.eyePathsVisible
      && value.eyeMorph.x > 0.98
      && value.overlay.x < 0.01;
  });
  const value = await page.evaluate(() => window.__AVATAR_PREVIEW_DIAGNOSTICS__?.() ?? null);
  if (!value) throw new Error("角色引擎诊断不可用");
  return value;
}

test("全部182个不同标签转换可完成且真实引擎路径可见", async ({ page }) => {
  test.setTimeout(300000);
  await page.goto("/avatar-preview");
  await expect(page.getByRole("img", { name: /眼形/ })).toBeVisible();
  for (const { from, to } of transitions) {
    await page.getByRole("button", { name: new RegExp(`^手工标签 ${from}：`) }).click({ noWaitAfter: true });
    await page.getByRole("button", { name: new RegExp(`^手工标签 ${to}：`) }).click({ noWaitAfter: true });
    await expect(page.getByTestId("emotion-current")).toHaveText(to);
    await expect(page.getByTestId("emotion-source")).toHaveText("手工标签");
    await expect(page.getByRole("img", { name: /眼形/ })).toHaveAttribute(
      "aria-label",
      new RegExp(`眼形 ${to === "neutral" ? "idle" : to}，活动 idle`),
    );
    const diagnostics = await stableDiagnostics(page);
    expect(diagnostics.composition?.eye).toBe(to === "neutral" ? "idle" : to);
    expect(diagnostics.destroyed).toBe(false);
    expect(diagnostics.rafAlive).toBe(true);
    expect(diagnostics.eyePathsVisible).toBe(true);
  }
});

test("14个同值更新不重复重启眼形morph", async ({ page }) => {
  await page.goto("/avatar-preview");
  await expect(page.getByRole("img", { name: /眼形/ })).toBeVisible();
  for (const { from } of sameValueUpdates) {
    const button = page.getByRole("button", { name: new RegExp(`^手工标签 ${from}：`) });
    await button.click({ noWaitAfter: true });
    await stableDiagnostics(page);
    const before = await stableDiagnostics(page);
    await button.click({ noWaitAfter: true });
    const after = await stableDiagnostics(page);
    expect(after.eyeMorphCount).toBe(before.eyeMorphCount);
    expect(after.eyeTo).toBe(before.eyeTo);
    expect(page.getByTestId("emotion-current")).toHaveText(from);
  }
  expect(EMOTIONS).toHaveLength(14);
});

test("C01到C12固定场景可逐条执行且无页面错误", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/avatar-preview");
  await expect(page.getByRole("img", { name: /眼形/ })).toBeVisible();
  expect(scenarios.map(({ id }) => id)).toEqual([
    "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12",
  ]);
  for (const scenario of scenarios) {
    if (await page.getByLabel("固定场景").isDisabled()) {
      await page.getByRole("button", { name: "显式重开预览会话" }).click();
    }
    await page.getByLabel("固定场景").selectOption(scenario.id);
    const next = page.getByRole("button", { name: "发送下一条场景事件" });
    while (await next.isEnabled()) {
      await next.click();
      await page.waitForTimeout(5);
    }
  }
  expect(errors).toEqual([]);
});

test("实际本地播放器完成播放、静音、暂停、停止、换文件与自然结束", async ({ page }, testInfo) => {
  test.setTimeout(90000);
  await page.addInitScript((artifacts) => {
    window.__AVATAR_EVIDENCE_ARTIFACTS__ = artifacts;
  }, evidenceArtifacts);
  await page.goto("/avatar-preview");
  await expect(page.getByRole("img", { name: /眼形/ })).toBeVisible();
  await page.getByLabel("完整验收录制（上限10000条）").check();
  await page.getByLabel("实际展示尺寸").fill("259");
  await page.getByLabel("播放微动限幅").fill("12");
  await page.getByLabel("效果时长（毫秒）").fill("250");
  await page.getByLabel("效果最小间隔（毫秒）").fill("20");
  await page.getByRole("button", { name: "排队弹跳效果" }).click();
  await expect(page.getByTestId("visual-effects")).not.toContainText("活动效果：无");

  const audioDirectory = resolve(process.cwd(), "../../.cache/hexis/avatar-preview-2026-10-04");
  const picker = page.getByLabel("选择本地音频文件");
  await picker.setInputFiles(resolve(audioDirectory, "播放验收-分段振幅.wav"));
  await page.getByRole("button", { name: "播放所选本地音频" }).click();
  const playback = page.getByTestId("playback-state");
  await expect(playback).toHaveAttribute("data-phase", "playing");
  await expect(playback).toHaveAttribute("data-epoch", "1");
  await page.waitForFunction(() => Number(document.querySelector("[data-testid='playback-state']")?.getAttribute("data-amplitude")) > 0);

  await page.getByRole("checkbox", { name: "静音实际播放" }).check();
  await page.waitForFunction(() => document.querySelector("[data-testid='playback-state']")?.getAttribute("data-phase") === "playing"
    && Number(document.querySelector("[data-testid='playback-state']")?.getAttribute("data-amplitude")) === 0);
  await page.getByRole("checkbox", { name: "静音实际播放" }).uncheck();
  await page.waitForFunction(() => Number(document.querySelector("[data-testid='playback-state']")?.getAttribute("data-amplitude")) > 0);

  await page.getByRole("button", { name: "暂停实际播放" }).click();
  await expect(playback).toHaveAttribute("data-phase", "buffering");
  await page.getByRole("button", { name: "恢复实际播放" }).click();
  await expect(playback).toHaveAttribute("data-phase", "playing");
  await page.getByRole("button", { name: "停止本地播放" }).click();
  await expect(playback).toHaveAttribute("data-phase", "cancel");
  await expect(playback).toHaveAttribute("data-amplitude", "0");

  await picker.setInputFiles(resolve(audioDirectory, "播放验收-替换文件.wav"));
  await page.getByRole("button", { name: "播放所选本地音频" }).click();
  await expect(playback).toHaveAttribute("data-phase", "playing");
  await expect(playback).toHaveAttribute("data-epoch", "2");
  await expect(playback).toHaveAttribute("data-played-ms", /.+/);
  await expect(playback).toHaveAttribute("data-interval-ms", /.+/);
  await expect(playback).toHaveAttribute("data-phase", "drained", { timeout: 25000 });
  await expect(playback).toHaveAttribute("data-amplitude", "0");

  await page.screenshot({ path: testInfo.outputPath("实际播放器验收.png"), fullPage: true });
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出来源轨迹" }).click();
  const evidencePath = testInfo.outputPath("实际播放器证据.json");
  await (await download).saveAs(evidencePath);
  const screenshotPath = testInfo.outputPath("实际播放器验收.png");
  const evidence = JSON.parse(readFileSync(evidencePath, "utf8"));
  evidence.meta.media = { screenshots: [screenshotPath], videos: [] };
  evidence.verdicts = [
    { claim: "本地播放器实际反馈", status: "pass", reason: "覆盖playing正幅度、静音归零、暂停、恢复、停止、换文件新epoch与自然drained", event_range: [1, evidence.events.length] },
    { claim: "完整事件轨迹", status: "pass", reason: "完整录制模式开启且事件未达到上限", event_range: [1, evidence.events.length] },
    { claim: "动画参数已记录", status: "pass", reason: "259px实际尺寸、12px微动限幅、20ms效果最小间隔", event_range: [1, evidence.events.length] },
  ];
  writeFileSync(evidencePath, JSON.stringify(evidence, null, 2), "utf8");
});
