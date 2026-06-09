export type Speaker = "Ethan" | "Chelsie" | "Aiden";

export type GatewayMetrics = {
  vad_end_to_commit_ms?: number | null;
  commit_to_qwen_first_audio_ms?: number | null;
  qwen_first_audio_to_livekit_first_frame_ms?: number | null;
  speech_end_to_first_assistant_egress_ms?: number | null;
  speech_start_to_commit_ms?: number | null;
  turn_total_ms?: number | null;
  queue_depth_at_first_frame_ms?: number | null;
  assistant_queue_depth_ms?: number | null;
  interrupt_clear_ms?: number | null;
  commit_to_first_audio_played_ms?: number | null;
  mic_to_first_transcript_ms?: number | null;
  commit_to_first_transcript_ms?: number | null;
  commit_to_first_text_ms?: number | null;
  commit_to_first_audio_delta_ms?: number | null;
  commit_to_first_livekit_egress_ms?: number | null;
  full_response_ms?: number | null;
};

export type GatewayRollups = Record<string, number | null>;

export type GatewayCounters = {
  error_count?: number | null;
  interruption_count?: number | null;
  duplicate_commit_count?: number | null;
  stale_output_drop_count?: number | null;
};

type GatewayEventBase = {
  session_id?: string | null;
  participant_id?: string | null;
  participant_identity?: string | null;
  room?: string | null;
  turn_id?: string | null;
  input_epoch?: number | null;
  interrupt_epoch?: number | null;
  output_version?: number | null;
};

export type GatewayInboundEvent =
  | (GatewayEventBase & {
      type: "session.ready";
      session_id: string;
      backend?: "qwen_realtime_audio" | "qwen_chat_stream_audio" | "text_only";
      degraded_reason?: string | null;
    })
  | (GatewayEventBase & { type: "turn.started"; speech_duration_ms?: number | null })
  | (GatewayEventBase & {
      type: "turn.committed";
      speech_duration_ms?: number | null;
      silence_duration_ms?: number | null;
      input_audio_ms?: number | null;
    })
  | (GatewayEventBase & { type: "transcript.delta"; text: string })
  | (GatewayEventBase & { type: "assistant.text.delta"; text: string })
  | (GatewayEventBase & {
      type: "assistant.audio_started";
      sample_rate?: number | null;
      channels?: number | null;
      format?: string | null;
    })
  | (GatewayEventBase & {
      type: "assistant.audio.metadata";
      sample_rate?: number | null;
      channels?: number | null;
      format?: string | null;
      audio_base64_length?: number | null;
    })
  | (GatewayEventBase & { type: "assistant.done" })
  | (GatewayEventBase & { type: "assistant.interrupted" })
  | (GatewayEventBase & {
      type: "metrics.update";
      metrics: GatewayMetrics;
      timestamps?: Record<string, number | null>;
      rollups?: GatewayRollups;
      counters?: GatewayCounters;
      reconnects?: Record<string, number | null>;
      last_reconnect_reason?: string | null;
    })
  | (GatewayEventBase & { type: "room.busy"; code: "ROOM_BUSY"; message: string })
  | (GatewayEventBase & { type: "error"; code: string; message: string });
