import { encodePcm16Base64 } from "./pcm";
import { resampleFloat32 } from "./resampler";

type CaptureCallbacks = {
  onChunk: (audioBase64: string) => void;
  onStarted: () => void;
};

export class CaptureWorkletController {
  private audioContext: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private mediaStream: MediaStream | null = null;
  private mediaSource: MediaStreamAudioSourceNode | null = null;
  private sinkNode: GainNode | null = null;
  private transmitting = false;
  private pendingSamples: number[] = [];

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
    this.workletNode = new AudioWorkletNode(this.audioContext, "capture-processor");
    this.workletNode.port.onmessage = (event: MessageEvent<ArrayBuffer | Float32Array>) => {
      const raw = event.data instanceof Float32Array ? event.data : new Float32Array(event.data);
      this.handleSamples(raw);
    };

    this.mediaSource = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.sinkNode = this.audioContext.createGain();
    this.sinkNode.gain.value = 0;
    this.mediaSource.connect(this.workletNode);
    this.workletNode.connect(this.sinkNode);
    this.sinkNode.connect(this.audioContext.destination);
    await this.audioContext.resume();
    this.callbacks.onStarted();
  }

  setTransmitting(active: boolean): void {
    this.transmitting = active;
    if (!active) {
      this.pendingSamples = [];
    }
  }

  async stop(): Promise<void> {
    this.pendingSamples = [];
    this.transmitting = false;
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
  }

  private handleSamples(input: Float32Array): void {
    if (!this.transmitting || !this.audioContext) {
      this.pendingSamples = [];
      return;
    }

    const resampled = resampleFloat32(input, this.audioContext.sampleRate, this.targetSampleRate);
    const chunkSampleCount = Math.round((this.targetSampleRate * this.chunkMs) / 1000);
    for (const sample of resampled) {
      this.pendingSamples.push(sample);
    }

    while (this.pendingSamples.length >= chunkSampleCount) {
      const chunk = new Float32Array(this.pendingSamples.splice(0, chunkSampleCount));
      this.callbacks.onChunk(encodePcm16Base64(chunk));
    }
  }
}
