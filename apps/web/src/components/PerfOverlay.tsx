type PerfOverlayProps = {
  enabled: boolean;
  sessionState: "idle" | "connecting" | "ready";
  assistantPlaying: boolean;
  livekitConnected: boolean;
  canPlaybackAudio: boolean | null;
  assistantTrackSubscribed: boolean;
  assistantTrackSid: string | null;
  playoutDelayMs: number | null;
  jitterBufferAvgMs: number | null;
  packetsLost: number | null;
  concealedSamples: number | null;
  statsUpdatedAtLabel: string | null;
  commitToAudioPlayedMs: number | null | undefined;
  assistantQueueDepthMs: number | null | undefined;
  qwenReconnectsTotal: number | null;
  qwenLastReconnectReason: string | null;
};

export function PerfOverlay(props: PerfOverlayProps) {
  if (!props.enabled) {
    return null;
  }

  const items = [
    {
      label: "Session",
      value: props.sessionState,
    },
    {
      label: "LiveKit",
      value: props.livekitConnected ? "connected" : "waiting",
    },
    {
      label: "Playback",
      value:
        props.canPlaybackAudio === null
          ? "--"
          : props.canPlaybackAudio
            ? "unlocked"
            : "blocked",
    },
    {
      label: "Assistant Track",
      value: props.assistantTrackSubscribed ? "subscribed" : "missing",
    },
    {
      label: "Track SID",
      value: props.assistantTrackSid ?? "--",
    },
    {
      label: "Assistant Audio",
      value: props.assistantPlaying ? "playing" : "idle",
    },
    {
      label: "Playout Delay",
      value: formatMs(props.playoutDelayMs),
    },
    {
      label: "Jitter Buffer Avg",
      value: formatMs(props.jitterBufferAvgMs),
    },
    {
      label: "Packets Lost",
      value: formatCount(props.packetsLost),
    },
    {
      label: "Concealed Samples",
      value: formatCount(props.concealedSamples),
    },
    {
      label: "Commit to Heard",
      value: formatMs(props.commitToAudioPlayedMs),
    },
    {
      label: "Assistant Queue",
      value: formatMs(props.assistantQueueDepthMs),
    },
    {
      label: "Qwen Reconnects",
      value: formatCount(props.qwenReconnectsTotal),
    },
    {
      label: "Reconnect Reason",
      value: props.qwenLastReconnectReason ?? "--",
    },
    {
      label: "Stats Updated",
      value: props.statsUpdatedAtLabel ?? "--",
    },
  ];

  return (
    <section className="panel perf-panel">
      <header className="panel-header">
        <h2>Perf Overlay</h2>
        <span className="muted">Debug-only LiveKit receiver stats</span>
      </header>
      <div className="metrics-grid">
        {items.map((item) => (
          <div className="metric-card" key={item.label}>
            <span className="metric-label">{item.label}</span>
            <strong className="metric-value metric-value-compact">{item.value}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function formatMs(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "--";
  }
  return `${value.toFixed(1)} ms`;
}

function formatCount(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "--";
  }
  return value.toFixed(0);
}
