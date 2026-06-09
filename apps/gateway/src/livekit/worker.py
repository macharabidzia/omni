from __future__ import annotations

import asyncio
import base64
import json
import logging
import signal
import time
from collections import defaultdict, deque
from enum import Enum
from math import ceil
from pathlib import Path

from livekit import api as livekit_api, rtc
from livekit.agents.vad import VADEvent, VADEventType, VAD as InputVAD
from livekit.plugins import silero

from src.config import Settings, get_settings
from src.livekit.auth import build_worker_token
from src.livekit.output import AssistantAudioPublisher
from src.livekit.worker_state import LiveKitWorkerStateStore
from src.realtime.events import RuntimeGatewayEvent
from src.realtime.session import RealtimeSession
from src.security import redact_url_secrets

logger = logging.getLogger(__name__)
ASSISTANT_TRACK_NAME = "assistant"
ASSISTANT_TRACK_MAX_BITRATE = 128_000
METRIC_ROLLUP_MAX_SAMPLES = 256
MAX_PENDING_PRE_AUDIO_PAYLOADS = 64
UNRELIABLE_CONTROL_EVENT_TYPES = {
    "metrics.update",
    "assistant.audio.metadata",
}


class TurnLifecycleState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    COMMITTING = "committing"
    RESPONDING = "responding"
    INTERRUPTING = "interrupting"
    CLOSED = "closed"


class MetricRollupWindow:
    def __init__(self, *, max_samples: int = METRIC_ROLLUP_MAX_SAMPLES) -> None:
        self.max_samples = max_samples
        self.samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max_samples))

    def record_metrics(self, metrics: dict[str, object], *, last_seen: dict[str, float]) -> None:
        for key, value in metrics.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            numeric_value = float(value)
            previous_value = last_seen.get(key)
            if previous_value is not None and abs(previous_value - numeric_value) < 1e-9:
                continue
            last_seen[key] = numeric_value
            self.samples[key].append(numeric_value)

    def snapshot(self) -> dict[str, float | int]:
        payload: dict[str, float | int] = {}
        for key, values in self.samples.items():
            if not values:
                continue
            numbers = list(values)
            metric_prefix, metric_suffix = self._split_metric_name(key)
            payload[f"{metric_prefix}_last{metric_suffix}"] = round(numbers[-1], 2)
            payload[f"{metric_prefix}_p50{metric_suffix}"] = self._percentile(numbers, 50)
            payload[f"{metric_prefix}_p95{metric_suffix}"] = self._percentile(numbers, 95)
            payload[f"{metric_prefix}_p99{metric_suffix}"] = self._percentile(numbers, 99)
            payload[f"{metric_prefix}_count"] = len(numbers)
        return payload

    @staticmethod
    def _split_metric_name(name: str) -> tuple[str, str]:
        if name.endswith("_ms"):
            return name[:-3], "_ms"
        return name, ""

    @staticmethod
    def _percentile(values: list[float], pct: int) -> float:
        ordered = sorted(values)
        index = min(len(ordered) - 1, ceil((len(ordered) * pct / 100)) - 1)
        return round(ordered[index], 2)


def build_input_vad(settings: Settings) -> InputVAD | None:
    if not settings.livekit_input_vad_enabled:
        return None
    return silero.VAD.load(
        min_speech_duration=settings.livekit_input_vad_min_speech_duration,
        min_silence_duration=settings.livekit_input_vad_min_silence_duration,
        prefix_padding_duration=settings.livekit_input_vad_prefix_padding_duration,
        activation_threshold=settings.livekit_input_vad_activation_threshold,
        sample_rate=settings.qwen_input_sample_rate,
        force_cpu=settings.livekit_input_vad_force_cpu,
    )


def build_assistant_track_publish_options() -> rtc.TrackPublishOptions:
    options = rtc.TrackPublishOptions()
    options.source = rtc.TrackSource.SOURCE_MICROPHONE
    options.dtx = False
    options.red = True
    options.audio_encoding.max_bitrate = ASSISTANT_TRACK_MAX_BITRATE
    return options


