import { useEffect, useMemo, useRef, useState } from "react";

import { CaptureWorkletController } from "./audio/capture-worklet";
import { PlaybackWorkletController } from "./audio/playback-worklet";
import { CallControls } from "./components/CallControls";
import { MetricsHud } from "./components/MetricsHud";
import { SpeakerSelector } from "./components/SpeakerSelector";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { RealtimeClient } from "./realtime/client";
import type { GatewayInboundEvent, GatewayMetrics, SessionMode, Speaker } from "./realtime/events";

const INPUT_SAMPLE_RATE = 16000;
const CHUNK_MS = 80;

function resolveGatewayHttpUrl(): string {
  if (import.meta.env.VITE_GATEWAY_HTTP_URL) {
    return import.meta.env.VITE_GATEWAY_HTTP_URL;
  }
  return `${window.location.protocol}//${window.location.hostname}:8080`;
}

function resolveGatewayWsUrl(): string {
  if (import.meta.env.VITE_GATEWAY_WS_URL) {
    return import.meta.env.VITE_GATEWAY_WS_URL;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:8080/ws/realtime`;
}

export default function App() {
  const [speaker, setSpeaker] = useState<Speaker>("Ethan");
  const [mode, setMode] = useState<SessionMode>("text,audio");
  const [sessionState, setSessionState] = useState<"idle" | "connecting" | "ready">("idle");
  const [pushToTalkActive, setPushToTalkActive] = useState(false);
  const [autoCommit, setAutoCommit] = useState(true);
  const [gatewayMetrics, setGatewayMetrics] = useState<GatewayMetrics>({});
  const [assistantText, setAssistantText] = useState("");
  const [transcriptText, setTranscriptText] = useState("");
  const [debugEvents, setDebugEvents] = useState<GatewayInboundEvent[]>([]);
  const [errorText, setErrorText] = useState<string>("");
  const [gatewayReady, setGatewayReady] = useState<string>("checking");
  const [assistantPlaying, setAssistantPlaying] = useState(false);

  const captureRef = useRef<CaptureWorkletController | null>(null);
  const playbackRef = useRef<PlaybackWorkletController | null>(null);
  const clientRef = useRef<RealtimeClient | null>(null);
  const commitAtRef = useRef<number | null>(null);
  const localAudioPlayedMetricRef = useRef<number | null>(null);

  useEffect(() => {
    void refreshGatewayReady();
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
      onChunk: (audioBase64) => {
        clientRef.current?.send({
          type: "audio.append",
          audio_base64: audioBase64,
          sample_rate: INPUT_SAMPLE_RATE,
          channels: 1,
          format: "pcm16",
        });
      },
      onStarted: () => {
        setErrorText("");
      },
    });
    await captureRef.current.start();
  }

  async function ensurePlayback(): Promise<void> {
    if (mode === "text" || playbackRef.current) {
      return;
    }
    playbackRef.current = new PlaybackWorkletController({
      onStarted: () => {
        setAssistantPlaying(true);
        if (commitAtRef.current !== null && localAudioPlayedMetricRef.current === null) {
          localAudioPlayedMetricRef.current = performance.now() - commitAtRef.current;
          setGatewayMetrics((current) => ({ ...current }));
        }
      },
      onDrained: () => setAssistantPlaying(false),
    });
    await playbackRef.current.start();
  }

  async function startSession(): Promise<void> {
    setErrorText("");
    setTranscriptText("");
    setAssistantText("");
    setDebugEvents([]);
    setGatewayMetrics({});
    localAudioPlayedMetricRef.current = null;
    commitAtRef.current = null;

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
        onClose: () => setSessionState("idle"),
        onError: (message) => setErrorText(message),
      });
      await client.connect();
      clientRef.current = client;
      client.send({
        type: "session.start",
        speaker,
        modalities: mode === "text" ? ["text"] : ["text", "audio"],
        input_sample_rate: INPUT_SAMPLE_RATE,
        output_audio: mode === "text,audio",
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to start session.";
      setErrorText(message);
      setSessionState("idle");
    }
  }

  async function stopSession(): Promise<void> {
    captureRef.current?.setTransmitting(false);
    setPushToTalkActive(false);
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
    setDebugEvents((current) => [...current.slice(-199), event]);
    if (event.type === "session.ready") {
      setSessionState("ready");
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
      if (mode === "text,audio") {
        playbackRef.current?.enqueue(event.audio_base64, event.sample_rate);
        setAssistantPlaying(true);
      }
      return;
    }

    if (event.type === "assistant.done") {
      setPushToTalkActive(false);
      return;
    }

    if (event.type === "metrics.update") {
      setGatewayMetrics(event.metrics);
      return;
    }

    if (event.type === "error") {
      setErrorText(`${event.code}: ${event.message}`);
    }
  }

  function commitAudio(): void {
    if (!clientRef.current) {
      return;
    }
    commitAtRef.current = performance.now();
    localAudioPlayedMetricRef.current = null;
    clientRef.current.send({ type: "audio.commit" });
  }

  function cancelResponse(): void {
    playbackRef.current?.clear();
    setAssistantPlaying(false);
    clientRef.current?.send({ type: "response.cancel" });
  }

  function pushToTalkStart(): void {
    if (sessionState !== "ready" || !captureRef.current) {
      return;
    }
    if (assistantPlaying) {
      cancelResponse();
    }
    captureRef.current.setTransmitting(true);
    setPushToTalkActive(true);
  }

  function pushToTalkEnd(): void {
    if (!captureRef.current) {
      return;
    }
    captureRef.current.setTransmitting(false);
    setPushToTalkActive(false);
    if (autoCommit) {
      commitAudio();
    }
  }

  function exportDebugLog(): void {
    const blob = new Blob([JSON.stringify(debugEvents, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "realtime-events.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <h1>Qwen3-Omni Native Realtime</h1>
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
        pushToTalkActive={pushToTalkActive}
        autoCommit={autoCommit}
        mode={mode}
        onStart={() => void startSession()}
        onStop={() => void stopSession()}
        onPushToTalkStart={pushToTalkStart}
        onPushToTalkEnd={pushToTalkEnd}
        onCommit={commitAudio}
        onCancel={cancelResponse}
        onToggleAutoCommit={setAutoCommit}
        onModeChange={setMode}
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
