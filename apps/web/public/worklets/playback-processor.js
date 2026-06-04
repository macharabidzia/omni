class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.active = false;
    this.startupMinChunks = 2;
    this.startupMinFrames = Math.round(sampleRate * 0.22);
    this.startupMaxWaitFrames = Math.round(sampleRate * 0.22);
    this.startupWaitFrames = 0;
    this.underrunFrames = 0;
    this.underrunReported = false;
    this.drainHoldFrames = Math.round(sampleRate * 0.25);

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data.type === "enqueue" && data.samples) {
        const wasEmpty = this.queue.length === 0;
        this.queue.push(new Float32Array(data.samples));
        if (wasEmpty && !this.active) {
          this.startupWaitFrames = 0;
        }
        this.underrunFrames = 0;
        this.underrunReported = false;
        this.port.postMessage({
          type: "queue",
          queuedFrames: this.pendingFrames(),
          queuedMs: this.pendingMs(),
        });
      } else if (data.type === "clear") {
        this.queue = [];
        this.offset = 0;
        this.active = false;
        this.startupWaitFrames = 0;
        this.underrunFrames = 0;
        this.underrunReported = false;
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

  readyToStartPlayback() {
    const pendingFrames = this.pendingFrames();
    if (pendingFrames === 0) {
      return false;
    }
    return (
      this.queue.length >= this.startupMinChunks ||
      pendingFrames >= this.startupMinFrames ||
      this.startupWaitFrames >= this.startupMaxWaitFrames
    );
  }

  process(_inputs, outputs) {
    const output = outputs[0];
    const left = output[0];
    const right = output[1] ?? left;

    left.fill(0);
    right.fill(0);

    if (!this.active) {
      if (this.queue.length === 0) {
        this.startupWaitFrames = 0;
        return true;
      }

      this.startupWaitFrames += left.length;
      if (!this.readyToStartPlayback()) {
        return true;
      }

      this.active = true;
      this.startupWaitFrames = 0;
      this.underrunFrames = 0;
      this.underrunReported = false;
      this.port.postMessage({
        type: "started",
        queuedFrames: this.pendingFrames(),
        queuedMs: this.pendingMs(),
      });
    }

    for (let frame = 0; frame < left.length; frame += 1) {
      if (this.queue.length === 0) {
        if (this.active) {
          this.underrunFrames += left.length - frame;
          if (!this.underrunReported) {
            this.underrunReported = true;
            this.port.postMessage({
              type: "underrun",
              queuedFrames: 0,
              queuedMs: 0,
            });
          }
          if (this.underrunFrames >= this.drainHoldFrames) {
            this.active = false;
            this.underrunFrames = 0;
            this.underrunReported = false;
            this.port.postMessage({
              type: "drain",
              queuedFrames: 0,
              queuedMs: 0,
            });
          }
        }
        break;
      }

      this.underrunFrames = 0;
      this.underrunReported = false;
      const current = this.queue[0];
      const sample = current[this.offset] ?? 0;
      left[frame] = sample;
      right[frame] = sample;
      this.offset += 1;

      if (this.offset >= current.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }

    return true;
  }
}

registerProcessor("playback-processor", PlaybackProcessor);