class ParticipantBridgeSession:
    def __init__(
        self,
        *,
        settings: Settings,
        participant_identity: str,
        room: rtc.Room,
        audio_publisher: AssistantAudioPublisher,
        metric_rollups: MetricRollupWindow | None = None,
        worker_state_store: LiveKitWorkerStateStore | None = None,
        input_vad: InputVAD | None = None,
    ) -> None:
        self.settings = settings
        self.participant_identity = participant_identity
        self.room = room
        self.audio_publisher = audio_publisher
        self.preroll_audio = deque(maxlen=settings.livekit_preroll_frames)
        self.input_vad_preroll = deque(
            maxlen=max(
                1,
                int(
                    ceil(
                        (settings.livekit_input_vad_prefix_padding_duration * 1000)
                        / settings.default_browser_chunk_ms
                    )
                ),
            )
        )
        self.turn_state = TurnLifecycleState.IDLE
        self.turn_active = False
        self.active_turn_id: str | None = None
        self.response_turn_id: str | None = None
        self.turn_sequence = 0
        self.input_epoch = 0
        self.interrupt_epoch = 0
        self.current_turn_input_bytes = 0
        self.pending_input_audio = bytearray()
        self.pending_input_sample_rate: int | None = None
        self.pending_input_channels = 1
        self.drop_audio_until_turn_boundary = False
        self.started = False
        self.control_lock = asyncio.Lock()
        self.audio_task: asyncio.Task | None = None
        self.livekit_egress_metrics_task: asyncio.Task | None = None
        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads: deque[RuntimeGatewayEvent] = deque(
            maxlen=MAX_PENDING_PRE_AUDIO_PAYLOADS
        )
        self.debug_audio_metadata = False
        self.assistant_egress_active_until = 0.0
        self.browser_idle_deadline: float | None = None
        self.browser_idle_timeout_handle: asyncio.TimerHandle | None = None
        self.last_interrupt_clear_ms: float | None = None
        self.metric_rollups = metric_rollups
        self.worker_state_store = worker_state_store
        self.last_metric_rollup_values: dict[str, float] = {}
        self.input_vad = input_vad
        self.vad_stream = input_vad.stream() if input_vad is not None else None
        self.vad_task = (
            asyncio.create_task(self._consume_vad_events(), name=f"vad:{participant_identity}")
            if self.vad_stream is not None
            else None
        )
        self.session = RealtimeSession(
            settings=settings,
            emit_event=self._emit_runtime_event,
            participant_identity=participant_identity,
            room=settings.livekit_room,
        )
        self.audio_publisher.set_log_context(
            session_id=self.session.session_id,
            participant_identity=self.participant_identity,
            turn_id=None,
        )
        self.audio_publisher.frame_sink = self._handle_published_frame

    async def handle_control_message(self, payload: dict) -> None:
        async with self.control_lock:
            event_type = payload.get("type")
            if event_type == "session.start":
                self.debug_audio_metadata = bool(payload.get("debug_audio_metadata", False))
                await self.session.start_session(
                    {
                        "speaker": payload.get("speaker", self.settings.supported_speakers[0]),
                        "modalities": payload.get("modalities", list(self.settings.default_modalities)),
                        "input_sample_rate": self.settings.qwen_input_sample_rate,
                        "output_audio": True,
                    }
                )
                self.started = True
                self.turn_state = TurnLifecycleState.IDLE
                self.turn_active = False
                self.active_turn_id = None
                self.response_turn_id = None
                self.turn_sequence = 0
                self.input_epoch = 0
                self.interrupt_epoch = 0
                self.current_turn_input_bytes = 0
                self._clear_pending_input_audio()
                self.drop_audio_until_turn_boundary = False
                self.browser_idle_deadline = None
                self.last_interrupt_clear_ms = None
                self.last_metric_rollup_values.clear()
                self.audio_publisher.set_log_context(
                    session_id=self.session.session_id,
                    participant_identity=self.participant_identity,
                    turn_id=None,
                )
                self._record_browser_activity()
                return

            if not self.started:
                await self._publish_control(
                    {
                        "type": "error",
                        "code": "SESSION_NOT_STARTED",
                        "message": "Send session.start before speech control events.",
                    }
                )
                return

            if (
                event_type in {"client.speech.start", "client.speech.commit"}
                and self.vad_stream is not None
            ):
                await self._publish_control(
                    {
                        "type": "error",
                        "code": "TURN_CONTROL_DISABLED",
                        "message": (
                            "Server-side VAD owns speech start and commit while "
                            "LIVEKIT_INPUT_VAD_ENABLED is true."
                        ),
                    }
                )
                return

            self._record_browser_activity()

            if event_type == "client.telemetry":
                await self._handle_client_telemetry(payload)
                return

            if event_type == "client.speech.start":
                await self._start_turn()
                return

            if event_type == "client.speech.commit":
                await self._commit_turn()
                return

            if event_type == "client.interrupt":
                self.preroll_audio.clear()
                self.input_vad_preroll.clear()
                self.active_turn_id = None
                self.current_turn_input_bytes = 0
                self._clear_pending_input_audio()
                self.drop_audio_until_turn_boundary = False
                await self._hard_interrupt_assistant(reason="client_interrupt")
                return

            if event_type == "client.session.close":
                await self.close()
                return

            logger.debug(
                "Ignoring unsupported LiveKit control event type=%s %s",
                event_type,
                self._log_context(),
            )

    async def bind_audio_track(self, track: rtc.Track) -> None:
        if self.audio_task is not None:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass

        self.audio_task = asyncio.create_task(self._consume_audio_stream(track))

    def _set_turn_state(self, new_state: TurnLifecycleState, *, reason: str) -> bool:
        allowed_transitions = {
            TurnLifecycleState.IDLE: {
                TurnLifecycleState.LISTENING,
                TurnLifecycleState.INTERRUPTING,
                TurnLifecycleState.CLOSED,
            },
            TurnLifecycleState.LISTENING: {
                TurnLifecycleState.COMMITTING,
                TurnLifecycleState.INTERRUPTING,
                TurnLifecycleState.CLOSED,
            },
            TurnLifecycleState.COMMITTING: {
                TurnLifecycleState.RESPONDING,
                TurnLifecycleState.INTERRUPTING,
                TurnLifecycleState.IDLE,
                TurnLifecycleState.CLOSED,
            },
            TurnLifecycleState.RESPONDING: {
                TurnLifecycleState.INTERRUPTING,
                TurnLifecycleState.IDLE,
                TurnLifecycleState.CLOSED,
            },
            TurnLifecycleState.INTERRUPTING: {
                TurnLifecycleState.LISTENING,
                TurnLifecycleState.IDLE,
                TurnLifecycleState.CLOSED,
            },
            TurnLifecycleState.CLOSED: set(),
        }
        if self.turn_state == new_state:
            return True
        if new_state not in allowed_transitions[self.turn_state]:
            logger.debug(
                "Ignoring invalid turn state transition from=%s to=%s %s",
                self.turn_state.value,
                new_state.value,
                self._log_context(reason=reason),
            )
            return False
        logger.debug(
            "Turn state transition from=%s to=%s %s",
            self.turn_state.value,
            new_state.value,
            self._log_context(reason=reason),
        )
        self.turn_state = new_state
        self.turn_active = new_state == TurnLifecycleState.LISTENING
        return True

    async def _append_input_audio(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        channels: int = 1,
    ) -> None:
        if not audio_bytes:
            return
        self.current_turn_input_bytes += len(audio_bytes)
        if (
            self.pending_input_audio
            and (
                self.pending_input_sample_rate != sample_rate
                or self.pending_input_channels != channels
            )
        ):
            await self._flush_pending_input_audio()
        self.pending_input_sample_rate = sample_rate
        self.pending_input_channels = channels
        self.pending_input_audio.extend(audio_bytes)
        target_chunk_bytes = self._bytes_for_duration_ms(
            sample_rate=sample_rate,
            channels=channels,
            duration_ms=self.settings.livekit_qwen_input_chunk_ms,
        )
        while len(self.pending_input_audio) >= target_chunk_bytes:
            chunk = bytes(self.pending_input_audio[:target_chunk_bytes])
            del self.pending_input_audio[:target_chunk_bytes]
            await self._send_input_audio_chunk(
                chunk,
                sample_rate=sample_rate,
                channels=channels,
            )

    async def _send_input_audio_chunk(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        channels: int,
    ) -> None:
        await self.session.append_audio_bytes(
            audio_bytes=audio_bytes,
            sample_rate=sample_rate,
            channels=channels,
        )

    def _bytes_for_duration_ms(
        self,
        *,
        sample_rate: int,
        channels: int,
        duration_ms: int,
    ) -> int:
        return int(sample_rate * channels * 2 * duration_ms / 1000)

    async def _flush_pending_input_audio(self) -> None:
        if not self.pending_input_audio or self.pending_input_sample_rate is None:
            self._clear_pending_input_audio()
            return
        sample_rate = self.pending_input_sample_rate
        channels = self.pending_input_channels
        for duration_ms in sorted(self.settings.allowed_chunk_ms, reverse=True):
            chunk_bytes = self._bytes_for_duration_ms(
                sample_rate=sample_rate,
                channels=channels,
                duration_ms=duration_ms,
            )
            while len(self.pending_input_audio) >= chunk_bytes:
                chunk = bytes(self.pending_input_audio[:chunk_bytes])
                del self.pending_input_audio[:chunk_bytes]
                await self._send_input_audio_chunk(
                    chunk,
                    sample_rate=sample_rate,
                    channels=channels,
                )
        if self.pending_input_audio:
            raise RuntimeError(
                "Pending input audio could not be flushed into a supported chunk duration."
            )
        self._clear_pending_input_audio()

    def _clear_pending_input_audio(self) -> None:
        self.pending_input_audio.clear()
        self.pending_input_sample_rate = None
        self.pending_input_channels = 1

    def _current_turn_input_audio_ms(self, *, sample_rate: int) -> float:
        if self.current_turn_input_bytes <= 0:
            return 0.0
        return (self.current_turn_input_bytes / 2 / sample_rate) * 1000

    async def _enforce_max_turn_length(self, *, sample_rate: int) -> None:
        if not self.turn_active:
            return
        input_audio_ms = self._current_turn_input_audio_ms(sample_rate=sample_rate)
        if input_audio_ms < self.settings.livekit_max_turn_ms:
            return
        self.drop_audio_until_turn_boundary = True
        logger.info(
            "Auto-committing overlong LiveKit turn input_audio_ms=%.2f max_turn_ms=%d %s",
            input_audio_ms,
            self.settings.livekit_max_turn_ms,
            self._log_context(turn_id=self.active_turn_id, reason="max_turn_length"),
        )
        await self._publish_control(
            {
                "type": "input.speech.truncated",
                "reason": "max_turn_length",
                "input_audio_ms": round(input_audio_ms, 2),
                "max_turn_ms": self.settings.livekit_max_turn_ms,
                "turn_id": self.active_turn_id,
            }
        )
        await self._commit_turn()

    async def ingest_audio_frame(self, frame: rtc.AudioFrame) -> None:
        if not self.started:
            return

        audio_bytes = bytes(frame.data)
        if not audio_bytes:
            return
        self._record_browser_activity()

        if self.vad_stream is not None:
            self.vad_stream.push_frame(frame)
            if self.drop_audio_until_turn_boundary:
                return
            if self.turn_active:
                await self._append_input_audio(
                    audio_bytes,
                    sample_rate=frame.sample_rate,
                    channels=frame.num_channels,
                )
                await self._enforce_max_turn_length(sample_rate=frame.sample_rate)
            else:
                self.input_vad_preroll.append(audio_bytes)
            return

        if self.drop_audio_until_turn_boundary:
            return

        if self.turn_active:
            await self._append_input_audio(
                audio_bytes,
                sample_rate=frame.sample_rate,
                channels=frame.num_channels,
            )
            await self._enforce_max_turn_length(sample_rate=frame.sample_rate)
            return

        self.preroll_audio.append(audio_bytes)

    async def close(self) -> None:
        self._set_turn_state(TurnLifecycleState.CLOSED, reason="session_close")
        self.started = False
        self._cancel_browser_idle_timeout_watchdog()
        self.preroll_audio.clear()
        self.input_vad_preroll.clear()
        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        self.assistant_egress_active_until = 0.0
        self.browser_idle_deadline = None
        self.active_turn_id = None
        self.response_turn_id = None
        self.current_turn_input_bytes = 0
        self._clear_pending_input_audio()
        self.drop_audio_until_turn_boundary = False
        self.last_interrupt_clear_ms = None
        self.last_metric_rollup_values.clear()
        if self.audio_task is not None:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass
            self.audio_task = None
        if self.livekit_egress_metrics_task is not None:
            self.livekit_egress_metrics_task.cancel()
            try:
                await self.livekit_egress_metrics_task
            except asyncio.CancelledError:
                pass
            self.livekit_egress_metrics_task = None
        vad_stream = self.vad_stream
        if vad_stream is not None:
            await vad_stream.aclose()
        if self.vad_task is not None:
            try:
                await self.vad_task
            except asyncio.CancelledError:
                pass
            self.vad_task = None
        self.vad_stream = None
        await self.audio_publisher.clear()
        self.audio_publisher.set_log_context(
            session_id=self.session.session_id,
            participant_identity=self.participant_identity,
            turn_id=None,
        )
        await self.session.close()

    async def _start_turn(self) -> None:
        if self.turn_state == TurnLifecycleState.CLOSED or self.turn_active:
            return

        if self._should_hard_interrupt_for_new_speech():
            await self._hard_interrupt_assistant(reason="speech_start")

        if not self._set_turn_state(TurnLifecycleState.LISTENING, reason="turn_start"):
            return

        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        self._clear_pending_input_audio()
        self.drop_audio_until_turn_boundary = False
        self.turn_sequence += 1
        self.active_turn_id = str(self.turn_sequence)
        self.response_turn_id = self.active_turn_id
        self.audio_publisher.set_log_context(
            session_id=self.session.session_id,
            participant_identity=self.participant_identity,
            turn_id=self.active_turn_id,
        )
        self.session.begin_turn(turn_id=self.active_turn_id)
        self.input_epoch += 1
        self.current_turn_input_bytes = 0
        if self.vad_stream is None:
            self.session.mark_vad_speech_start()
        if self.vad_stream is not None:
            pending_preroll = list(self.input_vad_preroll)
            self.input_vad_preroll.clear()
            for chunk in pending_preroll:
                await self._append_input_audio(
                    chunk,
                    sample_rate=self.settings.qwen_input_sample_rate,
                    channels=1,
                )
            return

        pending_preroll = list(self.preroll_audio)
        self.preroll_audio.clear()
        for chunk in pending_preroll:
            await self._append_input_audio(
                chunk,
                sample_rate=self.settings.qwen_input_sample_rate,
                channels=1,
            )

    async def _commit_turn(
        self,
        *,
        speech_duration_ms: float | None = None,
        silence_duration_ms: float | None = None,
    ) -> None:
        if not self.turn_active:
            if self.response_turn_id is not None:
                self._increment_worker_counter("duplicate_commit_count")
            return
        if not self._set_turn_state(TurnLifecycleState.COMMITTING, reason="turn_commit"):
            return
        if self.vad_stream is None:
            self.session.mark_vad_speech_end()

        turn_id = self.active_turn_id
        input_audio_ms = self._current_turn_input_audio_ms(
            sample_rate=self.settings.qwen_input_sample_rate
        )
        self.preroll_audio.clear()
        self.input_vad_preroll.clear()
        self.response_turn_id = turn_id
        self.active_turn_id = None
        await self._flush_pending_input_audio()

        payload = {
            "type": "turn.committed",
            "input_audio_ms": round(input_audio_ms, 2),
            "turn_id": turn_id,
        }
        if speech_duration_ms is not None:
            payload["speech_duration_ms"] = round(speech_duration_ms, 2)
        if silence_duration_ms is not None:
            payload["silence_duration_ms"] = round(silence_duration_ms, 2)

        await self._publish_control(payload)
        await self.session.commit_audio()
        self.current_turn_input_bytes = 0
        self._set_turn_state(TurnLifecycleState.RESPONDING, reason="turn_committed")

    def _queued_assistant_egress_seconds(self) -> float:
        queued_duration_seconds = getattr(self.audio_publisher, "queued_duration_seconds", None)
        if not callable(queued_duration_seconds):
            return 0.0
        try:
            return max(float(queued_duration_seconds()), 0.0)
        except Exception:
            logger.exception(
                "Failed to inspect assistant audio egress queue %s",
                self._log_context(),
            )
            return 0.0

    def _should_hard_interrupt_for_new_speech(self) -> bool:
        if self.session.has_active_response():
            return True
        if self._queued_assistant_egress_seconds() > 0.0:
            return True
        return time.monotonic() < self.assistant_egress_active_until

    async def _hard_interrupt_assistant(self, *, reason: str) -> None:
        if (
            not self.turn_active
            and not self.session.has_active_response()
            and self._queued_assistant_egress_seconds() <= 0.0
            and time.monotonic() >= self.assistant_egress_active_until
        ):
            return
        if not self._set_turn_state(TurnLifecycleState.INTERRUPTING, reason=reason):
            return

        interrupted_turn_id = self.response_turn_id or self.active_turn_id
        logger.info(
            "Hard interrupting assistant output queued_ms=%.2f %s",
            self._queued_assistant_egress_seconds() * 1000,
            self._log_context(turn_id=interrupted_turn_id, reason=reason),
        )
        started_at = time.perf_counter()
        self.interrupt_epoch += 1
        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        self.assistant_egress_active_until = 0.0
        self.active_turn_id = None
        self.current_turn_input_bytes = 0
        self._clear_pending_input_audio()
        self.drop_audio_until_turn_boundary = False
        await self.audio_publisher.clear()
        await self.session.cancel_response()
        self.last_interrupt_clear_ms = round(max(time.perf_counter() - started_at, 0.0) * 1000, 2)
        self._increment_worker_counter("interruption_count")
        await self._publish_control(
            {
                "type": "assistant.interrupted",
                "turn_id": interrupted_turn_id,
            }
        )
        self._set_turn_state(TurnLifecycleState.IDLE, reason=f"{reason}_complete")

    async def _consume_audio_stream(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(
            track,
            sample_rate=self.settings.qwen_input_sample_rate,
            num_channels=1,
            frame_size_ms=self.settings.default_browser_chunk_ms,
        )
        try:
            async for frame_event in stream:
                await self.ingest_audio_frame(frame_event.frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LiveKit audio stream failed %s",
                self._log_context(),
            )
            await self._publish_control(
                {
                    "type": "error",
                    "code": "LIVEKIT_AUDIO_STREAM_FAILED",
                    "message": "Browser audio stream ended unexpectedly.",
                }
            )
        finally:
            await stream.aclose()

    async def _consume_vad_events(self) -> None:
        assert self.vad_stream is not None

        try:
            async for event in self.vad_stream:
                if not self.started:
                    continue
                if event.type == VADEventType.START_OF_SPEECH:
                    await self._handle_vad_start(event)
                    continue
                if event.type == VADEventType.END_OF_SPEECH:
                    await self._handle_vad_end(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LiveKit VAD stream failed %s",
                self._log_context(),
            )
            await self._publish_control(
                {
                    "type": "error",
                    "code": "LIVEKIT_VAD_FAILED",
                    "message": "Input voice activity detection failed unexpectedly.",
                }
            )

    async def _handle_vad_start(self, event: VADEvent) -> None:
        if self.turn_active:
            return

        await self._start_turn()
        if not self.turn_active:
            return
        self.session.mark_vad_speech_start()
        await self._publish_control(
            {
                "type": "turn.started",
                "speech_duration_ms": round(event.speech_duration * 1000, 2),
            }
        )

    async def _handle_vad_end(self, event: VADEvent) -> None:
        if self.drop_audio_until_turn_boundary:
            self.drop_audio_until_turn_boundary = False
            self.input_vad_preroll.clear()
            return
        if not self.turn_active:
            return

        if self.current_turn_input_bytes <= 0:
            logger.info(
                "LiveKit VAD produced an empty speech segment speech_duration_ms=%.2f silence_duration_ms=%.2f %s",
                event.speech_duration * 1000,
                event.silence_duration * 1000,
                self._log_context(turn_id=self.active_turn_id, reason="vad_empty_turn"),
            )
            self.active_turn_id = None
            self._set_turn_state(TurnLifecycleState.IDLE, reason="vad_empty_turn")
            return

        self.session.mark_vad_speech_end()
        await self._commit_turn(
            speech_duration_ms=event.speech_duration * 1000,
            silence_duration_ms=event.silence_duration * 1000,
        )

    async def _handle_published_frame(self, _frame_bytes: bytes) -> None:
        self._mark_assistant_egress_active()
        if self.livekit_egress_started_for_turn:
            return
        self.livekit_egress_started_for_turn = True
        self._schedule_livekit_egress_metrics_update(
            queue_depth_ms=round(self._queued_assistant_egress_seconds() * 1000, 2)
        )

    def _mark_assistant_egress_active(self) -> None:
        queued_seconds = self._queued_assistant_egress_seconds()
        hold_seconds = max(queued_seconds, 0.25)
        self.assistant_egress_active_until = max(
            self.assistant_egress_active_until,
            time.monotonic() + hold_seconds,
        )

    def _schedule_livekit_egress_metrics_update(self, *, queue_depth_ms: float) -> None:
        if (
            self.livekit_egress_metrics_task is not None
            and not self.livekit_egress_metrics_task.done()
        ):
            return
        self.livekit_egress_metrics_task = asyncio.create_task(
            self._emit_livekit_egress_metrics_update(queue_depth_ms=queue_depth_ms),
            name=f"livekit-egress-metrics:{self.participant_identity}",
        )

    async def _emit_livekit_egress_metrics_update(self, *, queue_depth_ms: float) -> None:
        try:
            await self.session.mark_livekit_egress_started_with_queue_depth(
                queue_depth_ms=queue_depth_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to emit LiveKit egress metrics update %s",
                self._log_context(reason="livekit_egress_metrics_failed"),
            )
        finally:
            self.livekit_egress_metrics_task = None

    async def _handle_client_telemetry(self, payload: dict) -> None:
        event_name = payload.get("event_name")
        if not isinstance(event_name, str) or not event_name:
            return

        telemetry_turn_id = payload.get("turn_id")
        if not isinstance(telemetry_turn_id, str) or not telemetry_turn_id:
            telemetry_turn_id = self.response_turn_id or self.active_turn_id

        client_epoch_ms = _coerce_optional_float(payload.get("client_epoch_ms"))
        client_perf_now_ms = _coerce_optional_float(payload.get("client_perf_now_ms"))
        commit_to_first_audio_played_ms = _coerce_optional_float(
            payload.get("commit_to_first_audio_played_ms")
        )

        logger.info(
            "Client telemetry event=%s client_epoch_ms=%s client_perf_now_ms=%s commit_to_first_audio_played_ms=%s %s",
            event_name,
            round(client_epoch_ms, 2) if client_epoch_ms is not None else None,
            round(client_perf_now_ms, 2) if client_perf_now_ms is not None else None,
            (
                round(commit_to_first_audio_played_ms, 2)
                if commit_to_first_audio_played_ms is not None
                else None
            ),
            self._log_context(turn_id=telemetry_turn_id),
        )

        if event_name == "assistant.playback.started":
            self.session.mark_browser_first_audio_played(
                latency_ms=commit_to_first_audio_played_ms
            )
            await self.session.flush_metrics_update()

    async def _emit_runtime_event(self, payload: RuntimeGatewayEvent) -> None:
        event_type = payload.get("type")
        if self._is_stale_output_event(payload):
            return
        self._record_browser_activity()

        should_buffer_pre_audio = False
        if event_type in {"transcript.delta", "assistant.text.delta"}:
            should_buffer_pre_audio = not self.audio_started_for_turn
        elif event_type == "metrics.update":
            should_buffer_pre_audio = (
                not self.audio_started_for_turn
                and self.turn_state
                not in {
                    TurnLifecycleState.IDLE,
                    TurnLifecycleState.INTERRUPTING,
                    TurnLifecycleState.CLOSED,
                }
            )

        if should_buffer_pre_audio:
            self._buffer_pre_audio_payload(payload)
            return

        if event_type == "assistant.audio.delta":
            audio_base64 = payload.get("audio_base64")
            if not isinstance(audio_base64, str) or not audio_base64:
                return
            if self.turn_state == TurnLifecycleState.COMMITTING:
                self._set_turn_state(
                    TurnLifecycleState.RESPONDING,
                    reason="assistant_audio_started",
            )
            is_first_audio_for_turn = not self.audio_started_for_turn
            self.audio_started_for_turn = True
            sample_rate = int(payload.get("sample_rate") or self.settings.qwen_output_sample_rate)
            if is_first_audio_for_turn:
                await self._publish_control(
                    {
                        "type": "assistant.audio_started",
                        "sample_rate": sample_rate,
                        "channels": payload.get("channels", 1),
                        "format": payload.get("format", "pcm16"),
                    }
                )
            await self.audio_publisher.enqueue_base64(
                audio_base64,
                input_sample_rate=sample_rate,
            )
            self._mark_assistant_egress_active()
            if is_first_audio_for_turn:
                await self._flush_pending_pre_audio_payloads()
            if self.debug_audio_metadata:
                await self._publish_control(
                    {
                        "type": "assistant.audio.metadata",
                        "sample_rate": sample_rate,
                        "channels": payload.get("channels", 1),
                        "format": payload.get("format", "pcm16"),
                        "audio_base64_length": len(audio_base64),
                        "output_version": self._payload_output_version(payload),
                    }
                )
            return

        if event_type == "assistant.done":
            await self.audio_publisher.finalize_turn()
            if self.audio_started_for_turn:
                self._mark_assistant_egress_active()
            if self.pending_pre_audio_payloads:
                await self._flush_pending_pre_audio_payloads()
            self._set_turn_state(TurnLifecycleState.IDLE, reason="assistant_done")

        await self._publish_control(payload)
        if event_type == "error":
            self._set_turn_state(TurnLifecycleState.IDLE, reason="runtime_error")

    def _is_stale_output_event(self, payload: dict) -> bool:
        event_type = payload.get("type")
        if event_type not in {
            "assistant.text.delta",
            "assistant.audio.delta",
            "assistant.done",
        }:
            return False

        payload_output_version = self._payload_output_version(payload)
        active_output_version = getattr(self.session, "output_version", None)
        if payload_output_version is None or active_output_version is None:
            return False
        if payload_output_version == active_output_version:
            return False

        self._increment_worker_counter("stale_output_drop_count")
        logger.info(
            "Dropping stale LiveKit assistant egress event type=%s event_output_version=%s active_output_version=%s %s",
            event_type,
            payload_output_version,
            active_output_version,
            self._log_context(turn_id=self.response_turn_id or self.active_turn_id),
        )
        return True

    @staticmethod
    def _payload_output_version(payload: dict) -> int | None:
        value = payload.get("output_version")
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    async def _publish_control(self, payload: dict, *, reliable: bool = True) -> None:
        payload = dict(payload)
        event_type = payload.get("type")
        if event_type in UNRELIABLE_CONTROL_EVENT_TYPES:
            reliable = False
        payload.setdefault("participant_id", self.participant_identity)
        payload.setdefault("participant_identity", self.participant_identity)
        payload.setdefault("room", self.settings.livekit_room)
        session_id = getattr(self.session, "session_id", None)
        if isinstance(session_id, str) and session_id:
            payload.setdefault("session_id", session_id)
        if self.metric_rollups is not None and event_type != "metrics.update":
            top_level_numeric_values = {
                key: value
                for key, value in payload.items()
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and key.endswith("_ms")
                )
            }
            if top_level_numeric_values:
                self.metric_rollups.record_metrics(
                    top_level_numeric_values,
                    last_seen=self.last_metric_rollup_values,
                )
                payload["rollups"] = self.metric_rollups.snapshot()
        if event_type == "metrics.update":
            metrics = dict(payload.get("metrics") or {})
            metrics["assistant_queue_depth_ms"] = round(
                self._queued_assistant_egress_seconds() * 1000,
                2,
            )
            if self.last_interrupt_clear_ms is not None:
                metrics["interrupt_clear_ms"] = self.last_interrupt_clear_ms
            payload["metrics"] = metrics
            if self.metric_rollups is not None:
                self.metric_rollups.record_metrics(
                    metrics,
                    last_seen=self.last_metric_rollup_values,
                )
                payload["rollups"] = self.metric_rollups.snapshot()
            if self.worker_state_store is not None:
                reconnects = payload.get("reconnects")
                if isinstance(reconnects, dict):
                    normalized_reconnects = {
                        str(key): int(value)
                        for key, value in reconnects.items()
                        if isinstance(value, int) and not isinstance(value, bool)
                    }
                    self.worker_state_store.update(
                        qwen_reconnects=sum(normalized_reconnects.values()),
                        qwen_reconnect_reasons=normalized_reconnects,
                        qwen_last_reconnect_reason=(
                            payload.get("last_reconnect_reason")
                            if isinstance(payload.get("last_reconnect_reason"), str)
                            else None
                        ),
                    )
            counters = self._worker_counters_snapshot()
            if counters:
                payload["counters"] = counters
        output_version = getattr(self.session, "output_version", None)
        if isinstance(output_version, int) and not isinstance(output_version, bool):
            payload.setdefault("output_version", output_version)
        if "turn_id" not in payload:
            if event_type in {"turn.started", "turn.committed"}:
                turn_id = self.active_turn_id or self.response_turn_id
            else:
                turn_id = self.response_turn_id or self.active_turn_id
            if turn_id is not None:
                payload["turn_id"] = turn_id
        if self.started and event_type != "session.ready":
            payload.setdefault("input_epoch", self.input_epoch)
            payload.setdefault("interrupt_epoch", self.interrupt_epoch)
        if self.worker_state_store is not None and self.metric_rollups is not None:
            self.worker_state_store.record_metric_rollups(
                rollups=self.metric_rollups.snapshot(),
                assistant_queue_depth_ms=self._queued_assistant_egress_seconds() * 1000,
                interrupt_clear_ms=self.last_interrupt_clear_ms,
            )
        if event_type == "error":
            self._increment_worker_counter("error_count")
        await self.room.local_participant.publish_data(
            json.dumps(payload),
            reliable=reliable,
            destination_identities=[self.participant_identity],
            topic=self.settings.livekit_control_topic,
        )

    async def _flush_pending_pre_audio_payloads(self) -> None:
        if not self.pending_pre_audio_payloads:
            return
        pending_payloads = list(self.pending_pre_audio_payloads)
        self.pending_pre_audio_payloads.clear()
        for payload in pending_payloads:
            await self._publish_control(payload)

    def _buffer_pre_audio_payload(self, payload: RuntimeGatewayEvent) -> None:
        if len(self.pending_pre_audio_payloads) >= self.pending_pre_audio_payloads.maxlen:
            dropped_index = next(
                (
                    index
                    for index, buffered_payload in enumerate(self.pending_pre_audio_payloads)
                    if buffered_payload.get("type") == "metrics.update"
                ),
                0,
            )
            dropped_payload = self.pending_pre_audio_payloads[dropped_index]
            del self.pending_pre_audio_payloads[dropped_index]
            logger.warning(
                "Dropping buffered pre-audio payload dropped_type=%s pending_count=%d %s",
                dropped_payload.get("type"),
                len(self.pending_pre_audio_payloads),
                self._log_context(),
            )
        self.pending_pre_audio_payloads.append(payload)

    def _increment_worker_counter(self, name: str) -> None:
        if self.worker_state_store is None:
            return
        self.worker_state_store.increment_counter(name)

    def _worker_counters_snapshot(self) -> dict[str, int]:
        if self.worker_state_store is None:
            return {}
        snapshot = getattr(self.worker_state_store, "snapshot", {})
        counters: dict[str, int] = {}
        for key in (
            "error_count",
            "interruption_count",
            "duplicate_commit_count",
            "stale_output_drop_count",
        ):
            value = snapshot.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                counters[key] = value
        return counters

    def _record_browser_activity(self) -> None:
        if self.turn_state == TurnLifecycleState.CLOSED:
            return
        self.browser_idle_deadline = time.monotonic() + self.settings.livekit_browser_idle_timeout_seconds
        if self.browser_idle_timeout_handle is not None:
            self.browser_idle_timeout_handle.cancel()
        expected_deadline = self.browser_idle_deadline
        self.browser_idle_timeout_handle = asyncio.get_running_loop().call_later(
            self.settings.livekit_browser_idle_timeout_seconds,
            lambda: asyncio.create_task(
                self._handle_browser_idle_timeout_if_deadline(expected_deadline),
                name=f"browser-idle:{self.participant_identity}",
            ),
        )

    def _cancel_browser_idle_timeout_watchdog(self) -> None:
        handle = self.browser_idle_timeout_handle
        self.browser_idle_timeout_handle = None
        self.browser_idle_deadline = None
        if handle is None:
            return
        handle.cancel()

    async def _handle_browser_idle_timeout_if_deadline(self, expected_deadline: float) -> None:
        if self.browser_idle_deadline != expected_deadline:
            return
        self.browser_idle_timeout_handle = None
        await self._handle_browser_idle_timeout()

    async def _handle_browser_idle_timeout(self) -> None:
        async with self.control_lock:
            if not self.started or self.turn_state == TurnLifecycleState.CLOSED:
                return
            logger.warning(
                "Closing idle browser session idle_timeout_seconds=%.2f %s",
                self.settings.livekit_browser_idle_timeout_seconds,
                self._log_context(reason="browser_idle_timeout"),
            )
            await self._publish_control(
                {
                    "type": "error",
                    "code": "BROWSER_IDLE_TIMEOUT",
                    "message": (
                        "The browser session was closed after exceeding the configured idle timeout."
                    ),
                    "reason": "browser_idle_timeout",
                }
            )
            await self.close()

    def _log_context(
        self,
        *,
        turn_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        backend = (
            "inactive"
            if not self.started
            else (
                "qwen_realtime_audio"
                if self.settings.qwen_audio_backend == "realtime"
                else "qwen_chat_stream_audio"
            )
        )
        fields = [
            f"session_id={self.session.session_id}",
            f"participant={self.participant_identity}",
            f"room={self.settings.livekit_room}",
            f"turn_id={turn_id or self.active_turn_id or self.response_turn_id}",
            f"output_version={getattr(self.session, 'output_version', None)}",
            f"backend={backend}",
        ]
        if reason is not None:
            fields.append(f"reason={reason}")
        return " ".join(fields)


class LiveKitWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.room = rtc.Room()
        self.stop_event = asyncio.Event()
        self.draining = False
        self.drain_reason: str | None = None
        self.drain_task: asyncio.Task | None = None
        self.sessions: dict[str, ParticipantBridgeSession] = {}
        self.active_participant_identity: str | None = None
        self.audio_source = rtc.AudioSource(
            settings.livekit_output_sample_rate,
            1,
            queue_size_ms=settings.livekit_output_queue_ms,
        )
        self.audio_publisher = AssistantAudioPublisher(
            audio_source=self.audio_source,
            output_sample_rate=settings.livekit_output_sample_rate,
            output_frame_ms=settings.livekit_output_frame_ms,
        )
        self.metric_rollups = MetricRollupWindow()
        self.worker_state_store = LiveKitWorkerStateStore(path=settings.livekit_worker_state_path)
        self.input_vad = build_input_vad(settings)
        self.livekit_reconnects = 0
        self.worker_state_store.update(
            livekit_worker_status="starting",
            livekit_room_joined=False,
            livekit_input_track_status="idle",
            livekit_output_track_status="missing",
            active_sessions=0,
            livekit_reconnects=self.livekit_reconnects,
        )

    async def request_drain(self, *, reason: str) -> None:
        if self.draining:
            return

        self.draining = True
        self.drain_reason = reason
        logger.warning(
            "LiveKit worker entering drain mode reason=%s active_sessions=%d",
            reason,
            len(self.sessions),
        )
        self.worker_state_store.update(livekit_worker_status="draining")
        if not self.sessions:
            self.stop_event.set()
            return

        self.drain_task = asyncio.create_task(
            self._force_drain_after_timeout(),
            name=f"livekit-drain-timeout:{self.settings.livekit_room}",
        )

    async def _force_drain_after_timeout(self) -> None:
        try:
            await asyncio.sleep(self.settings.livekit_drain_timeout_seconds)
        except asyncio.CancelledError:
            return

        if self.stop_event.is_set():
            return

        logger.warning(
            "LiveKit worker drain timeout reached reason=%s active_sessions=%d timeout_seconds=%.2f",
            self.drain_reason,
            len(self.sessions),
            self.settings.livekit_drain_timeout_seconds,
        )
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()
        self.active_participant_identity = None
        self.worker_state_store.update(
            active_sessions=0,
            livekit_input_track_status="idle",
        )
        self.stop_event.set()

    async def _reject_participant(
        self,
        participant_identity: str,
        *,
        code: str,
        message: str,
        remove_from_room: bool = True,
    ) -> None:
        await self.room.local_participant.publish_data(
            json.dumps(
                {
                    "type": "room.busy",
                    "code": code,
                    "message": message,
                    "room": self.settings.livekit_room,
                    "participant_id": participant_identity,
                    "participant_identity": participant_identity,
                }
            ),
            reliable=True,
            destination_identities=[participant_identity],
            topic=self.settings.livekit_control_topic,
        )
        if not remove_from_room:
            return
        try:
            api_client = livekit_api.LiveKitAPI(
                self.settings.livekit_url,
                self.settings.livekit_api_key,
                self.settings.livekit_api_secret,
            )
        except Exception:
            logger.exception(
                "Failed to create LiveKit API client to remove participant=%s",
                participant_identity,
            )
            return

        try:
            await api_client.room.remove_participant(
                livekit_api.RoomParticipantIdentity(
                    room=self.settings.livekit_room,
                    identity=participant_identity,
                )
            )
            logger.warning(
                "Removed extra LiveKit participant identity=%s active_identity=%s",
                participant_identity,
                self.active_participant_identity,
            )
        except Exception:
            logger.exception(
                "Failed to remove extra LiveKit participant identity=%s active_identity=%s",
                participant_identity,
                self.active_participant_identity,
            )
        finally:
            await api_client.aclose()

    async def run(self) -> None:
        self._register_room_handlers()

        logger.info(
            "LiveKit worker config livekit_url=%s api_key_present=%s api_secret_present=%s room=%s agent_id=%s livekit_input_sample_rate=%s qwen_input_sample_rate=%s qwen_output_sample_rate=%s",
            redact_url_secrets(self.settings.livekit_url),
            bool(self.settings.livekit_api_key),
            bool(self.settings.livekit_api_secret),
            self.settings.livekit_room,
            self.settings.livekit_agent_id,
            self.settings.livekit_input_sample_rate,
            self.settings.qwen_input_sample_rate,
            self.settings.qwen_output_sample_rate,
        )

        token = build_worker_token(self.settings)
        await self.room.connect(self.settings.livekit_url, token)
        logger.info("LiveKit worker connected room=%s", self.settings.livekit_room)
        self.worker_state_store.update(
            livekit_worker_status="connected",
            livekit_room_joined=True,
        )

        assistant_track = rtc.LocalAudioTrack.create_audio_track(
            ASSISTANT_TRACK_NAME,
            self.audio_source,
        )
        publish_options = build_assistant_track_publish_options()
        await self.room.local_participant.publish_track(assistant_track, publish_options)
        logger.info(
            "LiveKit worker published assistant track sample_rate=%s frame_ms=%s queue_ms=%s dtx=%s red=%s max_bitrate=%s",
            self.settings.livekit_output_sample_rate,
            self.settings.livekit_output_frame_ms,
            self.settings.livekit_output_queue_ms,
            publish_options.dtx,
            publish_options.red,
            publish_options.audio_encoding.max_bitrate,
        )
        self.worker_state_store.update(livekit_output_track_status="ready")

        await self.stop_event.wait()

    async def shutdown(self) -> None:
        if self.drain_task is not None:
            self.drain_task.cancel()
            self.drain_task = None
        self.worker_state_store.update(
            livekit_worker_status="stopped",
            livekit_room_joined=False,
            livekit_input_track_status="idle",
            livekit_output_track_status="missing",
            active_sessions=0,
        )
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()
        await self.audio_source.aclose()
        await self.room.disconnect()

    def _register_room_handlers(self) -> None:
        @self.room.on("data_received")
        def on_data_received(data_packet: rtc.DataPacket) -> None:
            asyncio.create_task(self._handle_data_packet(data_packet))

        @self.room.on("track_subscribed")
        def on_track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            del publication
            asyncio.create_task(self._handle_track_subscribed(track, participant))

        @self.room.on("participant_disconnected")
        def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
            asyncio.create_task(self._drop_participant(participant.identity))

        @self.room.on("disconnected")
        def on_disconnected(_reason) -> None:
            logger.warning("LiveKit worker disconnected from room.")
            self.worker_state_store.update(
                livekit_worker_status="disconnected",
                livekit_room_joined=False,
            )
            self.stop_event.set()

        @self.room.on("reconnected")
        def on_reconnected() -> None:
            logger.info("LiveKit worker reconnected to room %s", self.settings.livekit_room)
            self.livekit_reconnects += 1
            self.worker_state_store.update(
                livekit_worker_status="connected",
                livekit_room_joined=True,
                livekit_reconnects=self.livekit_reconnects,
            )

        @self.room.on("reconnecting")
        def on_reconnecting() -> None:
            logger.warning("LiveKit worker reconnecting to room %s", self.settings.livekit_room)
            self.worker_state_store.update(livekit_worker_status="reconnecting")

    async def _handle_data_packet(self, data_packet: rtc.DataPacket) -> None:
        if data_packet.topic != self.settings.livekit_control_topic:
            return

        participant = data_packet.participant
        if participant is None:
            return

        try:
            payload = json.loads(data_packet.data.decode("utf-8"))
        except Exception:
            logger.exception("Invalid LiveKit control payload from %s", participant.identity)
            return

        event_type = payload.get("type")
        client_sent_at_ms = payload.get("_client_sent_at_epoch_ms")
        client_sequence = payload.get("_client_debug_sequence")
        if (
            event_type in {"client.speech.start", "client.speech.commit", "client.interrupt"}
            and isinstance(client_sent_at_ms, (int, float))
        ):
            transit_ms: float | None = None
            transit_ms = max((time.time() * 1000) - float(client_sent_at_ms), 0.0)
            logger.info(
                "LiveKit control received participant=%s type=%s sequence=%s transit_ms=%s reliable_topic=%s",
                participant.identity,
                event_type,
                client_sequence,
                round(transit_ms, 2) if transit_ms is not None else None,
                data_packet.topic,
            )

        session = self.sessions.get(participant.identity)
        if session is None:
            if event_type != "session.start":
                return
            if self.draining:
                await self._reject_participant(
                    participant.identity,
                    code="ROOM_BUSY",
                    message="This LiveKit worker is draining and not accepting new sessions.",
                )
                return
            if (
                self.active_participant_identity is not None
                and self.active_participant_identity != participant.identity
            ):
                await self._reject_participant(
                    participant.identity,
                    code="ROOM_BUSY",
                    message="This LiveKit room currently supports one active browser session.",
                )
                return

            session = ParticipantBridgeSession(
                settings=self.settings,
                participant_identity=participant.identity,
                room=self.room,
                audio_publisher=self.audio_publisher,
                metric_rollups=self.metric_rollups,
                worker_state_store=self.worker_state_store,
                input_vad=self.input_vad,
            )
            self.sessions[participant.identity] = session
            self.active_participant_identity = participant.identity
            self.worker_state_store.update(
                active_sessions=len(self.sessions),
                livekit_input_track_status="ready",
            )

        await session.handle_control_message(payload)

    async def _handle_track_subscribed(
        self,
        track: rtc.Track,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if self.draining and participant.identity not in self.sessions:
            logger.warning(
                "Rejecting subscribed audio track while draining identity=%s room=%s",
                participant.identity,
                self.settings.livekit_room,
            )
            await self._reject_participant(
                participant.identity,
                code="ROOM_BUSY",
                message="This LiveKit worker is draining and not accepting new sessions.",
            )
            return
        if (
            self.active_participant_identity is not None
            and self.active_participant_identity != participant.identity
        ):
            logger.warning(
                "Rejecting subscribed audio track from extra participant identity=%s active_identity=%s",
                participant.identity,
                self.active_participant_identity,
            )
            await self._reject_participant(
                participant.identity,
                code="ROOM_BUSY",
                message="This LiveKit room currently supports one active browser session.",
            )
            return

        session = self.sessions.get(participant.identity)
        if session is None:
            if self.active_participant_identity is None:
                self.active_participant_identity = participant.identity
            session = ParticipantBridgeSession(
                settings=self.settings,
                participant_identity=participant.identity,
                room=self.room,
                audio_publisher=self.audio_publisher,
                metric_rollups=self.metric_rollups,
                worker_state_store=self.worker_state_store,
                input_vad=self.input_vad,
            )
            self.sessions[participant.identity] = session
            self.worker_state_store.update(
                active_sessions=len(self.sessions),
                livekit_input_track_status="ready",
            )
        await session.bind_audio_track(track)

    async def _drop_participant(self, participant_identity: str) -> None:
        session = self.sessions.pop(participant_identity, None)
        if session is None:
            return
        await session.close()
        if self.active_participant_identity == participant_identity:
            self.active_participant_identity = None
        self.worker_state_store.update(
            active_sessions=len(self.sessions),
            livekit_input_track_status="ready" if self.sessions else "idle",
        )
        if self.draining and not self.sessions:
            self.stop_event.set()

async def _run_worker() -> None:
    settings = get_settings()
    worker = LiveKitWorker(settings)
    _install_worker_signal_handlers(worker)
    try:
        await worker.run()
    finally:
        await worker.shutdown()


def configure_worker_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.DEBUG if settings.qwen_debug_raw_events else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not settings.audio_artifact_logging_enabled:
        logging.getLogger(__name__).info(
            "Audio artifact logging disabled output_sample_rate=%d frame_ms=%d queue_ms=%d preroll_frames=%d",
            settings.livekit_output_sample_rate,
            settings.livekit_output_frame_ms,
            settings.livekit_output_queue_ms,
            settings.livekit_preroll_frames,
        )
        return

    artifact_log_path = Path(settings.audio_artifact_log_path)
    artifact_log_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_handler = logging.FileHandler(artifact_log_path, mode="w", encoding="utf-8")
    artifact_handler.setLevel(logging.INFO)
    artifact_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    for logger_name in ("src.livekit.output", "src.livekit.worker"):
        logging.getLogger(logger_name).addHandler(artifact_handler)
    logging.getLogger(__name__).info(
        "Audio artifact log started output_sample_rate=%d frame_ms=%d queue_ms=%d preroll_frames=%d path=%s",
        settings.livekit_output_sample_rate,
        settings.livekit_output_frame_ms,
        settings.livekit_output_queue_ms,
        settings.livekit_preroll_frames,
        artifact_log_path,
    )


def main() -> None:
    settings = get_settings()
    configure_worker_logging(settings)
    asyncio.run(_run_worker())


def _install_worker_signal_handlers(worker: LiveKitWorker) -> None:
    loop = asyncio.get_running_loop()

    def schedule_drain(sig_name: str) -> None:
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                worker.request_drain(reason=f"signal_{sig_name.lower()}")
            )
        )

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, schedule_drain, sig.name)
        except NotImplementedError:
            signal.signal(sig, lambda _signum, _frame, name=sig.name: schedule_drain(name))


def _coerce_optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


if __name__ == "__main__":
    main()
