import { encodeAudioBytesBase64 } from "./pcm";

type CaptureCallbacks = {
  onChunk: (chunk: { audioBase64: string; level: number }) => void;
  onStarted: () => void;
};

type CaptureProcessorEvent =
  | {
      type: "chunk";
      audioBytes: ArrayBuffer;
    }
  | {
      type: "flush-complete";
      flushId: number;
    };

export class CaptureWorkletController {
  private audioContext: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private mediaStream: MediaStream | null = null;
  private mediaSource: MediaStreamAudioSourceNode | null = null;
  private sinkNode: GainNode | null = null;
  private transmitting = false;
  private nextFlushId = 0;
  private pendingFlushes = new Map<number, () => void>();
  private readonly flushTimeoutMs = 1000;

  constructor(
    private readonly targetSampleRate: number,
    private readonly chunkMs: number,
    private readonly callbacks: CaptureCallbacks,
  ) {}

  async start(): Promise<void> {
    if (this.audioContext) {
      return;
    }

    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });

    this.audioContext = new AudioContext();
    await this.audioContext.audioWorklet.addModule("/worklets/capture-processor.js");
    this.workletNode = new AudioWorkletNode(this.audioContext, "capture-processor", {
      processorOptions: {
        targetSampleRate: this.targetSampleRate,
        chunkFrames: Math.round((this.targetSampleRate * this.chunkMs) / 1000),
      },
    });
    this.workletNode.port.onmessage = (event: MessageEvent<CaptureProcessorEvent>) => {
      if (event.data.type === "chunk") {
        this.callbacks.onChunk({
          audioBase64: encodeAudioBytesBase64(event.data.audioBytes),
          level: measurePcm16Level(event.data.audioBytes),
        });
        return;
      }
      if (event.data.type !== "flush-complete") {
        return;
      }
      const resolve = this.pendingFlushes.get(event.data.flushId);
      if (!resolve) {
        return;
      }
      this.pendingFlushes.delete(event.data.flushId);
      resolve();
    };

    this.mediaSource = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.sinkNode = this.audioContext.createGain();
    this.sinkNode.gain.value = 0;
    this.mediaSource.connect(this.workletNode);
    this.workletNode.connect(this.sinkNode);
    this.sinkNode.connect(this.audioContext.destination);
    this.workletNode.port.postMessage({ type: "set-transmitting", active: this.transmitting });
    await this.audioContext.resume();
    this.callbacks.onStarted();
  }

  setTransmitting(active: boolean): void {
    this.transmitting = active;
    this.workletNode?.port.postMessage({ type: "set-transmitting", active });
  }

  async flushAndPause(): Promise<void> {
    this.transmitting = false;
    if (!this.workletNode) {
      return;
    }

    const flushId = ++this.nextFlushId;
    const flushed = new Promise<void>((resolve) => {
      this.pendingFlushes.set(flushId, resolve);
    });

    this.workletNode.port.postMessage({ type: "flush-and-stop", flushId });
    try {
      await Promise.race([
        flushed,
        new Promise<never>((_, reject) => {
          window.setTimeout(() => {
            if (!this.pendingFlushes.delete(flushId)) {
              return;
            }
            reject(new Error("Timed out waiting for microphone flush."));
          }, this.flushTimeoutMs);
        }),
      ]);
    } finally {
      this.pendingFlushes.delete(flushId);
    }
  }

  async stop(): Promise<void> {
    this.transmitting = false;
    this.workletNode?.port.postMessage({ type: "set-transmitting", active: false });
    this.workletNode?.disconnect();
    this.mediaSource?.disconnect();
    this.sinkNode?.disconnect();
    this.mediaStream?.getTracks().forEach((track) => track.stop());
    if (this.audioContext) {
      await this.audioContext.close();
    }
    this.audioContext = null;
    this.workletNode = null;
    this.mediaSource = null;
    this.mediaStream = null;
    this.sinkNode = null;
    this.pendingFlushes.clear();
  }
}

function measurePcm16Level(audioBytes: ArrayBuffer): number {
  const samples = new Int16Array(audioBytes);
  if (samples.length === 0) {
    return 0;
  }

  let sumSquares = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const normalized = samples[index] / 0x8000;
    sumSquares += normalized * normalized;
  }

  return Math.sqrt(sumSquares / samples.length);
}
