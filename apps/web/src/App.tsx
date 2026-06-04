import { useEffect, useMemo, useRef, useState } from "react";

import { CaptureWorkletController } from "./audio/capture-worklet";
import { PlaybackWorkletController } from "./audio/playback-worklet";
import { CallControls } from "./components/CallControls";
import { MetricsHud } from "./components/MetricsHud";
import { SpeakerSelector } from "./components/SpeakerSelector";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { RealtimeClient } from "./realtime/client";
import type { GatewayInboundEvent, GatewayMetrics, Speaker } from "./realtime/events";

type DebugEvent = GatewayInboundEvent | { type: string; [key: string]: unknown };

const INPUT_SAMPLE_RATE = 16000;
const CHUNK_MS = 20;
const LIVE_SPEECH_LEVEL_THRESHOLD = 0.015;
const LIVE_BARGE_IN_LEVEL_THRESHOLD = 0.045;
const LIVE_SILENCE_COMMIT_MS = 450;
const LIVE_MIN_SPEECH_CHUNKS = 2;
const LIVE_BARGE_IN_MIN_SPEECH_CHUNKS = 4;
const LIVE_PREROLL_CHUNKS = 8;
const LIVE_POST_PLAYBACK_REARM_MS = 180;
const PLAYBACK_DEBUG_QUEUE_EVENT_MIN_STEP_MS = 40;
const DEBUG_EVENT_LIMIT = 200;
const DEBUG_AUDIO_FLUSH_DELAY_MS = 120;
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

