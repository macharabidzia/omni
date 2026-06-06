import fs from "node:fs/promises";
import process from "node:process";

import { chromium } from "playwright";

const url = process.argv[2];
const fakeAudioPath = process.argv[3];
const outputPath = process.argv[4];

if (!url || !fakeAudioPath || !outputPath) {
  console.error("usage: node run-live-check.mjs <url> <fake-audio.wav> <output.json>");
  process.exit(2);
}

const origin = new URL(url).origin;
const consoleMessages = [];
const pageErrors = [];

const browser = await chromium.launch({
  headless: true,
  args: [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${fakeAudioPath}`,
    "--autoplay-policy=no-user-gesture-required",
  ],
});

try {
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
  });
  await context.grantPermissions(["microphone"], { origin });

  const page = await context.newPage();
  page.on("console", (message) => {
    consoleMessages.push({
      type: message.type(),
      text: message.text(),
    });
  });
  page.on("pageerror", (error) => {
    pageErrors.push(String(error));
  });

  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForFunction(
    () => document.body.innerText.includes("Ready: ready"),
    undefined,
    { timeout: 60000 },
  );

  await page.getByRole("button", { name: "Start Session" }).click();
  await page.waitForFunction(
    () => document.body.innerText.includes("Session: ready"),
    undefined,
    { timeout: 30000 },
  );
  await page.waitForFunction(
    () => document.body.innerText.includes("Mic Live"),
    undefined,
    { timeout: 10000 },
  );

  let waitError = null;
  try {
    await page.waitForFunction(
      () =>
        document.querySelector(".debug-log")?.textContent?.includes('"type": "local.turn.commit"') &&
        document.querySelector(".debug-log")?.textContent?.includes('"type": "local.playback.started"'),
      undefined,
      { timeout: 60000 },
    );
    await page.waitForTimeout(1500);
  } catch (error) {
    waitError = error instanceof Error ? error.message : String(error);
  }

  const result = await page.evaluate(() => {
    const debugText = document.querySelector(".debug-log")?.textContent ?? "[]";
    let debugEvents = [];
    try {
      debugEvents = JSON.parse(debugText);
    } catch {
      debugEvents = [];
    }

    const metrics = Array.from(document.querySelectorAll(".metric-card")).map((card) => {
      const label = card.querySelector(".metric-label")?.textContent?.trim() ?? "";
      const value = card.querySelector(".metric-value")?.textContent?.trim() ?? "";
      return { label, value };
    });

    return {
      statusLine: document.querySelector(".status-line")?.textContent?.trim() ?? "",
      livePill: document.querySelector(".live-pill")?.textContent?.trim() ?? "",
      transcript: document.querySelectorAll(".transcript-body")[0]?.textContent ?? "",
      assistantText: document.querySelectorAll(".transcript-body")[1]?.textContent ?? "",
      errorText: document.querySelector(".error-banner")?.textContent?.trim() ?? null,
      debugEvents,
      debugEventTypes: debugEvents.map((event) => event.type),
      metrics,
    };
  });

  const output = {
    url,
    fakeAudioPath,
    waitError,
    ...result,
    consoleMessages,
    pageErrors,
  };
  await fs.writeFile(outputPath, JSON.stringify(output, null, 2), "utf-8");

  const hasPlaybackStart = result.debugEventTypes.includes("local.playback.started");
  const hasCommit = result.debugEventTypes.includes("local.turn.commit");
  if (!hasPlaybackStart || !hasCommit || result.errorText || pageErrors.length > 0 || waitError) {
    process.exit(1);
  }
} finally {
  await browser.close();
}
