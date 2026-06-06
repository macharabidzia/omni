import { useEffect, useMemo, useRef, useState } from "react";

import { CallControls } from "./components/CallControls";
import { MetricsHud } from "./components/MetricsHud";
import { SpeakerSelector } from "./components/SpeakerSelector";
import { TranscriptPanel } from "./components/TranscriptPanel";
import type { RealtimeClient } from "./realtime/client";
import type { GatewayInboundEvent, GatewayMetrics, Speaker } from "./realtime/events";

type DebugEvent = GatewayInboundEvent | { type: string; [key: string]: unknown };

const LIVE_SPEECH_LEVEL_THRESHOLD = 0.015;
const LIVE_BARGE_IN_LEVEL_THRESHOLD = 0.03;
const LIVE_SPEECH_RESUME_LEVEL_THRESHOLD = 0.0225;
const LIVE_SILENCE_COMMIT_MS = resolveNumberQueryParam("silenceCommitMs", 300);
const LIVE_MIN_SPEECH_CHUNKS = 2;
const LIVE_POST_PLAYBACK_REARM_MS = 180;
const AUDIO_CHUNK_DEBUG_ENABLED = resolveBooleanQueryParam("transportDebug", false);
const AUDIO_CHUNK_NEAR_ZERO_ABS = 8;
const DEBUG_EVENT_LIMIT = 200;
const DEBUG_EVENT_FLUSH_MS = 120;
const GATEWAY_METRIC_KEYS: (keyof GatewayMetrics)[] = [
  "mic_to_first_transcript_ms",
  "commit_to_first_transcript_ms",
  "commit_to_first_text_ms",
  "commit_to_first_audio_delta_ms",
  "commit_to_first_audio_played_ms",
  "full_response_ms",
];

function resolveGatewayHttpUrl(): string {
  if (import.meta.env.VITE_GATEWAY_HTTP_URL) {
    return import.meta.env.VITE_GATEWAY_HTTP_URL;
  }
  return window.location.origin;
}

function metricsEqual(left: GatewayMetrics, right: GatewayMetrics): boolean {
  return GATEWAY_METRIC_KEYS.every((key) => left[key] === right[key]);
}

