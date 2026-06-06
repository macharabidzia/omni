import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class SessionMetrics:
    t_session_start: float = field(default_factory=time.perf_counter)
    t_microphone_started: float | None = None
    t_first_audio_chunk_sent: float | None = None
    t_audio_commit_sent: float | None = None
    t_first_transcript_delta: float | None = None
    t_first_text_delta: float | None = None
    t_first_audio_delta_received: float | None = None
    t_first_livekit_egress: float | None = None
    t_first_audio_played: float | None = None
    t_response_done: float | None = None
    t_barge_in: float | None = None

    def mark_once(self, field_name: str) -> bool:
        if getattr(self, field_name) is not None:
            return False
        setattr(self, field_name, time.perf_counter())
        return True

    def snapshot(self) -> dict[str, dict[str, float | None]]:
        return {
            "timestamps": {
                "t_session_start": self._relative_ms(self.t_session_start),
                "t_microphone_started": self._relative_ms(self.t_microphone_started),
                "t_first_audio_chunk_sent": self._relative_ms(self.t_first_audio_chunk_sent),
                "t_audio_commit_sent": self._relative_ms(self.t_audio_commit_sent),
                "t_first_transcript_delta": self._relative_ms(self.t_first_transcript_delta),
                "t_first_text_delta": self._relative_ms(self.t_first_text_delta),
                "t_first_audio_delta_received": self._relative_ms(self.t_first_audio_delta_received),
                "t_first_livekit_egress": self._relative_ms(self.t_first_livekit_egress),
                "t_first_audio_played": self._relative_ms(self.t_first_audio_played),
                "t_response_done": self._relative_ms(self.t_response_done),
                "t_barge_in": self._relative_ms(self.t_barge_in),
            },
            "metrics": {
                "mic_to_first_transcript_ms": self._delta_ms(
                    self.t_microphone_started,
                    self.t_first_transcript_delta,
                ),
                "commit_to_first_transcript_ms": self._delta_ms(
                    self.t_audio_commit_sent,
                    self.t_first_transcript_delta,
                ),
                "commit_to_first_text_ms": self._delta_ms(
                    self.t_audio_commit_sent,
                    self.t_first_text_delta,
                ),
                "commit_to_first_audio_delta_ms": self._delta_ms(
                    self.t_audio_commit_sent,
                    self.t_first_audio_delta_received,
                ),
                "commit_to_first_livekit_egress_ms": self._delta_ms(
                    self.t_audio_commit_sent,
                    self.t_first_livekit_egress,
                ),
                "commit_to_first_audio_played_ms": self._delta_ms(
                    self.t_audio_commit_sent,
                    self.t_first_audio_played,
                ),
                "full_response_ms": self._delta_ms(
                    self.t_audio_commit_sent,
                    self.t_response_done,
                ),
            },
        }

    def _relative_ms(self, value: float | None) -> float | None:
        if value is None:
            return None
        return round((value - self.t_session_start) * 1000, 2)

    @staticmethod
    def _delta_ms(start: float | None, end: float | None) -> float | None:
        if start is None or end is None:
            return None
        return round(max(end - start, 0.0) * 1000, 2)
