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
  | { type: "input.speech.start"; speech_duration_ms?: number | null }
  | {
      type: "input.speech.commit";
      speech_duration_ms?: number | null;
      silence_duration_ms?: number | null;
      input_audio_ms?: number | null;
    }
  | { type: "transcript.delta"; text: string }
  | { type: "assistant.text.delta"; text: string; output_version?: number | null }
  | {
      type: "assistant.audio.metadata";
      sample_rate?: number | null;
      channels?: number | null;
      format?: string | null;
      audio_base64_length?: number | null;
      output_version?: number | null;
    }
  | { type: "assistant.done"; output_version?: number | null }
  | { type: "assistant.interrupted" }
  | { type: "metrics.update"; metrics: GatewayMetrics; timestamps?: Record<string, number | null> }
  | { type: "error"; code: string; message: string };
