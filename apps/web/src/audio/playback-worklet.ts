import { decodePcm16Base64, pcm16ToFloat32 } from "./pcm";
import { resampleFloat32 } from "./resampler";

type PlaybackCallbacks = {
  onStarted: () => void;
  onDrained: () => void;
  onUnderrun?: (state: { queuedFrames: number; queuedMs: number }) => void;
  onQueueChanged?: (state: { queuedFrames: number; queuedMs: number }) => void;
  onCleared?: () => void;
};

export class PlaybackWorkletController {
  private audioContext: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;

  constructor(private readonly callbacks: PlaybackCallbacks) {}

  async start(): Promise<void> {
    if (this.audioContext) {
      return;
    }

    this.audioContext = new AudioContext();
    await this.audioContext.audioWorklet.addModule("/worklets/playback-processor.js");
    this.workletNode = new AudioWorkletNode(this.audioContext, "playback-processor", {
      outputChannelCount: [2],
    });
    this.workletNode.port.onmessage = (
      event: MessageEvent<{ type: string; queuedFrames?: number; queuedMs?: number }>,
    ) => {
      if (event.data.type === "started") {
        this.callbacks.onStarted();
      }
      if (event.data.type === "drain") {
        this.callbacks.onDrained();
      }
      if (event.data.type === "underrun") {
        this.callbacks.onUnderrun?.({
          queuedFrames: event.data.queuedFrames ?? 0,
          queuedMs: event.data.queuedMs ?? 0,
        });
      }
      if (event.data.type === "queue" || event.data.type === "started" || event.data.type === "drain") {
        this.callbacks.onQueueChanged?.({
          queuedFrames: event.data.queuedFrames ?? 0,
          queuedMs: event.data.queuedMs ?? 0,
        });
      }
      if (event.data.type === "clear") {
        this.callbacks.onCleared?.();
      }
    };
    this.workletNode.connect(this.audioContext.destination);
    await this.audioContext.resume();
  }

  enqueue(audioBase64: string, sampleRate: number): void {
    if (!this.audioContext || !this.workletNode) {
      return;
    }
    const pcm16 = decodePcm16Base64(audioBase64);
    const float32 = pcm16ToFloat32(pcm16);
    const resampled = resampleFloat32(float32, sampleRate, this.audioContext.sampleRate);
    this.workletNode.port.postMessage(
      { type: "enqueue", samples: resampled },
      [resampled.buffer],
    );
  }

  clear(): void {
    this.workletNode?.port.postMessage({ type: "clear" });
  }

  async stop(): Promise<void> {
    this.clear();
    this.workletNode?.disconnect();
    if (this.audioContext) {
      await this.audioContext.close();
    }
    this.audioContext = null;
    this.workletNode = null;
  }
}
