import type { GatewayMetrics } from "../realtime/events";

type MetricsHudProps = {
  metrics: GatewayMetrics;
};

const metricLabels: Record<keyof GatewayMetrics, string> = {
  mic_to_first_transcript_ms: "Mic to Transcript",
  commit_to_first_transcript_ms: "Commit to Transcript",
  commit_to_first_text_ms: "Commit to Text",
  commit_to_first_audio_delta_ms: "Commit to Audio Delta",
  commit_to_first_audio_played_ms: "Commit to Audio Played",
  full_response_ms: "Full Response",
};

export function MetricsHud(props: MetricsHudProps) {
  return (
    <section className="panel metrics-panel">
      <header className="panel-header">
        <h2>Metrics</h2>
      </header>
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

