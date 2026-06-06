import fs from "node:fs/promises";
import process from "node:process";

import { chromium } from "playwright";

const url = process.argv[2];
const fakeAudioPath = process.argv[3];
const outputJsonPath = process.argv[4];
const outputAudioPath = process.argv[5];

if (!url || !fakeAudioPath || !outputJsonPath || !outputAudioPath) {
  console.error(
    "usage: node playwright-livekit-capture.mjs <url> <fake-audio.wav> <output.json> <output.webm|output.wav>",
  );
  process.exit(2);
}

const captureMode = outputAudioPath.toLowerCase().endsWith(".wav") ? "wav" : "webm";

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
  await page.waitForFunction(
    () => {
      const captureStream = window.__assistantCaptureStream;
      if (captureStream instanceof MediaStream && captureStream.getAudioTracks().length > 0) {
        return true;
      }
      return document.querySelector("audio") instanceof HTMLAudioElement;
    },
    undefined,
    { timeout: 15000 },
  );

  await page.evaluate(async (requestedCaptureMode) => {
    const captureStream = window.__assistantCaptureStream;
    const audioElement = document.querySelector("audio");
    const stream =
      (captureStream instanceof MediaStream ? captureStream : null) ??
      (audioElement instanceof HTMLAudioElement
        ? (
            audioElement.captureStream?.() ??
            audioElement.mozCaptureStream?.() ??
            audioElement.srcObject
          )
        : null);
    if (!(stream instanceof MediaStream) || stream.getAudioTracks().length === 0) {
      throw new Error("Assistant capture stream was not available.");
    }

    const captureMode = requestedCaptureMode;

    if (captureMode === "wav") {
      const audioContext = new AudioContext({
        latencyHint: "interactive",
        sampleRate: 48000,
      });
      await audioContext.resume();
      const source = audioContext.createMediaStreamSource(stream);
      const processor = audioContext.createScriptProcessor(4096, 1, 1);
      const mute = audioContext.createGain();
      mute.gain.value = 0;
      const capturedChunks = [];

      processor.onaudioprocess = (event) => {
        const channelData = event.inputBuffer.getChannelData(0);
        capturedChunks.push(new Float32Array(channelData));
      };

      source.connect(processor);
      processor.connect(mute);
      mute.connect(audioContext.destination);

      const stopped = Promise.resolve().then(async () => {
        return {
          stop: async () => {
            processor.disconnect();
            source.disconnect();
            mute.disconnect();
            processor.onaudioprocess = null;
            const sampleRate = audioContext.sampleRate;
            await audioContext.close();

            const totalSamples = capturedChunks.reduce((sum, chunk) => sum + chunk.length, 0);
            const pcmBytes = new Uint8Array(totalSamples * 2);
            let writeOffset = 0;
            for (const chunk of capturedChunks) {
              for (let index = 0; index < chunk.length; index += 1) {
                const sample = Math.max(-1, Math.min(1, chunk[index] ?? 0));
                const value = sample < 0 ? Math.round(sample * 0x8000) : Math.round(sample * 0x7fff);
                pcmBytes[writeOffset] = value & 0xff;
                pcmBytes[writeOffset + 1] = (value >> 8) & 0xff;
                writeOffset += 2;
              }
            }

            const wavBytes = new Uint8Array(44 + pcmBytes.length);
            const view = new DataView(wavBytes.buffer);
            const writeAscii = (offset, text) => {
              for (let index = 0; index < text.length; index += 1) {
                wavBytes[offset + index] = text.charCodeAt(index);
              }
            };
            writeAscii(0, "RIFF");
            view.setUint32(4, 36 + pcmBytes.length, true);
            writeAscii(8, "WAVE");
            writeAscii(12, "fmt ");
            view.setUint32(16, 16, true);
            view.setUint16(20, 1, true);
            view.setUint16(22, 1, true);
            view.setUint32(24, sampleRate, true);
            view.setUint32(28, sampleRate * 2, true);
            view.setUint16(32, 2, true);
            view.setUint16(34, 16, true);
            writeAscii(36, "data");
            view.setUint32(40, pcmBytes.length, true);
            wavBytes.set(pcmBytes, 44);

            let binary = "";
            const chunkSize = 0x8000;
            for (let offset = 0; offset < wavBytes.length; offset += chunkSize) {
              binary += String.fromCharCode(...wavBytes.subarray(offset, offset + chunkSize));
            }

            return {
              base64: btoa(binary),
              mimeType: "audio/wav",
              size: wavBytes.length,
              sampleRate,
            };
          },
        };
      });

      window.__livekitCapture = {
        mode: "wav",
        stopped,
      };
      return;
    }

    const recorder = new MediaRecorder(stream, {
      mimeType: "audio/webm;codecs=opus",
    });
    const chunks = [];
    const stopped = new Promise((resolve) => {
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunks.push(event.data);
        }
      };
      recorder.onstop = async () => {
        const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
        const bytes = new Uint8Array(await blob.arrayBuffer());
        let binary = "";
        const chunkSize = 0x8000;
        for (let offset = 0; offset < bytes.length; offset += chunkSize) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
        }
        resolve({
          base64: btoa(binary),
          mimeType: recorder.mimeType || "audio/webm",
          size: bytes.length,
        });
      };
    });

    window.__livekitCapture = {
      mode: "webm",
      recorder,
      stopped,
    };
    recorder.start(50);
  }, captureMode);

  let waitError = null;
  try {
    await page.waitForFunction(
      () => {
        const debugText = document.querySelector(".debug-log")?.textContent ?? "";
        return (
          debugText.includes('"type": "local.turn.commit"') &&
          debugText.includes('"type": "local.playback.started"') &&
          debugText.includes('"type": "assistant.done"') &&
          debugText.includes('"type": "local.playback.drain"')
        );
      },
      undefined,
      { timeout: 30000 },
    );
  } catch (error) {
    waitError = error instanceof Error ? error.message : String(error);
  }

  const capture = await page.evaluate(async () => {
    const state = window.__livekitCapture;
    if (!state) {
      throw new Error("Capture state was not initialized.");
    }
    if (state.mode === "wav") {
      const controller = await state.stopped;
      return await controller.stop();
    }
    if (state.recorder.state !== "inactive") {
      state.recorder.stop();
    }
    return await state.stopped;
  });

  try {
    await page.getByRole("button", { name: "Stop Session" }).click();
    await page.waitForTimeout(500);
  } catch {
    // Best effort cleanup only.
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
      errorText: document.querySelector(".error-banner")?.textContent?.trim() ?? null,
      debugEvents,
      debugEventTypes: debugEvents.map((event) => event.type),
      metrics,
    };
  });

  await fs.writeFile(outputJsonPath, JSON.stringify({
    url,
    fakeAudioPath,
    waitError,
    captureSize: capture.size,
    captureMimeType: capture.mimeType,
    ...result,
    consoleMessages,
    pageErrors,
  }, null, 2), "utf-8");
  await fs.writeFile(outputAudioPath, Buffer.from(capture.base64, "base64"));

  const hasPlaybackStart = result.debugEventTypes.includes("local.playback.started");
  const hasCommit = result.debugEventTypes.includes("local.turn.commit");
  const hasDone = result.debugEventTypes.includes("assistant.done");
  if (!hasPlaybackStart || !hasCommit || !hasDone || result.errorText || pageErrors.length > 0 || waitError) {
    process.exit(1);
  }
} finally {
  await browser.close();
}