export default function App() {
  const [speaker, setSpeaker] = useState<Speaker>("Ethan");
  const [sessionState, setSessionState] = useState<"idle" | "connecting" | "ready">("idle");
  const [liveMicActive, setLiveMicActive] = useState(false);
  const [gatewayMetrics, setGatewayMetrics] = useState<GatewayMetrics>({});
  const [assistantText, setAssistantText] = useState("");
  const [transcriptText, setTranscriptText] = useState("");
  const [debugEvents, setDebugEvents] = useState<DebugEvent[]>([]);
  const [errorText, setErrorText] = useState("");
  const [gatewayReady, setGatewayReady] = useState("checking");
  const [assistantPlaying, setAssistantPlaying] = useState(false);

  const clientRef = useRef<RealtimeClient | null>(null);
  const realtimeClientCtorRef = useRef<Promise<typeof import("./realtime/client")> | null>(null);
  const liveCaptureActiveRef = useRef(false);
  const turnActiveRef = useRef(false);
  const assistantResponseActiveRef = useRef(false);
  const assistantPlaybackActiveRef = useRef(false);
  const candidateSpeechChunkCountRef = useRef(0);
  const speechChunkCountRef = useRef(0);
  const commitInFlightRef = useRef(false);
  const bargeInRequestedRef = useRef(false);
  const speechRearmAtRef = useRef(0);
  const silenceLoggedThisTurnRef = useRef(false);
  const silenceRecoveryChunkCountRef = useRef(0);
  const silenceCommitTimerRef = useRef<number | null>(null);
  const commitAtRef = useRef<number | null>(null);
  const localAudioPlayedMetricRef = useRef<number | null>(null);
  const debugEventsRef = useRef<DebugEvent[]>([]);
  const debugFlushTimerRef = useRef<number | null>(null);

  useEffect(() => {
    void refreshGatewayReady();
    return () => {
      if (silenceCommitTimerRef.current !== null) {
        window.clearTimeout(silenceCommitTimerRef.current);
      }
      if (debugFlushTimerRef.current !== null) {
        window.clearTimeout(debugFlushTimerRef.current);
      }
      void clientRef.current?.close();
    };
  }, []);

  const mergedMetrics = useMemo<GatewayMetrics>(() => {
    return {
      ...gatewayMetrics,
      commit_to_first_audio_played_ms:
        localAudioPlayedMetricRef.current ?? gatewayMetrics.commit_to_first_audio_played_ms,
    };
  }, [gatewayMetrics]);

  async function refreshGatewayReady(): Promise<string> {
    try {
      const response = await fetch(`${resolveGatewayHttpUrl()}/ready`);
      const payload = (await response.json()) as { status?: string };
      const status = payload.status ?? "unknown";
      setGatewayReady(status);
      return status;
    } catch {
      setGatewayReady("failed");
      return "failed";
    }
  }

  async function startSession(): Promise<void> {
    setErrorText("");
    setTranscriptText("");
    setAssistantText("");
    resetDebugEvents();
    setGatewayMetrics({});
    localAudioPlayedMetricRef.current = null;
    commitAtRef.current = null;
    resetRealtimeState();

    const readyStatus = await refreshGatewayReady();
    if (readyStatus !== "ready") {
      setErrorText(`Control plane ready state is ${readyStatus}. Wait for Qwen and the LiveKit worker to be ready.`);
      setSessionState("idle");
      return;
    }

    setSessionState("connecting");
    const { RealtimeClient } = await loadRealtimeClientModule();
    const client = new RealtimeClient({
      baseUrl: resolveGatewayHttpUrl(),
      onEvent: handleGatewayEvent,
      onClose: handleSessionClosed,
      onError: (message) => setErrorText(message),
      onDebugEvent: appendDebugEvent,
      onLocalAudioLevel: handleLocalAudioLevel,
      onAssistantPlaybackStarted: handleAssistantPlaybackStarted,
      onAssistantPlaybackDrained: handleAssistantPlaybackDrained,
    });

    try {
      await client.connect(speaker);
      clientRef.current = client;
    } catch (error) {
      await client.close();
      const message = error instanceof Error ? error.message : "Failed to start session.";
      setErrorText(message);
      setSessionState("idle");
    }
  }

  async function loadRealtimeClientModule(): Promise<typeof import("./realtime/client")> {
    if (realtimeClientCtorRef.current === null) {
      realtimeClientCtorRef.current = import("./realtime/client");
    }
    return await realtimeClientCtorRef.current;
  }

  async function stopSession(): Promise<void> {
    clearSilenceCommitTimer();
    liveCaptureActiveRef.current = false;
    const client = clientRef.current;
    clientRef.current = null;
    if (client) {
      try {
        await client.send({ type: "client.session.close" });
      } catch {
        // Ignore shutdown races.
      }
      await client.close();
    }
    resetRealtimeState();
    setSessionState("idle");
    setLiveMicActive(false);
    setAssistantPlaying(false);
  }

  function handleGatewayEvent(event: GatewayInboundEvent): void {
    appendGatewayDebugEvent(event);

    if (event.type === "session.ready") {
      setSessionState("ready");
      liveCaptureActiveRef.current = true;
      setLiveMicActive(true);
      appendLocalDebugEvent("local.capture.live");
      return;
    }

    if (event.type === "transcript.delta") {
      setTranscriptText((current) => `${current}${event.text}`);
      return;
    }

    if (event.type === "assistant.text.delta") {
      setAssistantText((current) => `${current}${event.text}`);
      return;
    }

    if (event.type === "assistant.audio.delta") {
      void clientRef.current?.enqueueAssistantAudio(event);
      return;
    }

    if (event.type === "assistant.done") {
      assistantResponseActiveRef.current = false;
      bargeInRequestedRef.current = false;
      clientRef.current?.finalizeAssistantAudio();
      return;
    }

    if (event.type === "assistant.interrupted") {
      assistantResponseActiveRef.current = false;
      bargeInRequestedRef.current = false;
      clientRef.current?.interruptAssistantAudio();
      return;
    }

    if (event.type === "metrics.update") {
      setGatewayMetrics((current) => (metricsEqual(current, event.metrics) ? current : event.metrics));
      return;
    }

    if (event.type === "error") {
      assistantResponseActiveRef.current = false;
      bargeInRequestedRef.current = false;
      clientRef.current?.interruptAssistantAudio();
      setErrorText(`${event.code}: ${event.message}`);
    }
  }

  function handleAssistantPlaybackStarted(): void {
    setAssistantPlaying(true);
    assistantPlaybackActiveRef.current = true;
    appendLocalDebugEvent("local.playback.started");
    if (commitAtRef.current !== null && localAudioPlayedMetricRef.current === null) {
      localAudioPlayedMetricRef.current = performance.now() - commitAtRef.current;
      setGatewayMetrics((current) => ({ ...current }));
    }
  }

  function handleAssistantPlaybackDrained(): void {
    setAssistantPlaying(false);
    assistantPlaybackActiveRef.current = false;
    speechRearmAtRef.current = performance.now() + LIVE_POST_PLAYBACK_REARM_MS;
    appendLocalDebugEvent("local.playback.drain");
  }

  function handleLocalAudioLevel(level: number): void {
    if (!liveCaptureActiveRef.current || !clientRef.current) {
      return;
    }

    if (performance.now() < speechRearmAtRef.current) {
      if (level < LIVE_SPEECH_LEVEL_THRESHOLD) {
        candidateSpeechChunkCountRef.current = 0;
      }
      return;
    }

    const speechThreshold =
      assistantResponseActiveRef.current || assistantPlaybackActiveRef.current
        ? LIVE_BARGE_IN_LEVEL_THRESHOLD
        : LIVE_SPEECH_LEVEL_THRESHOLD;
    const isSpeech = level >= speechThreshold;
    if (isSpeech) {
      if (assistantResponseActiveRef.current || assistantPlaybackActiveRef.current) {
        cancelResponse();
      }

      if (!turnActiveRef.current) {
        candidateSpeechChunkCountRef.current += 1;
        if (candidateSpeechChunkCountRef.current === 1) {
          appendLocalDebugEvent("local.speech.detected", {
            level: roundLevel(level),
          });
        }
        if (candidateSpeechChunkCountRef.current >= LIVE_MIN_SPEECH_CHUNKS) {
          void startSpeechTurn();
        }
      } else {
        speechChunkCountRef.current += 1;
        if (silenceCommitTimerRef.current !== null) {
          if (level < LIVE_SPEECH_RESUME_LEVEL_THRESHOLD) {
            return;
          }
          silenceRecoveryChunkCountRef.current += 1;
          if (silenceRecoveryChunkCountRef.current >= LIVE_MIN_SPEECH_CHUNKS) {
            appendLocalDebugEvent("local.turn.resume", {
              level: roundLevel(level),
            });
            clearSilenceCommitTimer();
          }
          return;
        }
      }
      silenceRecoveryChunkCountRef.current = 0;
      clearSilenceCommitTimer();
      return;
    }

    if (!turnActiveRef.current) {
      candidateSpeechChunkCountRef.current = 0;
      return;
    }

    silenceRecoveryChunkCountRef.current = 0;
    if (speechChunkCountRef.current < LIVE_MIN_SPEECH_CHUNKS) {
      return;
    }

    scheduleSilenceCommit();
  }

  async function startSpeechTurn(): Promise<void> {
    if (!clientRef.current || turnActiveRef.current) {
      return;
    }

    turnActiveRef.current = true;
    silenceLoggedThisTurnRef.current = false;
    silenceRecoveryChunkCountRef.current = 0;
    candidateSpeechChunkCountRef.current = 0;
    speechChunkCountRef.current = LIVE_MIN_SPEECH_CHUNKS;
    appendLocalDebugEvent("local.turn.started");

    try {
      await clientRef.current.send({ type: "client.speech.start" });
    } catch (error) {
      turnActiveRef.current = false;
      const message = error instanceof Error ? error.message : "Failed to start speech turn.";
      setErrorText(message);
    }
  }

  async function maybeCommitTurn(): Promise<void> {
    if (!clientRef.current || !turnActiveRef.current || commitInFlightRef.current) {
      return;
    }

    clearSilenceCommitTimer();
    commitInFlightRef.current = true;

    try {
      assistantResponseActiveRef.current = true;
      bargeInRequestedRef.current = false;
      commitAtRef.current = performance.now();
      speechRearmAtRef.current = commitAtRef.current + 500;
      localAudioPlayedMetricRef.current = null;
      clientRef.current.resetAssistantPlaybackMonitor();
      appendLocalDebugEvent("local.turn.commit", {
        speech_chunks: speechChunkCountRef.current,
      });
      await clientRef.current.send({ type: "client.speech.commit" });
      turnActiveRef.current = false;
      speechChunkCountRef.current = 0;
      candidateSpeechChunkCountRef.current = 0;
    } catch (error) {
      assistantResponseActiveRef.current = false;
      const message = error instanceof Error ? error.message : "Failed to commit speech turn.";
      setErrorText(message);
    } finally {
      commitInFlightRef.current = false;
    }
  }

  function cancelResponse(): void {
    if (!clientRef.current || bargeInRequestedRef.current) {
      return;
    }
    bargeInRequestedRef.current = true;
    assistantResponseActiveRef.current = false;
    appendLocalDebugEvent("local.turn.barge_in");
    void clientRef.current.send({ type: "client.interrupt" });
  }

  function scheduleSilenceCommit(): void {
    if (silenceCommitTimerRef.current !== null) {
      return;
    }
    if (!silenceLoggedThisTurnRef.current) {
      silenceLoggedThisTurnRef.current = true;
      appendLocalDebugEvent("local.turn.silence");
    }
    silenceRecoveryChunkCountRef.current = 0;
    silenceCommitTimerRef.current = window.setTimeout(() => {
      silenceCommitTimerRef.current = null;
      appendLocalDebugEvent("local.turn.commit_timer_fired");
      void maybeCommitTurn();
    }, LIVE_SILENCE_COMMIT_MS);
  }

  function clearSilenceCommitTimer(): void {
    if (silenceCommitTimerRef.current === null) {
      return;
    }
    window.clearTimeout(silenceCommitTimerRef.current);
    silenceCommitTimerRef.current = null;
  }

  function resetRealtimeState(): void {
    clearSilenceCommitTimer();
    liveCaptureActiveRef.current = false;
    turnActiveRef.current = false;
    assistantResponseActiveRef.current = false;
    assistantPlaybackActiveRef.current = false;
    candidateSpeechChunkCountRef.current = 0;
    speechChunkCountRef.current = 0;
    commitInFlightRef.current = false;
    bargeInRequestedRef.current = false;
    speechRearmAtRef.current = 0;
    silenceLoggedThisTurnRef.current = false;
    silenceRecoveryChunkCountRef.current = 0;
  }

  function handleSessionClosed(): void {
    resetRealtimeState();
    setSessionState("idle");
    setLiveMicActive(false);
    setAssistantPlaying(false);
  }

  function exportDebugLog(): void {
    flushDebugEvents();
    const blob = new Blob([JSON.stringify(debugEventsRef.current, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "realtime-events.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function appendDebugEvent(event: DebugEvent): void {
    const nextEvents = debugEventsRef.current;
    nextEvents.push(event);
    if (nextEvents.length > DEBUG_EVENT_LIMIT) {
      nextEvents.splice(0, nextEvents.length - DEBUG_EVENT_LIMIT);
    }
    scheduleDebugEventFlush();
  }

  function scheduleDebugEventFlush(): void {
    if (debugFlushTimerRef.current !== null) {
      return;
    }
    debugFlushTimerRef.current = window.setTimeout(() => {
      debugFlushTimerRef.current = null;
      setDebugEvents([...debugEventsRef.current]);
    }, DEBUG_EVENT_FLUSH_MS);
  }

  function flushDebugEvents(): void {
    if (debugFlushTimerRef.current !== null) {
      window.clearTimeout(debugFlushTimerRef.current);
      debugFlushTimerRef.current = null;
    }
    setDebugEvents([...debugEventsRef.current]);
  }

  function resetDebugEvents(): void {
    if (debugFlushTimerRef.current !== null) {
      window.clearTimeout(debugFlushTimerRef.current);
      debugFlushTimerRef.current = null;
    }
    debugEventsRef.current = [];
    setDebugEvents([]);
  }

  function appendLocalDebugEvent(type: string, payload: Record<string, unknown> = {}): void {
    appendDebugEvent({
      type,
      perf_now_ms: Math.round(performance.now() * 100) / 100,
      ...payload,
    });
  }

  function appendGatewayDebugEvent(event: GatewayInboundEvent): void {
    if (event.type === "assistant.audio.delta") {
      const audioChunkDebug =
        AUDIO_CHUNK_DEBUG_ENABLED && (event.sample_rate ?? 0) > 0
          ? analyzeAudioBase64Chunk(event.audio_base64, event.sample_rate ?? null)
          : {};
      appendDebugEvent({
        type: event.type,
        perf_now_ms: Math.round(performance.now() * 100) / 100,
        sample_rate: event.sample_rate,
        channels: event.channels,
        format: event.format,
        audio_base64_length: event.audio_base64.length,
        ...audioChunkDebug,
      });
      return;
    }
    appendDebugEvent(event);
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <h1>Realtime AI</h1>
          <p className="status-line">
            Ready: {gatewayReady} | Session: {sessionState} | Playback: {assistantPlaying ? "active" : "idle"}
          </p>
        </div>
        <div className="header-controls">
          <SpeakerSelector
            speaker={speaker}
            disabled={sessionState !== "idle"}
            onChange={setSpeaker}
          />
          <button className="button" onClick={() => void refreshGatewayReady()}>
            Refresh Ready
          </button>
        </div>
      </header>

      <CallControls
        sessionActive={sessionState === "ready"}
        connecting={sessionState === "connecting"}
        startDisabled={sessionState !== "idle" || gatewayReady !== "ready"}
        liveMicActive={liveMicActive}
        onStart={() => void startSession()}
        onStop={() => void stopSession()}
      />

      {errorText ? <div className="error-banner">{errorText}</div> : null}

      <MetricsHud metrics={mergedMetrics} />

      <section className="panel-grid">
        <TranscriptPanel
          title="User Transcript"
          text={transcriptText}
          placeholder="Transcript deltas will appear here."
        />
        <TranscriptPanel
          title="Assistant Text"
          text={assistantText}
          placeholder="Assistant text deltas will appear here."
        />
      </section>

      <section className="panel debug-panel">
        <header className="panel-header">
          <h2>Debug Events</h2>
          <button className="button" onClick={exportDebugLog}>
            Export JSON
          </button>
        </header>
        <pre className="debug-log">{JSON.stringify(debugEvents, null, 2)}</pre>
      </section>
    </main>
  );
}

function roundLevel(level: number): number {
  return Math.round(level * 10000) / 10000;
}

function analyzeAudioBase64Chunk(
  audioBase64: string,
  sampleRate: number | null,
): Record<string, number> {
  const pcm16Bytes = decodeBase64(audioBase64);
  if (pcm16Bytes === null || pcm16Bytes.length < 2 || pcm16Bytes.length % 2 !== 0) {
    return {};
  }

  const sampleCount = pcm16Bytes.length / 2;
  let peakAbs = 0;
  let sumSquares = 0;
  let nearZeroCount = 0;
  let leadingNearZeroCount = 0;
  let trailingNearZeroCount = 0;
  let longestNearZeroRun = 0;
  let currentNearZeroRun = 0;

  for (let index = 0; index < sampleCount; index += 1) {
    const byteOffset = index * 2;
    let sample = pcm16Bytes[byteOffset] | (pcm16Bytes[byteOffset + 1] << 8);
    if (sample >= 0x8000) {
      sample -= 0x10000;
    }

    const absSample = Math.abs(sample);
    peakAbs = Math.max(peakAbs, absSample);
    sumSquares += sample * sample;

    const isNearZero = absSample <= AUDIO_CHUNK_NEAR_ZERO_ABS;
    if (!isNearZero) {
      if (currentNearZeroRun > longestNearZeroRun) {
        longestNearZeroRun = currentNearZeroRun;
      }
      currentNearZeroRun = 0;
      continue;
    }

    nearZeroCount += 1;
    currentNearZeroRun += 1;
    if (index === leadingNearZeroCount) {
      leadingNearZeroCount += 1;
    }
  }

  if (currentNearZeroRun > longestNearZeroRun) {
    longestNearZeroRun = currentNearZeroRun;
  }
  trailingNearZeroCount = currentNearZeroRun;

  const durationMs = sampleRate && sampleRate > 0 ? (sampleCount / sampleRate) * 1000 : 0;
  const rms = Math.sqrt(sumSquares / sampleCount);
  return {
    audio_chunk_duration_ms: roundMetric(durationMs),
    audio_chunk_sample_count: sampleCount,
    audio_chunk_peak_abs: roundMetric(peakAbs / 32768),
    audio_chunk_rms: roundMetric(rms / 32768),
    audio_chunk_near_zero_pct: roundMetric((nearZeroCount / sampleCount) * 100),
    audio_chunk_leading_near_zero_ms: roundMetric((leadingNearZeroCount / sampleCount) * durationMs),
    audio_chunk_trailing_near_zero_ms: roundMetric((trailingNearZeroCount / sampleCount) * durationMs),
    audio_chunk_longest_near_zero_run_ms: roundMetric((longestNearZeroRun / sampleCount) * durationMs),
  };
}

function decodeBase64(value: string): Uint8Array | null {
  try {
    const binary = window.atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  } catch {
    return null;
  }
}

function roundMetric(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function resolveBooleanQueryParam(name: string, fallback: boolean): boolean {
  if (typeof window === "undefined") {
    return fallback;
  }
  const raw = new URLSearchParams(window.location.search).get(name);
  if (raw === null) {
    return fallback;
  }
  const normalized = raw.trim().toLowerCase();
  if (normalized === "1" || normalized === "true") {
    return true;
  }
  if (normalized === "0" || normalized === "false") {
    return false;
  }
  return fallback;
}

function resolveNumberQueryParam(name: string, fallback: number): number {
  if (typeof window === "undefined") {
    return fallback;
  }
  const raw = new URLSearchParams(window.location.search).get(name);
  if (!raw) {
    return fallback;
  }
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}