function resolveGatewayWsUrl(): string {
  if (import.meta.env.VITE_GATEWAY_WS_URL) {
    return import.meta.env.VITE_GATEWAY_WS_URL;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/realtime`;
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

  const captureRef = useRef<CaptureWorkletController | null>(null);
  const playbackRef = useRef<PlaybackWorkletController | null>(null);
  const clientRef = useRef<RealtimeClient | null>(null);
  const commitAtRef = useRef<number | null>(null);
  const localAudioPlayedMetricRef = useRef<number | null>(null);
  const debugEventsRef = useRef<DebugEvent[]>([]);
  const debugFlushTimerRef = useRef<number | null>(null);
  const silenceCommitTimerRef = useRef<number | null>(null);
  const liveCaptureActiveRef = useRef(false);
  const uploadTurnActiveRef = useRef(false);
  const commitInFlightRef = useRef(false);
  const assistantResponseActiveRef = useRef(false);
  const pendingSpeechTurnRef = useRef(false);
  const speechChunkCountRef = useRef(0);
  const candidateSpeechChunkCountRef = useRef(0);
  const preRollChunksRef = useRef<string[]>([]);
  const queuedCommitRef = useRef(false);
  const bargeInRequestedRef = useRef(false);
  const lastPlaybackQueueMsRef = useRef<number | null>(null);
  const assistantPlaybackActiveRef = useRef(false);
  const assistantPlaybackQueuedMsRef = useRef(0);
  const speechRearmAtRef = useRef(0);
  const bargeInChunkCountRef = useRef(0);
  const bargeInChunksRef = useRef<string[]>([]);
  const silenceLoggedThisTurnRef = useRef(false);

  useEffect(() => {
    void refreshGatewayReady();
    return () => {
      if (debugFlushTimerRef.current !== null) {
        window.clearTimeout(debugFlushTimerRef.current);
      }
      if (silenceCommitTimerRef.current !== null) {
        window.clearTimeout(silenceCommitTimerRef.current);
      }
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
      setGatewayReady("qwen_unreachable");
      return "qwen_unreachable";
    }
  }

  async function ensureCapture(): Promise<void> {
    if (captureRef.current) {
      return;
    }

    captureRef.current = new CaptureWorkletController(INPUT_SAMPLE_RATE, CHUNK_MS, {
      onChunk: ({ audioBase64, level }) => {
        handleCapturedChunk(audioBase64, level);
      },
      onStarted: () => {
        setErrorText("");
      },
    });

    await captureRef.current.start();
  }

  async function ensurePlayback(): Promise<void> {
    if (playbackRef.current) {
      return;
    }

    playbackRef.current = new PlaybackWorkletController({
      onStarted: () => {
        setAssistantPlaying(true);
        assistantPlaybackActiveRef.current = true;
        if (!uploadTurnActiveRef.current) {
          clearSilenceCommitTimer();
          candidateSpeechChunkCountRef.current = 0;
          preRollChunksRef.current = [];
        }
        resetBargeInDetection();
        appendLocalDebugEvent("local.playback.started");
        if (commitAtRef.current !== null && localAudioPlayedMetricRef.current === null) {
          localAudioPlayedMetricRef.current = performance.now() - commitAtRef.current;
          setGatewayMetrics((current) => ({ ...current }));
        }
      },
      onDrained: () => {
        setAssistantPlaying(false);
        assistantResponseActiveRef.current = false;
        bargeInRequestedRef.current = false;
        assistantPlaybackActiveRef.current = false;
        assistantPlaybackQueuedMsRef.current = 0;
        if (!uploadTurnActiveRef.current) {
          clearSilenceCommitTimer();
          candidateSpeechChunkCountRef.current = 0;
          preRollChunksRef.current = [];
        }
        resetBargeInDetection();
        speechRearmAtRef.current = performance.now() + LIVE_POST_PLAYBACK_REARM_MS;
        appendLocalDebugEvent("local.playback.drain");
        if (queuedCommitRef.current && pendingSpeechTurnRef.current) {
          void maybeCommitTurn();
        }
      },
      onQueueChanged: ({ queuedMs }) => {
        assistantPlaybackQueuedMsRef.current = queuedMs;
        const previous = lastPlaybackQueueMsRef.current;
        if (previous === null || Math.abs(previous - queuedMs) >= PLAYBACK_DEBUG_QUEUE_EVENT_MIN_STEP_MS || queuedMs === 0) {
          lastPlaybackQueueMsRef.current = queuedMs;
          appendLocalDebugEvent("local.playback.queue", { queued_ms: queuedMs });
        }
      },
      onCleared: () => {
        assistantResponseActiveRef.current = false;
        bargeInRequestedRef.current = false;
        assistantPlaybackActiveRef.current = false;
        assistantPlaybackQueuedMsRef.current = 0;
        lastPlaybackQueueMsRef.current = 0;
        if (!uploadTurnActiveRef.current) {
          clearSilenceCommitTimer();
          candidateSpeechChunkCountRef.current = 0;
          preRollChunksRef.current = [];
        }
        resetBargeInDetection();
        speechRearmAtRef.current = performance.now() + LIVE_POST_PLAYBACK_REARM_MS;
        setAssistantPlaying(false);
        appendLocalDebugEvent("local.playback.clear");
        if (queuedCommitRef.current && pendingSpeechTurnRef.current) {
          void maybeCommitTurn();
        }
      },
    });

    await playbackRef.current.start();
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
    if (readyStatus !== "qwen_ready") {
      setErrorText(`Gateway ready state is ${readyStatus}. Wait for Qwen to finish loading before starting a session.`);
      setSessionState("idle");
      return;
    }

    setSessionState("connecting");

    try {
      await ensureCapture();
      await ensurePlayback();

      const client = new RealtimeClient({
        url: resolveGatewayWsUrl(),
        onEvent: handleGatewayEvent,
        onClose: handleSessionClosed,
        onError: (message) => setErrorText(message),
      });
      await client.connect();
      clientRef.current = client;
      client.send({
        type: "session.start",
        speaker,
        modalities: ["text", "audio"],
        input_sample_rate: INPUT_SAMPLE_RATE,
        output_audio: true,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to start session.";
      setErrorText(message);
      setSessionState("idle");
    }
  }

  async function stopSession(): Promise<void> {
    captureRef.current?.setTransmitting(false);
    resetRealtimeState();
    setLiveMicActive(false);
    clientRef.current?.send({ type: "session.end" });
    clientRef.current?.close();
    clientRef.current = null;
    await captureRef.current?.stop();
    await playbackRef.current?.stop();
    captureRef.current = null;
    playbackRef.current = null;
    setSessionState("idle");
    setAssistantPlaying(false);
  }

  function handleGatewayEvent(event: GatewayInboundEvent): void {
    appendDebugEvent(event);

    if (event.type === "session.ready") {
      setSessionState("ready");
      liveCaptureActiveRef.current = true;
      captureRef.current?.setTransmitting(true);
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
      assistantResponseActiveRef.current = true;
      bargeInRequestedRef.current = false;
      playbackRef.current?.enqueue(event.audio_base64, event.sample_rate);
      return;
    }

    if (event.type === "assistant.done") {
      assistantResponseActiveRef.current = false;
      bargeInRequestedRef.current = false;
      if (queuedCommitRef.current && pendingSpeechTurnRef.current) {
        void maybeCommitTurn();
      }
      return;
    }

    if (event.type === "metrics.update") {
      setGatewayMetrics((current) => (metricsEqual(current, event.metrics) ? current : event.metrics));
      return;
    }

    if (event.type === "error") {
      assistantResponseActiveRef.current = false;
      bargeInRequestedRef.current = false;
      setErrorText(`${event.code}: ${event.message}`);
      if (queuedCommitRef.current && pendingSpeechTurnRef.current) {
        void maybeCommitTurn();
      }
    }
  }

  async function maybeCommitTurn(): Promise<void> {
    if (!clientRef.current) {
      return;
    }
    if (!pendingSpeechTurnRef.current) {
      queuedCommitRef.current = false;
      return;
    }
    if (commitInFlightRef.current || assistantResponseActiveRef.current) {
      queuedCommitRef.current = true;
      return;
    }

    clearSilenceCommitTimer();
    queuedCommitRef.current = false;
    commitInFlightRef.current = true;

    try {
      assistantResponseActiveRef.current = true;
      bargeInRequestedRef.current = false;
      uploadTurnActiveRef.current = false;
      commitAtRef.current = performance.now();
      localAudioPlayedMetricRef.current = null;
      appendLocalDebugEvent("local.turn.commit", {
        speech_chunks: speechChunkCountRef.current,
      });
      clientRef.current.send({ type: "audio.commit" });
      pendingSpeechTurnRef.current = false;
      speechChunkCountRef.current = 0;
      candidateSpeechChunkCountRef.current = 0;
      preRollChunksRef.current = [];
    } catch (error) {
      assistantResponseActiveRef.current = false;
      queuedCommitRef.current = true;
      const message = error instanceof Error ? error.message : "Failed to commit audio.";
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
    appendLocalDebugEvent("local.turn.barge_in");
    playbackRef.current?.clear();
    lastPlaybackQueueMsRef.current = 0;
    setAssistantPlaying(false);
    clientRef.current.send({ type: "response.cancel" });
  }

  function handleCapturedChunk(audioBase64: string, level: number): void {
    if (!liveCaptureActiveRef.current || !clientRef.current) {
      return;
    }

    if (!uploadTurnActiveRef.current && isAssistantOutputBlocking()) {
      handleSuppressedChunk(audioBase64, level);
      return;
    }

    const isSpeech = level >= LIVE_SPEECH_LEVEL_THRESHOLD;
    if (!uploadTurnActiveRef.current) {
      pushChunkWithLimit(preRollChunksRef.current, audioBase64, LIVE_PREROLL_CHUNKS);
    }

    if (isSpeech) {
      let startedThisChunk = false;
      if (assistantResponseActiveRef.current) {
        cancelResponse();
      }
      if (!uploadTurnActiveRef.current) {
        candidateSpeechChunkCountRef.current += 1;
        if (candidateSpeechChunkCountRef.current === 1) {
          appendLocalDebugEvent("local.speech.detected", {
            level: roundLevel(level),
          });
        }
        if (candidateSpeechChunkCountRef.current >= LIVE_MIN_SPEECH_CHUNKS) {
          startLiveTurnUpload();
          startedThisChunk = true;
        }
      }

      if (uploadTurnActiveRef.current) {
        if (!startedThisChunk) {
          sendAudioChunk(audioBase64);
        }
        pendingSpeechTurnRef.current = true;
        speechChunkCountRef.current += 1;
      }
      clearSilenceCommitTimer();
      return;
    }

    if (!uploadTurnActiveRef.current) {
      candidateSpeechChunkCountRef.current = 0;
      return;
    }

    sendAudioChunk(audioBase64);

    if (!pendingSpeechTurnRef.current || speechChunkCountRef.current < LIVE_MIN_SPEECH_CHUNKS) {
      return;
    }

    scheduleSilenceCommit();
  }

  function startLiveTurnUpload(): void {
    if (uploadTurnActiveRef.current) {
      return;
    }
    uploadTurnActiveRef.current = true;
    pendingSpeechTurnRef.current = true;
    silenceLoggedThisTurnRef.current = false;
    speechChunkCountRef.current = LIVE_MIN_SPEECH_CHUNKS - 1;
    candidateSpeechChunkCountRef.current = 0;

    const preRollChunks = preRollChunksRef.current;
    preRollChunksRef.current = [];
    appendLocalDebugEvent("local.turn.started", {
      preroll_chunks: preRollChunks.length,
    });
    for (const chunk of preRollChunks) {
      sendAudioChunk(chunk);
    }
  }

  function sendAudioChunk(audioBase64: string): void {
    if (!clientRef.current) {
      return;
    }
    try {
      clientRef.current.send({
        type: "audio.append",
        audio_base64: audioBase64,
        sample_rate: INPUT_SAMPLE_RATE,
        channels: 1,
        format: "pcm16",
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to stream microphone audio.";
      setErrorText(message);
    }
  }

  function scheduleSilenceCommit(): void {
    if (silenceCommitTimerRef.current !== null) {
      return;
    }
    if (!silenceLoggedThisTurnRef.current) {
      silenceLoggedThisTurnRef.current = true;
      appendLocalDebugEvent("local.turn.silence");
    }
    silenceCommitTimerRef.current = window.setTimeout(() => {
      silenceCommitTimerRef.current = null;
      queuedCommitRef.current = true;
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
    uploadTurnActiveRef.current = false;
    commitInFlightRef.current = false;
    assistantResponseActiveRef.current = false;
    pendingSpeechTurnRef.current = false;
    speechChunkCountRef.current = 0;
    candidateSpeechChunkCountRef.current = 0;
    preRollChunksRef.current = [];
    queuedCommitRef.current = false;
    bargeInRequestedRef.current = false;
    lastPlaybackQueueMsRef.current = null;
    assistantPlaybackActiveRef.current = false;
    assistantPlaybackQueuedMsRef.current = 0;
    speechRearmAtRef.current = 0;
    silenceLoggedThisTurnRef.current = false;
    resetBargeInDetection();
  }

  function isAssistantOutputBlocking(): boolean {
    return (
      assistantResponseActiveRef.current ||
      assistantPlaybackActiveRef.current ||
      assistantPlaybackQueuedMsRef.current > 0 ||
      performance.now() < speechRearmAtRef.current
    );
  }

  function handleSuppressedChunk(audioBase64: string, level: number): void {
    clearSilenceCommitTimer();
    candidateSpeechChunkCountRef.current = 0;
    preRollChunksRef.current = [];

    if (level < LIVE_BARGE_IN_LEVEL_THRESHOLD) {
      resetBargeInDetection();
      return;
    }

    pushChunkWithLimit(bargeInChunksRef.current, audioBase64, LIVE_PREROLL_CHUNKS);
    bargeInChunkCountRef.current += 1;
    if (bargeInChunkCountRef.current === 1) {
      appendLocalDebugEvent("local.barge_in.detected", {
        level: roundLevel(level),
      });
    }

    if (bargeInChunkCountRef.current < LIVE_BARGE_IN_MIN_SPEECH_CHUNKS) {
      return;
    }

    appendLocalDebugEvent("local.barge_in.accepted", {
      level: roundLevel(level),
      speech_chunks: bargeInChunkCountRef.current,
    });
    cancelResponse();
    preRollChunksRef.current = [...bargeInChunksRef.current];
    const acceptedSpeechChunks = bargeInChunkCountRef.current;
    resetBargeInDetection();
    startLiveTurnUpload();
    speechChunkCountRef.current = acceptedSpeechChunks;
  }

  function resetBargeInDetection(): void {
    bargeInChunkCountRef.current = 0;
    bargeInChunksRef.current = [];
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

    if (event.type === "assistant.audio.delta") {
      scheduleDebugEventFlush();
      return;
    }

    flushDebugEvents();
  }

  function scheduleDebugEventFlush(): void {
    if (debugFlushTimerRef.current !== null) {
      return;
    }

    debugFlushTimerRef.current = window.setTimeout(() => {
      debugFlushTimerRef.current = null;
      setDebugEvents([...debugEventsRef.current]);
    }, DEBUG_AUDIO_FLUSH_DELAY_MS);
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
    appendDebugEvent({ type, ...payload });
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <h1>Realtime AI</h1>
          <p className="status-line">
            Gateway: {gatewayReady} | Session: {sessionState}
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
        startDisabled={sessionState !== "idle" || gatewayReady !== "qwen_ready"}
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

function pushChunkWithLimit(buffer: string[], audioBase64: string, limit: number): void {
  buffer.push(audioBase64);
  if (buffer.length > limit) {
    buffer.splice(0, buffer.length - limit);
  }
}
