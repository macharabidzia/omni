type PcmPlayerOptions = {
  onStarted: () => void;
  onDrained: () => void;
  onError: (message: string) => void;
  onDebugEvent?: (event: { type: string; [key: string]: unknown }) => void;
};

const DEFAULT_SAMPLE_RATE = 24000;
const PLAYOUT_LEAD_SECONDS = 0.01;
const MAX_DEBUGGED_CHUNKS_PER_TURN = 1;

export class PcmPlayer {
  private audioContext: AudioContext | null = null;
  private captureDestination: MediaStreamAudioDestinationNode | null = null;
  private nextStartTime = 0;
  private activeSources = new Set<AudioBufferSourceNode>();
  private drainTimer: number | null = null;
  private started = false;
  private finalized = false;
  private drainingNotified = false;
  private debugChunkCount = 0;

  constructor(private readonly options: PcmPlayerOptions) {}

  async prepare(): Promise<void> {
    if (!this.audioContext || this.audioContext.state === "closed") {
      this.audioContext = new AudioContext({ latencyHint: "interactive" });
      this.captureDestination = this.audioContext.createMediaStreamDestination();
      this.nextStartTime = 0;
    }
    try {
      await this.audioContext.resume();
    } catch {
      this.options.onError("Browser blocked assistant audio playback.");
    }
  }

  async enqueueBase64(params: {
    audioBase64: string;
    sampleRate?: number | null;
    channels?: number | null;
  }): Promise<void> {
    await this.prepare();
    if (!this.audioContext) {
      return;
    }

    const sampleRate = params.sampleRate ?? DEFAULT_SAMPLE_RATE;
    const channels = params.channels ?? 1;
    const interleaved = decodePcm16(params.audioBase64);
    this.enqueueInterleaved({
      interleaved,
      sampleRate,
      channels,
    });
  }

  async enqueuePcmBytes(params: {
    pcmBytes: Uint8Array;
    sampleRate?: number | null;
    channels?: number | null;
  }): Promise<void> {
    await this.prepare();
    if (!this.audioContext) {
      return;
    }

    const sampleRate = params.sampleRate ?? DEFAULT_SAMPLE_RATE;
    const channels = params.channels ?? 1;
    const interleaved = decodePcm16Bytes(params.pcmBytes);
    this.enqueueInterleaved({
      interleaved,
      sampleRate,
      channels,
    });
  }

  private enqueueInterleaved(params: {
    interleaved: Int16Array;
    sampleRate: number;
    channels: number;
  }): void {
    if (!this.audioContext) {
      return;
    }

    const { interleaved, sampleRate, channels } = params;
    if (interleaved.length === 0) {
      return;
    }
    const frameCount = Math.floor(interleaved.length / channels);
    if (frameCount === 0) {
      return;
    }

    const audioBuffer = this.audioContext.createBuffer(channels, frameCount, sampleRate);
    for (let channel = 0; channel < channels; channel += 1) {
      const channelData = audioBuffer.getChannelData(channel);
      for (let frame = 0; frame < frameCount; frame += 1) {
        const sample = interleaved[frame * channels + channel] ?? 0;
        channelData[frame] = sample / 32768;
      }
    }

    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.audioContext.destination);
    if (this.captureDestination) {
      source.connect(this.captureDestination);
    }

    const now = this.audioContext.currentTime;
    const startTime = Math.max(this.nextStartTime, now + PLAYOUT_LEAD_SECONDS);
    this.nextStartTime = startTime + audioBuffer.duration;
    this.clearDrainTimer();

    if (this.debugChunkCount < MAX_DEBUGGED_CHUNKS_PER_TURN) {
      this.options.onDebugEvent?.({
        type: "local.audio.chunk.queued",
        sample_rate: sampleRate,
        channels,
        frames: frameCount,
        start_time_s: round(startTime),
        duration_ms: round(audioBuffer.duration * 1000),
        queue_ahead_ms: round(Math.max(startTime - now, 0) * 1000),
      });
      this.debugChunkCount += 1;
    }

    source.onended = () => {
      this.activeSources.delete(source);
      if (this.finalized && this.activeSources.size === 0) {
        this.notifyDrained();
      }
    };
    this.activeSources.add(source);
    source.start(startTime);

    if (!this.started) {
      this.started = true;
      this.drainingNotified = false;
      const delayMs = Math.max((startTime - now) * 1000, 0);
      window.setTimeout(() => {
        if (!this.started) {
          return;
        }
        this.options.onStarted();
      }, delayMs);
    }
  }

  finalize(): void {
    this.finalized = true;
    if (!this.audioContext || this.activeSources.size === 0) {
      this.notifyDrained();
      return;
    }
    const remainingMs = Math.max((this.nextStartTime - this.audioContext.currentTime) * 1000, 0);
    this.clearDrainTimer();
    this.drainTimer = window.setTimeout(() => {
      this.drainTimer = null;
      if (this.finalized && this.activeSources.size === 0) {
        this.notifyDrained();
      }
    }, remainingMs + 20);
  }

  reset(): void {
    this.finalized = false;
    this.clearDrainTimer();
    this.debugChunkCount = 0;
    for (const source of this.activeSources) {
      try {
        source.stop();
      } catch {
        // Ignore nodes that have already ended.
      }
      source.disconnect();
    }
    this.activeSources.clear();
    if (this.audioContext) {
      this.nextStartTime = this.audioContext.currentTime;
    } else {
      this.nextStartTime = 0;
    }
    if (this.started && !this.drainingNotified) {
      this.notifyDrained();
    } else {
      this.started = false;
      this.drainingNotified = false;
    }
  }

  async close(): Promise<void> {
    this.reset();
    if (this.audioContext) {
      await this.audioContext.close();
      this.audioContext = null;
    }
    this.captureDestination = null;
  }

  captureStream(): MediaStream | null {
    return this.captureDestination?.stream ?? null;
  }

  private clearDrainTimer(): void {
    if (this.drainTimer !== null) {
      window.clearTimeout(this.drainTimer);
      this.drainTimer = null;
    }
  }

  private notifyDrained(): void {
    if (!this.started || this.drainingNotified) {
      this.started = false;
      return;
    }
    this.started = false;
    this.drainingNotified = true;
    this.options.onDrained();
  }
}

function decodePcm16(audioBase64: string): Int16Array {
  const binary = atob(audioBase64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return decodePcm16Bytes(bytes);
}

function decodePcm16Bytes(bytes: Uint8Array): Int16Array {
  return new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}
