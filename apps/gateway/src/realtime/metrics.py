import time
from dataclasses import dataclass, field


TIMESTAMP_FIELDS = (
    "session_start_received",
    "qwen_ws_connected",
    "qwen_session_ready",
    "vad_speech_start",
    "first_user_audio_uploaded",
    "last_user_audio_uploaded",
    "vad_speech_end",
    "commit_sent",
    "qwen_first_transcript",
    "qwen_first_text",
    "qwen_first_audio",
    "first_livekit_frame_captured",
    "assistant_done",
    "turn_closed",
)

DERIVED_METRIC_FIELDS = (
    "vad_end_to_commit_ms",
    "commit_to_qwen_first_audio_ms",
    "qwen_first_audio_to_livekit_first_frame_ms",
    "speech_end_to_first_assistant_egress_ms",
    "speech_start_to_commit_ms",
    "turn_total_ms",
    "queue_depth_at_first_frame_ms",
)


@dataclass(slots=True)
class SessionMetrics:
    session_start_received: float = field(default_factory=time.perf_counter)
    qwen_ws_connected: float | None = None
    qwen_session_ready: float | None = None
    vad_speech_start: float | None = None
    first_user_audio_uploaded: float | None = None
    last_user_audio_uploaded: float | None = None
    vad_speech_end: float | None = None
    commit_sent: float | None = None
    qwen_first_transcript: float | None = None
    qwen_first_text: float | None = None
    qwen_first_audio: float | None = None
    first_livekit_frame_captured: float | None = None
    assistant_done: float | None = None
    turn_closed: float | None = None
    queue_depth_at_first_frame_ms: float | None = None
    first_audio_played_client_ms: float | None = None
    barge_in: float | None = None

    def reset_turn(self) -> None:
        self.vad_speech_start = None
        self.first_user_audio_uploaded = None
        self.last_user_audio_uploaded = None
        self.vad_speech_end = None
        self.commit_sent = None
        self.qwen_first_transcript = None
        self.qwen_first_text = None
        self.qwen_first_audio = None
        self.first_livekit_frame_captured = None
        self.assistant_done = None
        self.turn_closed = None
        self.queue_depth_at_first_frame_ms = None
        self.first_audio_played_client_ms = None
        self.barge_in = None

    def mark_once(self, field_name: str) -> bool:
        if getattr(self, field_name) is not None:
            return False
        setattr(self, field_name, time.perf_counter())
        return True

    def mark_latest(self, field_name: str) -> None:
        setattr(self, field_name, time.perf_counter())

    def set_value_once(self, field_name: str, value: float | None) -> bool:
        if getattr(self, field_name) is not None:
            return False
        setattr(self, field_name, value)
        return True

    def snapshot(self) -> dict[str, dict[str, float | None]]:
        timestamps = {
            name: self._relative_ms(getattr(self, name))
            for name in TIMESTAMP_FIELDS
        }
        metrics = {
            "vad_end_to_commit_ms": self._delta_ms(
                self.vad_speech_end,
                self.commit_sent,
            ),
            "commit_to_qwen_first_audio_ms": self._delta_ms(
                self.commit_sent,
                self.qwen_first_audio,
            ),
            "qwen_first_audio_to_livekit_first_frame_ms": self._delta_ms(
                self.qwen_first_audio,
                self.first_livekit_frame_captured,
            ),
            "speech_end_to_first_assistant_egress_ms": self._delta_ms(
                self.vad_speech_end,
                self.first_livekit_frame_captured,
            ),
            "speech_start_to_commit_ms": self._delta_ms(
                self.vad_speech_start,
                self.commit_sent,
            ),
            "turn_total_ms": self._delta_ms(
                self.vad_speech_start,
                self.turn_closed,
            ),
            "queue_depth_at_first_frame_ms": self.queue_depth_at_first_frame_ms,
        }
        metrics.update(
            {
                "mic_to_first_transcript_ms": self._delta_ms(
                    self.first_user_audio_uploaded,
                    self.qwen_first_transcript,
                ),
                "commit_to_first_transcript_ms": self._delta_ms(
                    self.commit_sent,
                    self.qwen_first_transcript,
                ),
                "commit_to_first_text_ms": self._delta_ms(
                    self.commit_sent,
                    self.qwen_first_text,
                ),
                "commit_to_first_audio_delta_ms": metrics["commit_to_qwen_first_audio_ms"],
                "commit_to_first_livekit_egress_ms": self._delta_ms(
                    self.commit_sent,
                    self.first_livekit_frame_captured,
                ),
                "commit_to_first_audio_played_ms": self.first_audio_played_client_ms,
                "full_response_ms": self._delta_ms(
                    self.commit_sent,
                    self.assistant_done,
                ),
            }
        )
        timestamps.update(
            {
                "t_session_start": timestamps["session_start_received"],
                "t_microphone_started": timestamps["vad_speech_start"],
                "t_first_audio_chunk_sent": timestamps["first_user_audio_uploaded"],
                "t_audio_commit_sent": timestamps["commit_sent"],
                "t_first_transcript_delta": timestamps["qwen_first_transcript"],
                "t_first_text_delta": timestamps["qwen_first_text"],
                "t_first_audio_delta_received": timestamps["qwen_first_audio"],
                "t_first_livekit_egress": timestamps["first_livekit_frame_captured"],
                "t_first_audio_played": self.first_audio_played_client_ms,
                "t_response_done": timestamps["assistant_done"],
                "t_barge_in": self._relative_ms(self.barge_in),
            }
        )
        return {
            "timestamps": timestamps,
            "metrics": metrics,
        }

    def _relative_ms(self, value: float | None) -> float | None:
        if value is None:
            return None
        return round((value - self.session_start_received) * 1000, 2)

    @staticmethod
    def _delta_ms(start: float | None, end: float | None) -> float | None:
        if start is None or end is None:
            return None
        return round(max(end - start, 0.0) * 1000, 2)
