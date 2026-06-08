import { useEffect, useMemo, useRef, useState } from "react";

import { CallControls } from "./components/CallControls";
import { MetricsHud } from "./components/MetricsHud";
import { SpeakerSelector } from "./components/SpeakerSelector";
import { TranscriptPanel } from "./components/TranscriptPanel";
import type { RealtimeClient } from "./realtime/client";
import type { GatewayInboundEvent, GatewayMetrics, Speaker } from "./realtime/events";

type DebugEvent = GatewayInboundEvent | { type: string; [key: string]: unknown };

const DEBUG_EVENT_LIMIT = 200;
const DEBUG_EVENT_FLUSH_MS = 120;
const GATEWAY_METRIC_KEYS: (keyof GatewayMetrics)[] = [
  "mic_to_first_transcript_ms",
  "commit_to_first_transcript_ms",
  "commit_to_first_text_ms",
  "commit_to_first_audio_delta_ms",
  "commit_to_first_livekit_egress_ms",
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
  const assistantResponseActiveRef = useRef(false);
  const assistantPlaybackActiveRef = useRef(false);
  const commitAtRef = useRef<number | null>(null);
  const localAudioPlayedMetricRef = useRef<number | null>(null);
  const debugEventsRef = useRef<DebugEvent[]>([]);
  const debugFlushTimerRef = useRef<number | null>(null);

  useEffect(() => {
    void refreshGatewayReady();
    return () => {
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

    if (event.type === "input.speech.start") {
      appendLocalDebugEvent("local.turn.started", {
        speech_duration_ms: event.speech_duration_ms ?? null,
      });
      return;
    }

    if (event.type === "input.speech.commit") {
      assistantResponseActiveRef.current = true;
      commitAtRef.current = performance.now();
      localAudioPlayedMetricRef.current = null;
      clientRef.current?.resetAssistantPlaybackMonitor();
      appendLocalDebugEvent("local.turn.commit", {
        speech_duration_ms: event.speech_duration_ms ?? null,
        silence_duration_ms: event.silence_duration_ms ?? null,
        input_audio_ms: event.input_audio_ms ?? null,
      });
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

    if (event.type === "assistant.done") {
      assistantResponseActiveRef.current = false;
      clientRef.current?.finalizeAssistantAudio();
      return;
    }

    if (event.type === "assistant.interrupted") {
      assistantResponseActiveRef.current = false;
      clientRef.current?.interruptAssistantAudio();
      return;
    }

    if (event.type === "metrics.update") {
      setGatewayMetrics((current) => (metricsEqual(current, event.metrics) ? current : event.metrics));
      return;
    }

    if (event.type === "error") {
      assistantResponseActiveRef.current = false;
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
    appendLocalDebugEvent("local.playback.drain");
  }

  function resetRealtimeState(): void {
    liveCaptureActiveRef.current = false;
    assistantResponseActiveRef.current = false;
    assistantPlaybackActiveRef.current = false;
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
