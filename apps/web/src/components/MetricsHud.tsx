import type { GatewayCounters, GatewayMetrics, GatewayRollups } from "../realtime/events";

type MetricsHudProps = {
  metrics: GatewayMetrics;
  rollups?: GatewayRollups | null;
  counters?: GatewayCounters | null;
  qwenReconnectsTotal?: number | null;
  activeSessions?: number | null;
  workerStatus?: string | null;
  modelStatus?: string | null;
};

const metricLabels = {
  speech_end_to_first_assistant_egress_ms: "Speech End to LK Egress",
  commit_to_qwen_first_audio_ms: "Commit to Qwen Audio",
  qwen_first_audio_to_livekit_first_frame_ms: "Qwen Audio to LK Frame",
  vad_end_to_commit_ms: "VAD End to Commit",
  speech_start_to_commit_ms: "Speech Start to Commit",
  turn_total_ms: "Turn Total",
  queue_depth_at_first_frame_ms: "Queue at First Frame",
  assistant_queue_depth_ms: "Queue Depth",
  commit_to_first_audio_played_ms: "Commit to Audio Played",
  interrupt_clear_ms: "Interrupt Clear",
} satisfies Partial<Record<keyof GatewayMetrics, string>>;

export function MetricsHud(props: MetricsHudProps) {
  const summaryItems = [
    {
      label: "Speech Egress P50",
      value: formatMs(props.rollups?.speech_end_to_first_assistant_egress_p50_ms),
    },
    {
      label: "Speech Egress P95",
      value: formatMs(props.rollups?.speech_end_to_first_assistant_egress_p95_ms),
    },
    {
      label: "Commit to Qwen P95",
      value: formatMs(props.rollups?.commit_to_qwen_first_audio_p95_ms),
    },
    {
      label: "Qwen to LK P95",
      value: formatMs(props.rollups?.qwen_first_audio_to_livekit_first_frame_p95_ms),
    },
    {
      label: "Queue Depth P95",
      value: formatMs(props.rollups?.assistant_queue_depth_p95_ms),
    },
    {
      label: "Turn Samples",
      value: formatCount(props.rollups?.speech_end_to_first_assistant_egress_count),
    },
    {
      label: "Errors",
      value: formatCount(props.counters?.error_count),
    },
    {
      label: "Interrupts",
      value: formatCount(props.counters?.interruption_count),
    },
    {
      label: "Duplicate Commits",
      value: formatCount(props.counters?.duplicate_commit_count),
    },
    {
      label: "Stale Drops",
      value: formatCount(props.counters?.stale_output_drop_count),
    },
    {
      label: "Reconnects",
      value: formatCount(props.qwenReconnectsTotal),
    },
    {
      label: "Active Sessions",
      value: formatCount(props.activeSessions),
    },
    {
      label: "Worker",
      value: props.workerStatus ?? "--",
    },
    {
      label: "Model",
      value: props.modelStatus ?? "--",
    },
  ];

  return (
    <section className="panel metrics-panel">
      <header className="panel-header">
        <h2>Latency Dashboard</h2>
      </header>
      <div className="metrics-grid">
        {summaryItems.map((item) => (
          <div className="metric-card" key={item.label}>
            <span className="metric-label">{item.label}</span>
            <strong className="metric-value metric-value-compact">{item.value}</strong>
          </div>
        ))}
      </div>
      <div className="metrics-grid">
        {Object.entries(metricLabels).map(([key, label]) => {
          const value = props.metrics[key as keyof GatewayMetrics];
          return (
            <div className="metric-card" key={key}>
              <span className="metric-label">{label}</span>
              <strong className="metric-value">
                {typeof value === "number" ? `${value.toFixed(1)} ms` : "--"}
              </strong>
            </div>
          );
        })}
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
