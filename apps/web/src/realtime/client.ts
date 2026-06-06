import { Room, RoomEvent, Track, type RemoteTrack } from "livekit-client";

import { AudioLevelMonitor } from "../audio/level-monitor";
import { PcmPlayer } from "../audio/pcm-player";
import type { GatewayInboundEvent, Speaker } from "./events";

type RealtimeClientOptions = {
  baseUrl: string;
  onEvent: (event: GatewayInboundEvent) => void;
  onClose: () => void;
  onError: (message: string) => void;
  onDebugEvent: (event: { type: string; [key: string]: unknown }) => void;
  onLocalAudioLevel: (level: number) => void;
  onAssistantPlaybackStarted: () => void;
  onAssistantPlaybackDrained: () => void;
};

type LiveKitSessionResponse = {
  livekit_url: string;
  room: string;
  participant_identity: string;
  participant_token: string;
  control_topic: string;
};

type AssistantCaptureWindow = Window & {
  __assistantCaptureStream?: MediaStream | null;
};

const AUDIO_PACKET_TYPE_PCM16 = 1;
const AUDIO_PACKET_HEADER_BYTES = 6;
const ASSISTANT_TRACK_NAME = "assistant";
const ASSISTANT_TRACK_ACTIVITY_THRESHOLD = 0.002;
const ASSISTANT_TRACK_DRAIN_MS = 120;
const ASSISTANT_TRACK_PLAYOUT_DELAY_MS = resolveNumberQueryParam("playoutDelayMs", 90);
const LIVEKIT_WEB_AUDIO_MIX = resolveBooleanQueryParam("webAudioMix", true);

export class RealtimeClient {
  private room: Room | null = null;
  private controlTopic = "omni.control";
  private microphoneStream: MediaStream | null = null;
  private assistantAudioTransport: "unknown" | "track" | "packet" | "delta" = "unknown";
  private assistantRemoteTrack: RemoteTrack | null = null;
  private assistantTrackSid: string | null = null;
  private assistantTrackStream: MediaStream | null = null;
  private assistantTrackPlaybackActive = false;
  private assistantTrackFinalizePending = false;
  private assistantTrackLastActiveAt = 0;
  private assistantTrackSuppressUntil = 0;
  private localMonitor = new AudioLevelMonitor({
    threshold: 0,
    drainMs: 0,
    fftSize: 512,
    pollMs: 10,
    onLevel: (level) => this.options.onLocalAudioLevel(level),
  });
  private assistantTrackMonitor = new AudioLevelMonitor({
    threshold: 0,
    drainMs: 0,
    fftSize: 512,
    pollMs: 10,
    onLevel: (level) => this.handleAssistantTrackLevel(level),
    onPlaybackAttached: ({ readyState, paused }) =>
      this.options.onDebugEvent({
        type: "local.livekit.track_playback_attached",
        ready_state: readyState,
        paused,
      }),
    onPlaybackElementPlaying: () =>
      this.options.onDebugEvent({
        type: "local.livekit.track_playback_started",
        perf_now_ms: round(performance.now()),
      }),
    onPlaybackBlocked: () => {
      this.options.onDebugEvent({
        type: "local.livekit.track_playback_blocked",
      });
      this.options.onError("Browser blocked LiveKit assistant audio playback.");
    },
  });
  private assistantPlayer = new PcmPlayer({
    onStarted: () => this.options.onAssistantPlaybackStarted(),
    onDrained: () => this.options.onAssistantPlaybackDrained(),
    onError: (message) => this.options.onError(message),
    onDebugEvent: (event) => this.options.onDebugEvent(event),
  });
  private readonly encoder = new TextEncoder();
  private readonly decoder = new TextDecoder();
  private audioPacketDebugCount = 0;
  private controlEventSequence = 0;

  constructor(private readonly options: RealtimeClientOptions) {}

