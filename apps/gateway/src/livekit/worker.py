from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from math import ceil
from pathlib import Path

from livekit import rtc
from livekit.agents.vad import VADEvent, VADEventType, VAD as InputVAD
from livekit.plugins import silero

from src.config import Settings, get_settings
from src.livekit.auth import build_worker_token
from src.livekit.output import AssistantAudioPublisher
from src.realtime.audio import iter_pcm16_chunks
from src.realtime.session import RealtimeSession

logger = logging.getLogger(__name__)
ASSISTANT_TRACK_NAME = "assistant"
ASSISTANT_TRACK_MAX_BITRATE = 128_000


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
        self.vad_turn_audio = bytearray()
        self.turn_active = False
        self.started = False
        self.control_lock = asyncio.Lock()
        self.audio_task: asyncio.Task | None = None
        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads: list[dict] = []
        self.debug_audio_metadata = False
        self.assistant_egress_active_until = 0.0
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

            if event_type == "client.speech.start":
                await self._start_turn()
                return

            if event_type == "client.speech.commit":
                self.turn_active = False
                self.preroll_audio.clear()
                await self.session.commit_audio()
                return

            if event_type == "client.interrupt":
                self.turn_active = False
                self.preroll_audio.clear()
                await self._hard_interrupt_assistant(reason="client_interrupt")
                return

            if event_type == "client.session.close":
                await self.close()
                return

            logger.debug(
                "Ignoring unsupported LiveKit control event type=%s participant=%s",
                event_type,
                self.participant_identity,
            )

    async def bind_audio_track(self, track: rtc.Track) -> None:
        if self.audio_task is not None:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass

        self.audio_task = asyncio.create_task(self._consume_audio_stream(track))

    async def ingest_audio_frame(self, frame: rtc.AudioFrame) -> None:
        if not self.started:
            return

        audio_bytes = bytes(frame.data)
        if not audio_bytes:
            return

        if self.vad_stream is not None:
            self.vad_stream.push_frame(frame)
            if self.turn_active:
                self.vad_turn_audio.extend(audio_bytes)
            else:
                self.input_vad_preroll.append(audio_bytes)
            return

        encoded = base64.b64encode(audio_bytes).decode("ascii")
        if self.turn_active:
            await self.session.append_audio_chunk(
                audio_base64=encoded,
                sample_rate=frame.sample_rate,
                channels=frame.num_channels,
            )
            return

        self.preroll_audio.append(encoded)

    async def close(self) -> None:
        self.turn_active = False
        self.started = False
        self.preroll_audio.clear()
        self.input_vad_preroll.clear()
        self.vad_turn_audio.clear()
        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        self.assistant_egress_active_until = 0.0
        if self.audio_task is not None:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass
            self.audio_task = None
        if self.vad_stream is not None:
            await self.vad_stream.aclose()
            self.vad_stream = None
        if self.vad_task is not None:
            try:
                await self.vad_task
            except asyncio.CancelledError:
                pass
            self.vad_task = None
        await self.audio_publisher.clear()
        await self.session.close()

    async def _start_turn(self) -> None:
        if self.turn_active:
            return

        if self._should_hard_interrupt_for_new_speech():
            await self._hard_interrupt_assistant(reason="speech_start")

        self.turn_active = True
        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        if self.vad_stream is not None:
            pending_preroll = list(self.input_vad_preroll)
            self.input_vad_preroll.clear()
            self.vad_turn_audio = bytearray()
            for chunk in pending_preroll:
                self.vad_turn_audio.extend(chunk)
            return

        pending_preroll = list(self.preroll_audio)
        self.preroll_audio.clear()
        for encoded in pending_preroll:
            await self.session.append_audio_chunk(
                audio_base64=encoded,
                sample_rate=self.settings.qwen_input_sample_rate,
                channels=1,
            )

    def _queued_assistant_egress_seconds(self) -> float:
        queued_duration_seconds = getattr(self.audio_publisher, "queued_duration_seconds", None)
        if not callable(queued_duration_seconds):
            return 0.0
        try:
            return max(float(queued_duration_seconds()), 0.0)
        except Exception:
            logger.exception(
                "Failed to inspect assistant audio egress queue participant=%s",
                self.participant_identity,
            )
            return 0.0

    def _should_hard_interrupt_for_new_speech(self) -> bool:
        if self.session.has_active_response():
            return True
        if self._queued_assistant_egress_seconds() > 0.0:
            return True
        return time.monotonic() < self.assistant_egress_active_until

    async def _hard_interrupt_assistant(self, *, reason: str) -> None:
        logger.info(
            "Hard interrupting assistant output participant=%s reason=%s queued_ms=%.2f",
            self.participant_identity,
            reason,
            self._queued_assistant_egress_seconds() * 1000,
        )
        self.audio_started_for_turn = False
        self.livekit_egress_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        self.assistant_egress_active_until = 0.0
        await self.audio_publisher.clear()
        await self.session.cancel_response()
        await self._publish_control({"type": "assistant.interrupted"})

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
                "LiveKit audio stream failed for participant %s",
                self.participant_identity,
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
                "LiveKit VAD stream failed for participant %s",
                self.participant_identity,
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
        await self._publish_control(
            {
                "type": "input.speech.start",
                "speech_duration_ms": round(event.speech_duration * 1000, 2),
            }
        )

    async def _handle_vad_end(self, event: VADEvent) -> None:
        if not self.turn_active:
            return

        pcm16_bytes, sample_rate = self._extract_vad_turn_audio(event)
        if not pcm16_bytes:
            logger.info(
                "LiveKit VAD produced an empty speech segment participant=%s speech_duration_ms=%.2f silence_duration_ms=%.2f",
                self.participant_identity,
                event.speech_duration * 1000,
                event.silence_duration * 1000,
            )
            self.turn_active = False
            return

        input_audio_ms = (len(pcm16_bytes) / 2 / sample_rate) * 1000
        for chunk in iter_pcm16_chunks(
            pcm16_bytes,
            duration_ms=self.settings.default_browser_chunk_ms,
            sample_rate=sample_rate,
            pad_final_chunk=True,
        ):
            await self.session.append_audio_chunk(
                audio_base64=base64.b64encode(chunk).decode("ascii"),
                sample_rate=sample_rate,
                channels=1,
            )

        self.turn_active = False
        self.preroll_audio.clear()
        self.input_vad_preroll.clear()
        self.vad_turn_audio.clear()
        await self._publish_control(
            {
                "type": "input.speech.commit",
                "speech_duration_ms": round(event.speech_duration * 1000, 2),
                "silence_duration_ms": round(event.silence_duration * 1000, 2),
                "input_audio_ms": round(input_audio_ms, 2),
            }
        )
        await self.session.commit_audio()

    def _extract_vad_turn_audio(self, event: VADEvent) -> tuple[bytes, int]:
        sample_rate = self.settings.qwen_input_sample_rate
        audio_bytes = bytes(self.vad_turn_audio)
        total_samples = len(audio_bytes) // 2
        if total_samples <= 0:
            return b"", sample_rate

        prefix_samples = int(
            round(self.settings.livekit_input_vad_prefix_padding_duration * sample_rate)
        )
        speech_samples = int(round(event.speech_duration * sample_rate))
        keep_samples = min(total_samples, max(0, prefix_samples + speech_samples))
        if keep_samples <= 0:
            return b"", sample_rate
        return audio_bytes[: keep_samples * 2], sample_rate

    async def _handle_published_frame(self, _frame_bytes: bytes) -> None:
        self._mark_assistant_egress_active()
        if self.livekit_egress_started_for_turn:
            return
        self.livekit_egress_started_for_turn = True
        self.session.mark_livekit_egress_started()

    def _mark_assistant_egress_active(self) -> None:
        queued_seconds = self._queued_assistant_egress_seconds()
        hold_seconds = max(queued_seconds, 0.25)
        self.assistant_egress_active_until = max(
            self.assistant_egress_active_until,
            time.monotonic() + hold_seconds,
        )

    async def _emit_runtime_event(self, payload: dict) -> None:
        event_type = payload.get("type")
        if self._is_stale_output_event(payload):
            return

        if (
            event_type in {"transcript.delta", "assistant.text.delta", "metrics.update"}
            and not self.audio_started_for_turn
        ):
            self.pending_pre_audio_payloads.append(payload)
            return

        if event_type == "assistant.audio.delta":
            audio_base64 = payload.get("audio_base64")
            if not isinstance(audio_base64, str) or not audio_base64:
                return
            is_first_audio_for_turn = not self.audio_started_for_turn
            self.audio_started_for_turn = True
            sample_rate = int(payload.get("sample_rate") or self.settings.qwen_output_sample_rate)
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

        await self._publish_control(payload)

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

        logger.info(
            "Dropping stale LiveKit assistant egress event participant=%s type=%s event_output_version=%s active_output_version=%s",
            self.participant_identity,
            event_type,
            payload_output_version,
            active_output_version,
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
        await self.room.local_participant.publish_data(
            json.dumps(payload),
            reliable=reliable,
            destination_identities=[self.participant_identity],
            topic=self.settings.livekit_control_topic,
        )

    async def _flush_pending_pre_audio_payloads(self) -> None:
        if not self.pending_pre_audio_payloads:
            return
        pending_payloads = self.pending_pre_audio_payloads
        self.pending_pre_audio_payloads = []
        for payload in pending_payloads:
            await self._publish_control(payload)


class LiveKitWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.room = rtc.Room()
        self.stop_event = asyncio.Event()
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
        self.input_vad = build_input_vad(settings)

    async def run(self) -> None:
        self._register_room_handlers()

        logger.info(
            "LiveKit worker config livekit_url=%s api_key_present=%s api_secret_present=%s room=%s agent_id=%s livekit_input_sample_rate=%s qwen_input_sample_rate=%s qwen_output_sample_rate=%s",
            self.settings.livekit_url,
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

        await self.stop_event.wait()

    async def shutdown(self) -> None:
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
            self.stop_event.set()

        @self.room.on("reconnected")
        def on_reconnected() -> None:
            logger.info("LiveKit worker reconnected to room %s", self.settings.livekit_room)

        @self.room.on("reconnecting")
        def on_reconnecting() -> None:
            logger.warning("LiveKit worker reconnecting to room %s", self.settings.livekit_room)

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
            if (
                self.active_participant_identity is not None
                and self.active_participant_identity != participant.identity
            ):
                await self.room.local_participant.publish_data(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "ROOM_BUSY",
                            "message": "This LiveKit room currently supports one active browser session.",
                        }
                    ),
                    reliable=True,
                    destination_identities=[participant.identity],
                    topic=self.settings.livekit_control_topic,
                )
                return

            session = ParticipantBridgeSession(
                settings=self.settings,
                participant_identity=participant.identity,
                room=self.room,
                audio_publisher=self.audio_publisher,
                input_vad=self.input_vad,
            )
            self.sessions[participant.identity] = session
            self.active_participant_identity = participant.identity

        await session.handle_control_message(payload)

    async def _handle_track_subscribed(
        self,
        track: rtc.Track,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        session = self.sessions.get(participant.identity)
        if session is None:
            session = ParticipantBridgeSession(
                settings=self.settings,
                participant_identity=participant.identity,
                room=self.room,
                audio_publisher=self.audio_publisher,
                input_vad=self.input_vad,
            )
            self.sessions[participant.identity] = session
        await session.bind_audio_track(track)

    async def _drop_participant(self, participant_identity: str) -> None:
        session = self.sessions.pop(participant_identity, None)
        if session is None:
            return
        await session.close()
        if self.active_participant_identity == participant_identity:
            self.active_participant_identity = None

async def _run_worker() -> None:
    settings = get_settings()
    worker = LiveKitWorker(settings)
    try:
        await worker.run()
    finally:
        await worker.shutdown()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.qwen_debug_raw_events else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
        "Audio artifact log started output_sample_rate=%d frame_ms=%d queue_ms=%d preroll_frames=%d",
        settings.livekit_output_sample_rate,
        settings.livekit_output_frame_ms,
        settings.livekit_output_queue_ms,
        settings.livekit_preroll_frames,
    )
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
