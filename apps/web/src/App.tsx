import { startTransition, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import { CallControls } from "./components/CallControls";
import { MetricsHud } from "./components/MetricsHud";
import { PerfOverlay } from "./components/PerfOverlay";
import { SpeakerSelector } from "./components/SpeakerSelector";
import { TranscriptPanel } from "./components/TranscriptPanel";
import type { RealtimeClient } from "./realtime/client";
import type {
  GatewayCounters,
  GatewayInboundEvent,
  GatewayMetrics,
  GatewayRollups,
  Speaker,
} from "./realtime/events";

type DebugEvent = GatewayInboundEvent | { type: string; [key: string]: unknown };
type PerfOverlayState = {
  livekitConnected: boolean;
  canPlaybackAudio: boolean | null;
  assistantTrackSubscribed: boolean;
  assistantTrackSid: string | null;
  playoutDelayMs: number | null;
  jitterBufferAvgMs: number | null;
  packetsLost: number | null;
  concealedSamples: number | null;
  statsUpdatedAtLabel: string | null;
  qwenReconnectsTotal: number | null;
  qwenLastReconnectReason: string | null;
};
type GatewayReadyPayload = {
  status?: string;
  detail?: string | null;
  livekit_worker_status?: string | null;
  model_server_status?: string | null;
  active_sessions?: number | null;
  assistant_queue_depth_ms?: number | null;
  qwen_reconnects?: number | null;
  qwen_last_reconnect_reason?: string | null;
  error_count?: number | null;
  interruption_count?: number | null;
  duplicate_commit_count?: number | null;
  stale_output_drop_count?: number | null;
  metric_rollups?: GatewayRollups;
};

const DEBUG_EVENT_LIMIT = 200;
const DEBUG_EVENT_FLUSH_MS = 120;
const GATEWAY_METRIC_KEYS: (keyof GatewayMetrics)[] = [
  "vad_end_to_commit_ms",
  "commit_to_qwen_first_audio_ms",
  "qwen_first_audio_to_livekit_first_frame_ms",
  "speech_end_to_first_assistant_egress_ms",
  "speech_start_to_commit_ms",
  "turn_total_ms",
  "queue_depth_at_first_frame_ms",
  "assistant_queue_depth_ms",
  "interrupt_clear_ms",
  "commit_to_first_audio_played_ms",
  "mic_to_first_transcript_ms",
  "commit_to_first_transcript_ms",
  "commit_to_first_text_ms",
  "commit_to_first_audio_delta_ms",
  "commit_to_first_livekit_egress_ms",
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
  const transportDebugEnabled = isTransportDebugEnabled();
  const [speaker, setSpeaker] = useState<Speaker>("Ethan");
  const [sessionState, setSessionState] = useState<"idle" | "connecting" | "ready">("idle");
  const [liveMicActive, setLiveMicActive] = useState(false);
  const [gatewayMetrics, setGatewayMetrics] = useState<GatewayMetrics>({});
  const [gatewayRollups, setGatewayRollups] = useState<GatewayRollups>({});
  const [gatewayCounters, setGatewayCounters] = useState<GatewayCounters>({});
  const [assistantText, setAssistantText] = useState("");
  const [transcriptText, setTranscriptText] = useState("");
  const [debugEvents, setDebugEvents] = useState<DebugEvent[]>([]);
  const [errorText, setErrorText] = useState("");
  const [gatewayReady, setGatewayReady] = useState("checking");
  const [gatewayReadySnapshot, setGatewayReadySnapshot] = useState<GatewayReadyPayload | null>(null);
  const [sessionBackend, setSessionBackend] = useState("inactive");
  const [assistantPlaying, setAssistantPlaying] = useState(false);
  const [perfOverlayState, setPerfOverlayState] = useState<PerfOverlayState>({
    livekitConnected: false,
    canPlaybackAudio: null,
    assistantTrackSubscribed: false,
    assistantTrackSid: null,
    playoutDelayMs: null,
    jitterBufferAvgMs: null,
    packetsLost: null,
    concealedSamples: null,
    statsUpdatedAtLabel: null,
    qwenReconnectsTotal: null,
    qwenLastReconnectReason: null,
  });

  const clientRef = useRef<RealtimeClient | null>(null);
  const realtimeClientCtorRef = useRef<Promise<typeof import("./realtime/client")> | null>(null);
  const liveCaptureActiveRef = useRef(false);
  const assistantResponseActiveRef = useRef(false);
  const assistantPlaybackActiveRef = useRef(false);
  const commitAtRef = useRef<number | null>(null);
  const activeTurnIdRef = useRef<string | null>(null);
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
  const deferredMergedMetrics = useDeferredValue(mergedMetrics);
  const deferredTranscriptText = useDeferredValue(transcriptText);
  const deferredAssistantText = useDeferredValue(assistantText);
  const deferredDebugEvents = useDeferredValue(debugEvents);

  async function refreshGatewayReady(): Promise<string> {
    try {
      const response = await fetch(`${resolveGatewayHttpUrl()}/ready`);
      const payload = (await response.json()) as GatewayReadyPayload;
      const status = payload.status ?? "unknown";
      setGatewayReady(status);
      setGatewayReadySnapshot(payload);
      if (payload.metric_rollups) {
        setGatewayRollups(payload.metric_rollups);
      }
      setGatewayCounters(extractReadyCounters(payload));
      setPerfOverlayState((current) => ({
        ...current,
        qwenReconnectsTotal:
          typeof payload.qwen_reconnects === "number"
            ? payload.qwen_reconnects
            : current.qwenReconnectsTotal,
        qwenLastReconnectReason:
          typeof payload.qwen_last_reconnect_reason === "string"
            ? payload.qwen_last_reconnect_reason
            : current.qwenLastReconnectReason,
      }));
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
    activeTurnIdRef.current = null;
    setSessionBackend("inactive");
    resetPerfOverlayState();
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
    setSessionBackend("inactive");
    setAssistantPlaying(false);
    resetPerfOverlayState();
  }

  async function interruptAssistant(): Promise<void> {
    const client = clientRef.current;
    if (!client || sessionState !== "ready") {
      return;
    }
    appendLocalDebugEvent("local.interrupt.clicked", {
      turn_id: activeTurnIdRef.current,
    });
    try {
      await client.sendInterrupt(activeTurnIdRef.current);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to interrupt assistant output.";
      setErrorText(message);
    }
  }

  function handleGatewayEvent(event: GatewayInboundEvent): void {
    appendGatewayDebugEvent(event);

    if (event.type === "session.ready") {
      setSessionState("ready");
      setSessionBackend(event.backend ?? "unknown");
      liveCaptureActiveRef.current = true;
      setLiveMicActive(true);
      appendLocalDebugEvent("local.capture.live");
      return;
    }

    if (event.type === "turn.started") {
      activeTurnIdRef.current = event.turn_id ?? activeTurnIdRef.current;
      appendLocalDebugEvent("local.turn.started", {
        turn_id: event.turn_id ?? null,
        speech_duration_ms: event.speech_duration_ms ?? null,
      });
      return;
    }

    if (event.type === "turn.committed") {
      assistantResponseActiveRef.current = true;
      commitAtRef.current = performance.now();
      activeTurnIdRef.current = event.turn_id ?? activeTurnIdRef.current;
      localAudioPlayedMetricRef.current = null;
      clientRef.current?.resetAssistantPlaybackMonitor();
      appendLocalDebugEvent("local.turn.commit", {
        turn_id: event.turn_id ?? null,
        speech_duration_ms: event.speech_duration_ms ?? null,
        silence_duration_ms: event.silence_duration_ms ?? null,
        input_audio_ms: event.input_audio_ms ?? null,
      });
      return;
    }

    if (event.type === "assistant.audio_started") {
      appendLocalDebugEvent("local.assistant.audio_started", {
        sample_rate: event.sample_rate ?? null,
        channels: event.channels ?? null,
        format: event.format ?? null,
      });
      return;
    }

    if (event.type === "transcript.delta") {
      startTransition(() => {
        setTranscriptText((current) => `${current}${event.text}`);
      });
      return;
    }

    if (event.type === "assistant.text.delta") {
      startTransition(() => {
        setAssistantText((current) => `${current}${event.text}`);
      });
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
      setGatewayCounters((current) => ({
        ...current,
        interruption_count:
          typeof current.interruption_count === "number" ? current.interruption_count + 1 : 1,
      }));
      return;
    }

    if (event.type === "metrics.update") {
      startTransition(() => {
        setGatewayMetrics((current) => (metricsEqual(current, event.metrics) ? current : event.metrics));
        if (event.rollups) {
          setGatewayRollups(event.rollups);
        }
        if (event.counters) {
          setGatewayCounters(event.counters);
        }
      });
      setPerfOverlayState((current) => ({
        ...current,
        qwenReconnectsTotal:
          event.reconnects !== undefined
            ? Object.values(event.reconnects).reduce<number>(
                (total, value) => total + (typeof value === "number" ? value : 0),
                0,
              )
            : current.qwenReconnectsTotal,
        qwenLastReconnectReason:
          typeof event.last_reconnect_reason === "string"
            ? event.last_reconnect_reason
            : current.qwenLastReconnectReason,
      }));
      return;
    }

    if (event.type === "error") {
      assistantResponseActiveRef.current = false;
      clientRef.current?.interruptAssistantAudio();
      setErrorText(`${event.code}: ${event.message}`);
      setGatewayCounters((current) => ({
        ...current,
        error_count: typeof current.error_count === "number" ? current.error_count + 1 : 1,
      }));
      return;
    }

    if (event.type === "room.busy") {
      assistantResponseActiveRef.current = false;
      clientRef.current?.interruptAssistantAudio();
      setErrorText(`${event.code}: ${event.message}`);
      return;
    }
  }

  function handleAssistantPlaybackStarted(): void {
    setAssistantPlaying(true);
    assistantPlaybackActiveRef.current = true;
    appendLocalDebugEvent("local.playback.started");
    if (commitAtRef.current !== null && localAudioPlayedMetricRef.current === null) {
      localAudioPlayedMetricRef.current = performance.now() - commitAtRef.current;
      startTransition(() => {
        setGatewayMetrics((current) => ({ ...current }));
      });
      void clientRef.current?.reportAssistantPlaybackStarted({
        turnId: activeTurnIdRef.current,
        commitToFirstAudioPlayedMs: localAudioPlayedMetricRef.current,
      });
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
    activeTurnIdRef.current = null;
  }

  function handleSessionClosed(): void {
    resetRealtimeState();
    setSessionState("idle");
    setLiveMicActive(false);
    setSessionBackend("inactive");
    setAssistantPlaying(false);
    resetPerfOverlayState();
  }

  function exportDebugLog(): void {
    if (!transportDebugEnabled) {
      return;
    }
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
    if (!transportDebugEnabled) {
      return;
    }
    updatePerfOverlayState(event);
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
      startTransition(() => {
        setDebugEvents([...debugEventsRef.current]);
      });
    }, DEBUG_EVENT_FLUSH_MS);
  }

  function flushDebugEvents(): void {
    if (debugFlushTimerRef.current !== null) {
      window.clearTimeout(debugFlushTimerRef.current);
      debugFlushTimerRef.current = null;
    }
    startTransition(() => {
      setDebugEvents([...debugEventsRef.current]);
    });
  }

  function resetDebugEvents(): void {
    if (debugFlushTimerRef.current !== null) {
      window.clearTimeout(debugFlushTimerRef.current);
      debugFlushTimerRef.current = null;
    }
    debugEventsRef.current = [];
    startTransition(() => {
      setDebugEvents([]);
    });
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

  function resetPerfOverlayState(): void {
    setPerfOverlayState({
      livekitConnected: false,
      canPlaybackAudio: null,
      assistantTrackSubscribed: false,
      assistantTrackSid: null,
      playoutDelayMs: null,
      jitterBufferAvgMs: null,
      packetsLost: null,
      concealedSamples: null,
      statsUpdatedAtLabel: null,
      qwenReconnectsTotal: null,
      qwenLastReconnectReason: null,
    });
  }

  function updatePerfOverlayState(event: DebugEvent): void {
    setPerfOverlayState((current) => {
      if (event.type === "local.livekit.connected") {
        return {
          ...current,
          livekitConnected: true,
          canPlaybackAudio:
            typeof event.can_playback_audio === "boolean"
              ? event.can_playback_audio
              : current.canPlaybackAudio,
        };
      }

      if (
        event.type === "local.livekit.audio_playback_status" ||
        event.type === "local.livekit.audio_playback_start_resolved"
      ) {
        return {
          ...current,
          canPlaybackAudio:
            typeof event.can_playback_audio === "boolean"
              ? event.can_playback_audio
              : current.canPlaybackAudio,
        };
      }

      if (event.type === "local.livekit.track_playout_delay_configured") {
        return {
          ...current,
          assistantTrackSubscribed: true,
          assistantTrackSid:
            typeof event.track_sid === "string" ? event.track_sid : current.assistantTrackSid,
          playoutDelayMs:
            typeof event.effective_delay_ms === "number"
              ? event.effective_delay_ms
              : current.playoutDelayMs,
        };
      }

      if (event.type === "local.livekit.track_subscribed") {
        const trackName = typeof event.track_name === "string" ? event.track_name : "";
        if (trackName !== "assistant") {
          return current;
        }
        return {
          ...current,
          assistantTrackSubscribed: true,
          assistantTrackSid:
            typeof event.track_sid === "string" ? event.track_sid : current.assistantTrackSid,
        };
      }

      if (event.type === "local.livekit.track_unsubscribed") {
        const trackName = typeof event.track_name === "string" ? event.track_name : "";
        if (trackName !== "assistant") {
          return current;
        }
        return {
          ...current,
          assistantTrackSubscribed: false,
          assistantTrackSid: null,
          jitterBufferAvgMs: null,
          packetsLost: null,
          concealedSamples: null,
          statsUpdatedAtLabel: null,
        };
      }

      if (event.type === "local.livekit.track_stats") {
        return {
          ...current,
          jitterBufferAvgMs:
            typeof event.jitter_buffer_avg_ms === "number"
              ? event.jitter_buffer_avg_ms
              : current.jitterBufferAvgMs,
          packetsLost:
            typeof event.packets_lost === "number" ? event.packets_lost : current.packetsLost,
          concealedSamples:
            typeof event.concealed_samples === "number"
              ? event.concealed_samples
              : current.concealedSamples,
          statsUpdatedAtLabel: new Date().toLocaleTimeString(),
        };
      }

      return current;
    });
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <h1>Realtime AI</h1>
          <p className="status-line">
            Ready: {gatewayReady} | Session: {sessionState} | Backend: {sessionBackend} | Playback: {assistantPlaying ? "active" : "idle"}
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
        onInterrupt={() => void interruptAssistant()}
        onStop={() => void stopSession()}
      />

      {errorText ? <div className="error-banner">{errorText}</div> : null}

      <MetricsHud
        metrics={deferredMergedMetrics}
        rollups={gatewayRollups}
        counters={gatewayCounters}
        qwenReconnectsTotal={
          perfOverlayState.qwenReconnectsTotal ?? gatewayReadySnapshot?.qwen_reconnects ?? null
        }
        activeSessions={gatewayReadySnapshot?.active_sessions ?? null}
        workerStatus={gatewayReadySnapshot?.livekit_worker_status ?? null}
        modelStatus={gatewayReadySnapshot?.model_server_status ?? null}
      />
      <PerfOverlay
        enabled={transportDebugEnabled}
        sessionState={sessionState}
        assistantPlaying={assistantPlaying}
        livekitConnected={perfOverlayState.livekitConnected}
        canPlaybackAudio={perfOverlayState.canPlaybackAudio}
        assistantTrackSubscribed={perfOverlayState.assistantTrackSubscribed}
        assistantTrackSid={perfOverlayState.assistantTrackSid}
        playoutDelayMs={perfOverlayState.playoutDelayMs}
        jitterBufferAvgMs={perfOverlayState.jitterBufferAvgMs}
        packetsLost={perfOverlayState.packetsLost}
        concealedSamples={perfOverlayState.concealedSamples}
        statsUpdatedAtLabel={perfOverlayState.statsUpdatedAtLabel}
        commitToAudioPlayedMs={deferredMergedMetrics.commit_to_first_audio_played_ms}
        assistantQueueDepthMs={deferredMergedMetrics.assistant_queue_depth_ms}
        qwenReconnectsTotal={perfOverlayState.qwenReconnectsTotal}
        qwenLastReconnectReason={perfOverlayState.qwenLastReconnectReason}
      />

      <section className="panel-grid">
        <TranscriptPanel
          title="User Transcript"
          text={deferredTranscriptText}
          placeholder="Transcript deltas will appear here."
        />
        <TranscriptPanel
          title="Assistant Text"
          text={deferredAssistantText}
          placeholder="Assistant text deltas will appear here."
        />
      </section>

      {transportDebugEnabled ? (
        <section className="panel debug-panel">
          <header className="panel-header">
            <h2>Debug Events</h2>
            <button className="button" onClick={exportDebugLog}>
              Export JSON
            </button>
          </header>
          <pre className="debug-log">{JSON.stringify(deferredDebugEvents, null, 2)}</pre>
        </section>
      ) : null}
    </main>
  );
}

function isTransportDebugEnabled(): boolean {
  const override = new URLSearchParams(window.location.search).get("transportDebug");
  return override === "1" || override === "true";
}

function extractReadyCounters(payload: GatewayReadyPayload): GatewayCounters {
  return {
    error_count: payload.error_count ?? null,
    interruption_count: payload.interruption_count ?? null,
    duplicate_commit_count: payload.duplicate_commit_count ?? null,
    stale_output_drop_count: payload.stale_output_drop_count ?? null,
  };
}
