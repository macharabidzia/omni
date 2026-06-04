class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.active = false;

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data.type === "enqueue" && data.samples) {
        this.queue.push(new Float32Array(data.samples));
        this.port.postMessage({
          type: "queue",
          queuedFrames: this.pendingFrames(),
          queuedMs: this.pendingMs(),
        });
      } else if (data.type === "clear") {
        this.queue = [];
        this.offset = 0;
        this.active = false;
        this.port.postMessage({ type: "clear" });
      }
    };
  }

  pendingFrames() {
    if (this.queue.length === 0) {
      return 0;
    }
    let frames = 0;
    for (let index = 0; index < this.queue.length; index += 1) {
      const chunk = this.queue[index];
      if (index === 0) {
        frames += Math.max(0, chunk.length - this.offset);
      } else {
        frames += chunk.length;
      }
    }
    return frames;
  }

  pendingMs() {
    return Math.round((this.pendingFrames() / sampleRate) * 1000);
  }

  process(_inputs, outputs) {
    const output = outputs[0];
    const left = output[0];
    const right = output[1] ?? left;

    left.fill(0);
    right.fill(0);

    for (let frame = 0; frame < left.length; frame += 1) {
      if (this.queue.length === 0) {
        if (this.active) {
          this.active = false;
          this.port.postMessage({
            type: "drain",
            queuedFrames: 0,
            queuedMs: 0,
          });
        }
        break;
      }

      const current = this.queue[0];
      const sample = current[this.offset] ?? 0;
      left[frame] = sample;
      right[frame] = sample;
      this.offset += 1;

      if (!this.active) {
        this.active = true;
        this.port.postMessage({
          type: "started",
          queuedFrames: this.pendingFrames(),
          queuedMs: this.pendingMs(),
        });
      }

      if (this.offset >= current.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }

    return true;
  }
}

registerProcessor("playback-processor", PlaybackProcessor);
