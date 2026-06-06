from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque

from livekit import rtc

from src.config import Settings, get_settings
from src.livekit.auth import build_worker_token
from src.livekit.output import AssistantAudioPublisher
from src.realtime.session import RealtimeSession

logger = logging.getLogger(__name__)
ASSISTANT_TRACK_NAME = "assistant"
PCM_AUDIO_PACKET_TYPE = 1


class ParticipantBridgeSession:
    def __init__(
        self,
        *,
        settings: Settings,
        participant_identity: str,
        room: rtc.Room,
        audio_publisher: AssistantAudioPublisher,
    ) -> None:
        self.settings = settings
        self.participant_identity = participant_identity
        self.room = room
        self.audio_publisher = audio_publisher
        self.preroll_audio = deque(maxlen=settings.livekit_preroll_frames)
        self.turn_active = False
        self.started = False
        self.audio_task: asyncio.Task | None = None
        self.audio_started_for_turn = False
        self.pending_pre_audio_payloads: list[dict] = []
        self.debug_audio_deltas = False
        self.session = RealtimeSession(
            settings=settings,
            emit_event=self._emit_runtime_event,
        )

    async def handle_control_message(self, payload: dict) -> None:
        event_type = payload.get("type")
        if event_type == "session.start":
            self.debug_audio_deltas = bool(payload.get("debug_audio_deltas", False))
            await self.session.start_session(
                {
                    "speaker": payload.get("speaker", self.settings.supported_speakers[0]),
                    "modalities": payload.get("modalities", list(self.settings.default_modalities)),
                    "input_sample_rate": self.settings.livekit_input_sample_rate,
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
            self.audio_started_for_turn = False
            self.pending_pre_audio_payloads.clear()
            await self.audio_publisher.clear()
            await self.session.cancel_response()
            await self._publish_control({"type": "assistant.interrupted"})
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
        self.audio_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        if self.audio_task is not None:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass
            self.audio_task = None
        await self.audio_publisher.clear()
        await self.session.close()

    async def _start_turn(self) -> None:
        if self.turn_active:
            return

        self.turn_active = True
        self.audio_started_for_turn = False
        self.pending_pre_audio_payloads.clear()
        pending_preroll = list(self.preroll_audio)
        self.preroll_audio.clear()
        for encoded in pending_preroll:
            await self.session.append_audio_chunk(
                audio_base64=encoded,
                sample_rate=self.settings.livekit_input_sample_rate,
                channels=1,
            )

    async def _consume_audio_stream(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(
            track,
            sample_rate=self.settings.livekit_input_sample_rate,
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

    async def _emit_runtime_event(self, payload: dict) -> None:
        event_type = payload.get("type")
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
            sample_rate = int(payload.get("sample_rate") or self.settings.output_sample_rate)
            await self.audio_publisher.enqueue_base64(
                audio_base64,
                input_sample_rate=sample_rate,
            )
            if is_first_audio_for_turn:
                await self._flush_pending_pre_audio_payloads()
            if self.debug_audio_deltas:
                await self._publish_control(payload)
            return

        if event_type == "assistant.done":
            await self.audio_publisher.finalize_turn()
            if self.pending_pre_audio_payloads:
                await self._flush_pending_pre_audio_payloads()

        await self._publish_control(payload)

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

    async def run(self) -> None:
        self._register_room_handlers()

        logger.info(
            "LiveKit worker config livekit_url=%s api_key_present=%s api_secret_present=%s room=%s agent_id=%s",
            self.settings.livekit_url,
            bool(self.settings.livekit_api_key),
            bool(self.settings.livekit_api_secret),
            self.settings.livekit_room,
            self.settings.livekit_agent_id,
        )

        token = build_worker_token(self.settings)
        await self.room.connect(self.settings.livekit_url, token)
        logger.info("LiveKit worker connected room=%s", self.settings.livekit_room)

        assistant_track = rtc.LocalAudioTrack.create_audio_track(
            ASSISTANT_TRACK_NAME,
            self.audio_source,
        )
        publish_options = rtc.TrackPublishOptions()
        publish_options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await self.room.local_participant.publish_track(assistant_track, publish_options)
        logger.info(
            "LiveKit worker published assistant track sample_rate=%s frame_ms=%s queue_ms=%s",
            self.settings.livekit_output_sample_rate,
            self.settings.livekit_output_frame_ms,
            self.settings.livekit_output_queue_ms,
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

    async def _publish_audio_packet(self, frame_bytes: bytes) -> None:
        if not frame_bytes or self.active_participant_identity is None:
            return

        payload = bytearray()
        payload.append(PCM_AUDIO_PACKET_TYPE)
        payload.extend(self.settings.livekit_output_sample_rate.to_bytes(4, byteorder="big"))
        payload.append(1)
        payload.extend(frame_bytes)
        await self.room.local_participant.publish_data(
            bytes(payload),
            reliable=True,
            destination_identities=[self.active_participant_identity],
            topic=self.settings.livekit_control_topic,
        )


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
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
