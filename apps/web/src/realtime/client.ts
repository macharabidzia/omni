import { Room, RoomEvent, Track, type RemoteTrack } from "livekit-client";

import { AudioLevelMonitor } from "../audio/level-monitor";
import type { GatewayInboundEvent, Speaker } from "./events";

type RealtimeClientOptions = {
  baseUrl: string;
  onEvent: (event: GatewayInboundEvent) => void;
  onClose: () => void;
  onError: (message: string) => void;
  onDebugEvent: (event: { type: string; [key: string]: unknown }) => void;
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

const ASSISTANT_TRACK_NAME = "assistant";
const ASSISTANT_TRACK_ACTIVITY_THRESHOLD = 0.002;
const ASSISTANT_TRACK_DRAIN_MS = 120;
const ASSISTANT_TRACK_PLAYOUT_DELAY_MS = resolveNumberQueryParam("playoutDelayMs", 90);
const LIVEKIT_WEB_AUDIO_MIX = resolveBooleanQueryParam("webAudioMix", true);

export class RealtimeClient {
  private room: Room | null = null;
  private sessionStarted = false;
  private controlTopic = "omni.control";
  private microphoneStream: MediaStream | null = null;
  private assistantRemoteTrack: RemoteTrack | null = null;
  private assistantTrackSid: string | null = null;
  private assistantTrackStream: MediaStream | null = null;
  private assistantTrackStatsIntervalId: number | null = null;
  private assistantTrackPlaybackActive = false;
  private assistantTrackFinalizePending = false;
  private assistantTrackLastActiveAt = 0;
  private assistantTrackSuppressUntil = 0;
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
  private readonly encoder = new TextEncoder();
  private readonly decoder = new TextDecoder();
  private controlEventSequence = 0;
  private pendingTelemetryEvents: Record<string, unknown>[] = [];

  constructor(private readonly options: RealtimeClientOptions) {}

  async connect(speaker: Speaker): Promise<void> {
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
    void this.sendTelemetry("livekit.connected", {
      can_playback_audio: room.canPlaybackAudio,
      remote_participant_count: room.remoteParticipants.size,
    }).catch(() => undefined);
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
    void this.sendTelemetry("microphone.published", {
      can_playback_audio: room.canPlaybackAudio,
    }).catch(() => undefined);
    await this.syncExistingRemoteAudioTracks();

    await this.send({
      type: "session.start",
      speaker,
      modalities: ["text", "audio"],
      debug_audio_metadata: this.isTransportDebugEnabled(),
    });
    this.sessionStarted = true;
    await this.flushPendingTelemetryEvents();
  }

  async send(event: Record<string, unknown>): Promise<void> {
    if (!this.room) {
      throw new Error("Realtime session is not connected.");
    }
    const reliable = this.isReliableControlEvent(event);
    await this.publishControlEvent(event, { reliable, includeDebugMetadata: this.isTransportDebugEnabled() });
  }

  async sendInterrupt(turnId: string | null): Promise<void> {
    await this.sendTelemetry("interrupt.clicked", {
      turn_id: turnId,
    });
    await this.send({
      type: "client.interrupt",
      turn_id: turnId,
    });
  }

  async reportAssistantPlaybackStarted(args: {
    turnId: string | null;
    commitToFirstAudioPlayedMs: number | null;
  }): Promise<void> {
    await this.sendTelemetry("assistant.playback.started", {
      turn_id: args.turnId,
      commit_to_first_audio_played_ms: args.commitToFirstAudioPlayedMs,
    });
  }

  private async sendTelemetry(
    eventName: string,
    payload: Record<string, unknown> = {},
  ): Promise<void> {
    if (!this.room) {
      return;
    }
    const telemetryEvent = {
      type: "client.telemetry",
      event_name: eventName,
      client_epoch_ms: Date.now(),
      client_perf_now_ms: round(performance.now()),
      ...payload,
    };
    if (!this.sessionStarted) {
      this.pendingTelemetryEvents.push(telemetryEvent);
      return;
    }
    await this.publishControlEvent(telemetryEvent, {
      reliable: false,
      includeDebugMetadata: false,
    });
  }

  private async publishControlEvent(
    event: Record<string, unknown>,
    options: {
      reliable: boolean;
      includeDebugMetadata: boolean;
    },
  ): Promise<void> {
    if (!this.room) {
      throw new Error("Realtime session is not connected.");
    }
    const debugSequence = ++this.controlEventSequence;
    const debugEventType = typeof event.type === "string" ? event.type : "unknown";
    const payload = options.includeDebugMetadata
      ? {
          ...event,
          _client_debug_sequence: debugSequence,
          _client_sent_at_epoch_ms: Date.now(),
        }
      : event;
    this.options.onDebugEvent({
      type: "local.control.send.started",
      event_type: debugEventType,
      reliable: options.reliable,
      sequence: debugSequence,
      perf_now_ms: round(performance.now()),
    });
    const startedAt = performance.now();
    await this.room.localParticipant.publishData(this.encoder.encode(JSON.stringify(payload)), {
      reliable: options.reliable,
      topic: this.controlTopic,
    });
    this.options.onDebugEvent({
      type: "local.control.send.resolved",
      event_type: debugEventType,
      reliable: options.reliable,
      sequence: debugSequence,
      perf_now_ms: round(performance.now()),
      duration_ms: round(performance.now() - startedAt),
    });
  }

  private async flushPendingTelemetryEvents(): Promise<void> {
    if (!this.room || !this.sessionStarted || this.pendingTelemetryEvents.length === 0) {
      return;
    }
    const pendingEvents = this.pendingTelemetryEvents;
    this.pendingTelemetryEvents = [];
    for (const event of pendingEvents) {
      await this.publishControlEvent(event, {
        reliable: false,
        includeDebugMetadata: false,
      });
    }
  }

  finalizeAssistantAudio(): void {
    if (this.assistantTrackStream !== null) {
      this.assistantTrackFinalizePending = true;
    }
  }

  interruptAssistantAudio(): void {
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = performance.now() + ASSISTANT_TRACK_DRAIN_MS;
    this.assistantTrackMonitor.rearm();
    if (this.assistantTrackPlaybackActive) {
      this.assistantTrackPlaybackActive = false;
      this.options.onAssistantPlaybackDrained();
    }
  }

  resetAssistantPlaybackMonitor(): void {
    this.assistantTrackFinalizePending = false;
    this.assistantTrackPlaybackActive = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.assistantTrackMonitor.rearm();
  }

  async close(): Promise<void> {
    this.sessionStarted = false;
    this.pendingTelemetryEvents = [];
    this.assistantRemoteTrack = null;
    this.assistantTrackSid = null;
    this.assistantTrackStream = null;
    this.assistantTrackPlaybackActive = false;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.stopAssistantTrackStatsSampler();
    this.clearAssistantCaptureStream();
    this.assistantTrackMonitor.detach();
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
    const microphoneTrack = this.microphoneStream.getAudioTracks()[0];
    if (microphoneTrack && this.isTransportDebugEnabled()) {
      const settings = microphoneTrack.getSettings();
      this.options.onDebugEvent({
        type: "local.capture.settings",
        channel_count: settings.channelCount ?? null,
        sample_rate: settings.sampleRate ?? null,
        sample_size: settings.sampleSize ?? null,
        echo_cancellation: settings.echoCancellation ?? null,
        noise_suppression: settings.noiseSuppression ?? null,
        auto_gain_control: settings.autoGainControl ?? null,
        device_id_present: Boolean(settings.deviceId),
      });
    }
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
    this.assistantTrackPlaybackActive = false;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.syncAssistantCaptureStream();
    await this.assistantTrackMonitor.attachStream(stream, { playback: true });
    this.syncAssistantCaptureStream();
    this.startAssistantTrackStatsSampler();
    void this.sendTelemetry("assistant.track_subscribed", {
      track_sid: resolvedTrackSid,
      playout_delay_ms: round(track.getPlayoutDelay() * 1000),
    }).catch(() => undefined);
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
    void this.emitAssistantTrackStats("unsubscribed");
    this.assistantRemoteTrack = null;
    this.assistantTrackSid = null;
    this.assistantTrackStream = null;
    this.assistantTrackFinalizePending = false;
    this.assistantTrackLastActiveAt = 0;
    this.assistantTrackSuppressUntil = 0;
    this.stopAssistantTrackStatsSampler();
    this.assistantTrackMonitor.detach();
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
      this.assistantTrackMonitor.captureStream() ?? this.assistantTrackStream;
  }

  private clearAssistantCaptureStream(): void {
    const captureWindow = window as AssistantCaptureWindow;
    captureWindow.__assistantCaptureStream = null;
  }

  private handleAssistantTrackLevel(level: number): void {
    if (this.assistantTrackStream === null) {
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
    void this.emitAssistantTrackStats("drained");
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

  private startAssistantTrackStatsSampler(): void {
    if (!this.isTransportDebugEnabled() || this.assistantRemoteTrack === null) {
      return;
    }
    if (this.assistantTrackStatsIntervalId !== null) {
      return;
    }
    this.assistantTrackStatsIntervalId = window.setInterval(() => {
      if (this.assistantRemoteTrack === null) {
        this.stopAssistantTrackStatsSampler();
        return;
      }
      void this.emitAssistantTrackStats("sample");
    }, 1000);
  }

  private stopAssistantTrackStatsSampler(): void {
    if (this.assistantTrackStatsIntervalId === null) {
      return;
    }
    window.clearInterval(this.assistantTrackStatsIntervalId);
    this.assistantTrackStatsIntervalId = null;
  }

  private async emitAssistantTrackStats(phase: string): Promise<void> {
    if (!this.isTransportDebugEnabled() || this.assistantRemoteTrack === null) {
      return;
    }

    try {
      const report = await this.assistantRemoteTrack.getRTCStatsReport();
      if (!report) {
        return;
      }
      this.options.onDebugEvent({
        type: "local.livekit.track_stats",
        phase,
        track_sid: this.assistantTrackSid,
        ...summarizeAudioReceiverStats(report),
      });
    } catch (error) {
      this.options.onDebugEvent({
        type: "local.livekit.track_stats_failed",
        phase,
        message: error instanceof Error ? error.message : String(error),
      });
    }
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

function summarizeAudioReceiverStats(report: RTCStatsReport): Record<string, unknown> {
  const summary: Record<string, unknown> = {};
  for (const stat of report.values()) {
    if (stat.type !== "inbound-rtp") {
      continue;
    }
    const mediaType =
      "kind" in stat && typeof stat.kind === "string"
        ? stat.kind
        : "mediaType" in stat && typeof stat.mediaType === "string"
          ? stat.mediaType
          : null;
    if (mediaType !== "audio") {
      continue;
    }
    copyIfFinite(summary, "packets_received", stat.packetsReceived);
    copyIfFinite(summary, "packets_lost", stat.packetsLost);
    copyIfFinite(summary, "jitter_s", stat.jitter);
    copyIfFinite(summary, "jitter_buffer_delay_s", stat.jitterBufferDelay);
    copyIfFinite(summary, "jitter_buffer_emitted_count", stat.jitterBufferEmittedCount);
    copyIfFinite(summary, "concealed_samples", stat.concealedSamples);
    copyIfFinite(summary, "silent_concealed_samples", stat.silentConcealedSamples);
    copyIfFinite(summary, "concealment_events", stat.concealmentEvents);
    copyIfFinite(summary, "inserted_samples_for_deceleration", stat.insertedSamplesForDeceleration);
    copyIfFinite(summary, "removed_samples_for_acceleration", stat.removedSamplesForAcceleration);
    copyIfFinite(summary, "total_samples_received", stat.totalSamplesReceived);
    copyIfFinite(summary, "total_samples_duration_s", stat.totalSamplesDuration);
    copyIfFinite(summary, "audio_level", stat.audioLevel);
    if (
      typeof stat.jitterBufferDelay === "number" &&
      Number.isFinite(stat.jitterBufferDelay) &&
      typeof stat.jitterBufferEmittedCount === "number" &&
      Number.isFinite(stat.jitterBufferEmittedCount) &&
      stat.jitterBufferEmittedCount > 0
    ) {
      summary.jitter_buffer_avg_ms =
        Math.round((stat.jitterBufferDelay / stat.jitterBufferEmittedCount) * 1000 * 100) / 100;
    }
    if (typeof stat.jitter === "number" && Number.isFinite(stat.jitter)) {
      summary.jitter_ms = Math.round(stat.jitter * 1000 * 100) / 100;
    }
    break;
  }
  return summary;
}

function copyIfFinite(target: Record<string, unknown>, key: string, value: unknown): void {
  if (typeof value === "number" && Number.isFinite(value)) {
    target[key] = value;
  }
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
