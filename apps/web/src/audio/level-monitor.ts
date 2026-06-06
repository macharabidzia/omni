type AudioLevelMonitorOptions = {
  threshold: number;
  drainMs: number;
  fftSize?: number;
  pollMs?: number;
  onLevel?: (level: number) => void;
  onStarted?: () => void;
  onDrained?: () => void;
  onPlaybackAttached?: (details: { readyState: number; paused: boolean }) => void;
  onPlaybackElementPlaying?: () => void;
  onPlaybackBlocked?: () => void;
};

export class AudioLevelMonitor {
  private audioContext: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private sampleBuffer: Float32Array<ArrayBuffer> | null = null;
  private pollTimer: number | null = null;
  private lastActiveAt = 0;
  private active = false;
  private audioElement: HTMLAudioElement | null = null;

  constructor(private readonly options: AudioLevelMonitorOptions) {}

  async attachStream(stream: MediaStream, { playback = false }: { playback?: boolean } = {}): Promise<void> {
    this.detach();

    this.audioContext = new AudioContext();
    this.analyser = this.audioContext.createAnalyser();
    this.analyser.fftSize = this.options.fftSize ?? 2048;
    this.sourceNode = this.audioContext.createMediaStreamSource(stream);
    this.sourceNode.connect(this.analyser);
    this.sampleBuffer = new Float32Array(
      new ArrayBuffer(this.analyser.fftSize * Float32Array.BYTES_PER_ELEMENT),
    );

    if (playback) {
      this.audioElement = document.createElement("audio");
      this.audioElement.autoplay = true;
      this.audioElement.setAttribute("playsinline", "true");
      this.audioElement.addEventListener("loadedmetadata", () => {
        this.options.onPlaybackAttached?.({
          readyState: this.audioElement?.readyState ?? 0,
          paused: this.audioElement?.paused ?? true,
        });
      });
      this.audioElement.addEventListener("playing", () => {
        this.options.onPlaybackElementPlaying?.();
      });
      this.audioElement.srcObject = stream;
      this.audioElement.style.display = "none";
      document.body.appendChild(this.audioElement);
      try {
        await this.audioElement.play();
      } catch {
        this.options.onPlaybackBlocked?.();
      }
    }

    await this.audioContext.resume();
    this.startPolling();
  }

  detach(): void {
    if (this.pollTimer !== null) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.sourceNode?.disconnect();
    this.analyser?.disconnect();
    if (this.audioElement) {
      this.audioElement.pause();
      this.audioElement.srcObject = null;
      this.audioElement.remove();
      this.audioElement = null;
    }
    if (this.audioContext) {
      void this.audioContext.close();
    }
    this.audioContext = null;
    this.analyser = null;
    this.sourceNode = null;
    this.sampleBuffer = null;
    if (this.active) {
      this.active = false;
      this.options.onDrained?.();
    }
    this.lastActiveAt = 0;
  }

  rearm(): void {
    this.lastActiveAt = 0;
    this.active = false;
  }

  private startPolling(): void {
    const pollMs = this.options.pollMs ?? 20;
    this.pollTimer = window.setInterval(() => {
      if (!this.analyser || !this.sampleBuffer) {
        return;
      }
      this.analyser.getFloatTimeDomainData(this.sampleBuffer);
      const level = computeRmsLevel(this.sampleBuffer);
      this.options.onLevel?.(level);

      const now = performance.now();
      if (level >= this.options.threshold) {
        this.lastActiveAt = now;
        if (!this.active) {
          this.active = true;
          this.options.onStarted?.();
        }
        return;
      }

      if (this.active && now - this.lastActiveAt >= this.options.drainMs) {
        this.active = false;
        this.options.onDrained?.();
      }
    }, pollMs);
  }
}

function computeRmsLevel(samples: ArrayLike<number>): number {
  if (samples.length === 0) {
    return 0;
  }

  let sumSquares = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const value = samples[index] ?? 0;
    sumSquares += value * value;
  }
  return Math.sqrt(sumSquares / samples.length);
}