  async connect(speaker: Speaker): Promise<void> {
    await this.assistantPlayer.prepare();
    this.syncAssistantCaptureStream();

    const session = await this.fetchSession();
    this.controlTopic = session.control_topic;
    const livekitUrl = this.resolveLivekitUrl(session.livekit_url);

    const room = new Room({
      adaptiveStream: false,
      dynacast: false,
      stopLocalTrackOnUnpublish: false,
      webAudioMix: LIVEKIT_WEB_AUDIO_MIX,
    });
    this.room = room;

    room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
      if (topic !== this.controlTopic) {
        return;
      }
      const payloadBytes = normalizeLivekitPayload(payload);
      if (this.handleAudioPacket(payloadBytes)) {
        return;
      }
      try {
        const event = JSON.parse(this.decoder.decode(payloadBytes)) as GatewayInboundEvent;
        this.options.onEvent(event);
      } catch {
        this.options.onError("Received invalid LiveKit control payload.");
      }
    });

    room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
      this.options.onDebugEvent({
        type: "local.livekit.track_subscribed",
        participant_identity: participant.identity,
        participant_sid: participant.sid,
        track_sid: publication.trackSid,
        track_name: publication.trackName,
        track_source: publication.source,
        kind: track.kind,
      });
      void this.handleTrackSubscribed(track, publication.trackSid, publication.trackName);
    });

    room.on(RoomEvent.TrackUnsubscribed, (track, publication, participant) => {
      this.options.onDebugEvent({
        type: "local.livekit.track_unsubscribed",
        participant_identity: participant.identity,
        participant_sid: participant.sid,
        track_sid: publication.trackSid,
        track_name: publication.trackName,
        track_source: publication.source,
        kind: track.kind,
      });
      this.handleTrackUnsubscribed(track, publication.trackSid);
    });

    room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
      this.options.onDebugEvent({
        type: "local.livekit.audio_playback_status",
        can_playback_audio: room.canPlaybackAudio,
      });
    });

    room.on(RoomEvent.Disconnected, () => {
      this.options.onClose();
    });

    this.options.onDebugEvent({
      type: "local.livekit.connect_target",
      requested_url: session.livekit_url,
      resolved_url: livekitUrl,
    });
    await room.connect(livekitUrl, session.participant_token);
    this.options.onDebugEvent({
      type: "local.livekit.connected",
      room_name: session.room,
      participant_identity: session.participant_identity,
      remote_participant_count: room.remoteParticipants.size,
      can_playback_audio: room.canPlaybackAudio,
    });
    if (!room.canPlaybackAudio) {
      this.options.onDebugEvent({
        type: "local.livekit.audio_playback_start_requested",
      });
      await room.startAudio();
      this.options.onDebugEvent({
        type: "local.livekit.audio_playback_start_resolved",
        can_playback_audio: room.canPlaybackAudio,
      });
    }

    await this.ensureMicrophoneStream();

    const microphoneTrack = this.microphoneStream?.getAudioTracks()[0];
    if (!microphoneTrack) {
      throw new Error("Microphone track was not available.");
    }

    await room.localParticipant.publishTrack(microphoneTrack, {
      source: Track.Source.Microphone,
      stopMicTrackOnMute: false,
    });
    await this.syncExistingRemoteAudioTracks();

    await this.send({
      type: "session.start",
      speaker,
      modalities: ["text", "audio"],
      debug_audio_deltas: this.isTransportDebugEnabled(),
    });
  }

  async send(event: Record<string, unknown>): Promise<void> {
    if (!this.room) {
      throw new Error("Realtime session is not connected.");
    }
    const reliable = this.isReliableControlEvent(event);
    const debugSequence = ++this.controlEventSequence;
    const debugEventType = typeof event.type === "string" ? event.type : "unknown";
    const payload = this.isTransportDebugEnabled()
      ? {
          ...event,
          _client_debug_sequence: debugSequence,
          _client_sent_at_epoch_ms: Date.now(),
        }
      : event;
    this.options.onDebugEvent({
      type: "local.control.send.started",
      event_type: debugEventType,
      reliable,
      sequence: debugSequence,
      perf_now_ms: round(performance.now()),
    });
    const startedAt = performance.now();
    await this.room.localParticipant.publishData(this.encoder.encode(JSON.stringify(payload)), {
      reliable,
      topic: this.controlTopic,
    });
    this.options.onDebugEvent({
      type: "local.control.send.resolved",
      event_type: debugEventType,
      reliable,
      sequence: debugSequence,
      perf_now_ms: round(performance.now()),
      duration_ms: round(performance.now() - startedAt),
    });
  }

  async enqueueAssistantAudio(event: {
    audio_base64: string;
    sample_rate?: number | null;
    channels?: number | null;
  }): Promise<void> {
    if (this.assistantAudioTransport === "track" || this.assistantAudioTransport === "packet") {
      return;
    }
    this.assistantAudioTransport = "delta";
    await this.assistantPlayer.enqueueBase64({
      audioBase64: event.audio_base64,
      sampleRate: event.sample_rate,
      channels: event.channels,
    });
  }

  finalizeAssistantAudio(): void {
    if (this.assistantAudioTransport === "track") {
      this.assistantTrackFinalizePending = true;
      return;
    }
    this.assistantPlayer.finalize();
  }

  interruptAssistantAudio(): void {
    this.audioPacketDebugCount = 0;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = performance.now() + ASSISTANT_TRACK_DRAIN_MS;
    this.assistantTrackMonitor.rearm();
    if (this.assistantAudioTransport === "track") {
      if (this.assistantTrackPlaybackActive) {
        this.assistantTrackPlaybackActive = false;
        this.options.onAssistantPlaybackDrained();
      }
    } else {
      this.assistantAudioTransport = "unknown";
    }
    this.assistantPlayer.reset();
  }

  resetAssistantPlaybackMonitor(): void {
    this.audioPacketDebugCount = 0;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackPlaybackActive = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.assistantTrackMonitor.rearm();
    if (this.assistantTrackStream === null) {
      this.assistantAudioTransport = "unknown";
    }
    this.assistantPlayer.reset();
  }

  async close(): Promise<void> {
    this.audioPacketDebugCount = 0;
    this.assistantAudioTransport = "unknown";
    this.assistantRemoteTrack = null;
    this.assistantTrackSid = null;
    this.assistantTrackStream = null;
    this.assistantTrackPlaybackActive = false;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.clearAssistantCaptureStream();
    await this.assistantPlayer.close();
    this.assistantTrackMonitor.detach();
    this.localMonitor.detach();
    this.microphoneStream?.getTracks().forEach((track) => track.stop());
    this.microphoneStream = null;
    await this.room?.disconnect();
    this.room = null;
  }

  private async fetchSession(): Promise<LiveKitSessionResponse> {
    const response = await fetch(`${this.options.baseUrl}/livekit/session`, {
      method: "POST",
    });
    if (!response.ok) {
      throw new Error(`Failed to create LiveKit session (${response.status}).`);
    }
    return (await response.json()) as LiveKitSessionResponse;
  }

  private resolveLivekitUrl(livekitUrl: string): string {
    const pageIsSecure = window.location.protocol === "https:";
    const livekitIsInsecure =
      livekitUrl.startsWith("ws://") || livekitUrl.startsWith("http://");
    if (pageIsSecure && livekitIsInsecure) {
      return new URL("/livekit-proxy", window.location.origin).toString();
    }
    return livekitUrl;
  }

  private async ensureMicrophoneStream(): Promise<void> {
    if (this.microphoneStream) {
      return;
    }
    this.microphoneStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
    await this.localMonitor.attachStream(this.microphoneStream);
  }

  private async handleTrackSubscribed(
    track: RemoteTrack,
    trackSid?: string,
    trackName?: string,
  ): Promise<void> {
    if (track.kind !== Track.Kind.Audio) {
      return;
    }
    if ((trackName ?? "") !== ASSISTANT_TRACK_NAME) {
      this.options.onDebugEvent({
        type: "local.livekit.track_ignored",
        track_sid: trackSid ?? track.sid ?? null,
        track_name: trackName ?? "",
        kind: track.kind,
      });
      return;
    }

    const resolvedTrackSid = trackSid ?? track.sid ?? null;
    if (resolvedTrackSid !== null && this.assistantTrackSid === resolvedTrackSid) {
      return;
    }

    if (ASSISTANT_TRACK_PLAYOUT_DELAY_MS >= 0) {
      const requestedPlayoutDelaySeconds = ASSISTANT_TRACK_PLAYOUT_DELAY_MS / 1000;
      track.setPlayoutDelay(requestedPlayoutDelaySeconds);
      this.options.onDebugEvent({
        type: "local.livekit.track_playout_delay_configured",
        track_sid: resolvedTrackSid,
        requested_delay_ms: ASSISTANT_TRACK_PLAYOUT_DELAY_MS,
        effective_delay_ms: round(track.getPlayoutDelay() * 1000),
      });
    }

    const stream = track.mediaStream ?? new MediaStream([track.mediaStreamTrack]);
    this.assistantRemoteTrack = track;
    this.assistantTrackSid = resolvedTrackSid;
    this.assistantTrackStream = stream;
    this.assistantAudioTransport = "track";
    this.assistantTrackPlaybackActive = false;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.assistantPlayer.reset();
    this.syncAssistantCaptureStream();
    await this.assistantTrackMonitor.attachStream(stream, { playback: true });
    this.syncAssistantCaptureStream();
    this.options.onDebugEvent({
      type: "local.livekit.track_playback_enabled",
      track_sid: resolvedTrackSid,
      kind: track.kind,
      audio_track_count: stream.getAudioTracks().length,
      perf_now_ms: round(performance.now()),
    });
    void this.emitAssistantTrackStats("subscribed");
  }

  private handleTrackUnsubscribed(track: RemoteTrack, trackSid?: string): void {
    if (track.kind !== Track.Kind.Audio) {
      return;
    }

    const resolvedTrackSid = trackSid ?? track.sid ?? null;
    if (this.assistantTrackSid !== null && resolvedTrackSid !== this.assistantTrackSid) {
      return;
    }

    if (this.assistantTrackPlaybackActive) {
      this.assistantTrackPlaybackActive = false;
      this.options.onAssistantPlaybackDrained();
    }
    this.assistantTrackSid = null;
    this.assistantTrackStream = null;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.assistantTrackMonitor.detach();
    if (this.assistantAudioTransport === "track") {
      this.assistantAudioTransport = "unknown";
    }
    this.syncAssistantCaptureStream();
  }

  private async syncExistingRemoteAudioTracks(): Promise<void> {
    if (!this.room) {
      return;
    }
    let audioTrackCount = 0;
    for (const participant of this.room.remoteParticipants.values()) {
      for (const publication of participant.trackPublications.values()) {
        const track = publication.track;
        if (!track || track.kind !== Track.Kind.Audio) {
          continue;
        }
        audioTrackCount += 1;
        this.options.onDebugEvent({
          type: "local.livekit.track_sync_found",
          participant_identity: participant.identity,
          participant_sid: participant.sid,
          track_sid: publication.trackSid,
          track_name: publication.trackName,
          track_source: publication.source,
        });
        await this.handleTrackSubscribed(track, publication.trackSid, publication.trackName);
        if (this.assistantTrackSid !== null) {
          return;
        }
      }
    }
    this.options.onDebugEvent({
      type: "local.livekit.track_sync_none",
      remote_participant_count: this.room.remoteParticipants.size,
      audio_track_count: audioTrackCount,
    });
  }

  private syncAssistantCaptureStream(): void {
    const captureWindow = window as AssistantCaptureWindow;
    captureWindow.__assistantCaptureStream =
      this.assistantTrackMonitor.captureStream() ??
      this.assistantTrackStream ??
      this.assistantPlayer.captureStream();
  }

  private clearAssistantCaptureStream(): void {
    const captureWindow = window as AssistantCaptureWindow;
    captureWindow.__assistantCaptureStream = null;
  }

  private handleAudioPacket(payload: Uint8Array): boolean {
    if (payload.byteLength < AUDIO_PACKET_HEADER_BYTES) {
      return false;
    }
    if (payload[0] !== AUDIO_PACKET_TYPE_PCM16) {
      return false;
    }
    if (this.assistantAudioTransport === "track") {
      return true;
    }
    if (this.assistantAudioTransport === "delta") {
      return true;
    }
    this.assistantAudioTransport = "packet";

    const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
    const sampleRate = view.getUint32(1);
    const channels = view.getUint8(5);
    const pcmBytes = payload.subarray(AUDIO_PACKET_HEADER_BYTES);
    if (this.audioPacketDebugCount === 0) {
      this.options.onDebugEvent({
        type: "assistant.audio.packet",
        perf_now_ms: Math.round(performance.now() * 100) / 100,
        sample_rate: sampleRate,
        channels,
        bytes: pcmBytes.byteLength,
      });
    }
    this.audioPacketDebugCount += 1;
    void this.assistantPlayer.enqueuePcmBytes({
      pcmBytes,
      sampleRate,
      channels,
    });
    return true;
  }

  private handleAssistantTrackLevel(level: number): void {
    if (this.assistantAudioTransport !== "track") {
      return;
    }

    const now = performance.now();
    if (now < this.assistantTrackSuppressUntil) {
      return;
    }

    if (level >= ASSISTANT_TRACK_ACTIVITY_THRESHOLD) {
      this.assistantTrackLastActiveAt = now;
      if (!this.assistantTrackPlaybackActive) {
        this.assistantTrackPlaybackActive = true;
        this.options.onDebugEvent({
          type: "local.livekit.track_audio_active",
          level: round(level),
        });
        this.options.onAssistantPlaybackStarted();
      }
      return;
    }

    if (
      !this.assistantTrackPlaybackActive ||
      !this.assistantTrackFinalizePending ||
      now - this.assistantTrackLastActiveAt < ASSISTANT_TRACK_DRAIN_MS
    ) {
      return;
    }

    this.assistantTrackPlaybackActive = false;
    this.assistantTrackFinalizePending = false;
    this.options.onDebugEvent({
      type: "local.livekit.track_audio_drained",
      perf_now_ms: round(now),
      silence_ms: round(now - this.assistantTrackLastActiveAt),
    });
    this.options.onAssistantPlaybackDrained();
  }

  private isReliableControlEvent(_event: Record<string, unknown>): boolean {
    const override = new URLSearchParams(window.location.search).get("controlReliable");
    if (override === "0" || override === "false") {
      return false;
    }
    return true;
  }

  private isTransportDebugEnabled(): boolean {
    const override = new URLSearchParams(window.location.search).get("transportDebug");
    return override === "1" || override === "true";
  }
}

function normalizeLivekitPayload(payload: Uint8Array | ArrayBuffer): Uint8Array {
  if (payload instanceof Uint8Array) {
    return payload;
  }
  return new Uint8Array(payload);
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

function resolveNumberQueryParam(name: string, fallback: number): number {
  const raw = new URLSearchParams(window.location.search).get(name);
  if (raw === null || raw.trim() === "") {
    return fallback;
  }
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

function resolveBooleanQueryParam(name: string, fallback: boolean): boolean {
  const raw = new URLSearchParams(window.location.search).get(name);
  if (raw === null || raw.trim() === "") {
    return fallback;
  }
  const normalized = raw.trim().toLowerCase();
  if (normalized === "1" || normalized === "true") {
    return true;
  }
  if (normalized === "0" || normalized === "false") {
    return false;
  }
  return fallback;
}
