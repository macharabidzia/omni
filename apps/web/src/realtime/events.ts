export type Speaker = "Ethan" | "Chelsie" | "Aiden";

export type GatewayMetrics = {
  mic_to_first_transcript_ms?: number | null;
  commit_to_first_transcript_ms?: number | null;
  commit_to_first_text_ms?: number | null;
  commit_to_first_audio_delta_ms?: number | null;
  commit_to_first_livekit_egress_ms?: number | null;
  commit_to_first_audio_played_ms?: number | null;
  full_response_ms?: number | null;
};

export type GatewayInboundEvent =
  | { type: "session.ready"; session_id: string }
  | { type: "transcript.delta"; text: string }
  | { type: "assistant.text.delta"; text: string }
  | {
      type: "assistant.audio.delta";
      audio_base64: string;
      sample_rate?: number | null;
      channels?: number | null;
      format?: string | null;
    }
  | { type: "assistant.done" }
  | { type: "assistant.interrupted" }
  | { type: "metrics.update"; metrics: GatewayMetrics; timestamps?: Record<string, number | null> }
  | { type: "error"; code: string; message: string };
