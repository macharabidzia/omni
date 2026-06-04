class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();

    const processorOptions = options?.processorOptions ?? {};
    this.targetSampleRate = processorOptions.targetSampleRate ?? sampleRate;
    this.chunkFrames = Math.max(1, processorOptions.chunkFrames ?? Math.round(this.targetSampleRate * 0.04));
    this.sourceToTargetRatio = sampleRate / this.targetSampleRate;
    this.transmitting = false;
    this.inputBuffer = new Float32Array(2048);
    this.inputLength = 0;
    this.readIndex = 0;
    this.chunkBuffer = new Float32Array(this.chunkFrames);
    this.chunkLength = 0;

    this.port.onmessage = (event) => {
      if (event.data?.type === "set-transmitting") {
        this.transmitting = Boolean(event.data.active);
        if (!this.transmitting) {
          this._resetBuffers();
        }
        return;
      }

      if (event.data?.type === "flush-and-stop") {
        this._drainResampler();
        this._flushChunk(true);
        this._resetBuffers();
        this.transmitting = false;
        this.port.postMessage({ type: "flush-complete", flushId: event.data.flushId });
      }
    };
  }

  process(inputs) {
    if (!this.transmitting) {
      return true;
    }

    const channel = inputs[0]?.[0];
    if (!channel || channel.length === 0) {
      return true;
    }

    this._appendInput(channel);
    this._drainResampler();
    return true;
  }

  _appendInput(samples) {
    const requiredLength = this.inputLength + samples.length;
    if (requiredLength > this.inputBuffer.length) {
      let nextLength = this.inputBuffer.length;
      while (nextLength < requiredLength) {
        nextLength *= 2;
      }
      const nextBuffer = new Float32Array(nextLength);
      nextBuffer.set(this.inputBuffer.subarray(0, this.inputLength));
      this.inputBuffer = nextBuffer;
    }

    this.inputBuffer.set(samples, this.inputLength);
    this.inputLength += samples.length;
  }

  _drainResampler() {
    while (this.inputLength - this.readIndex > 1) {
      const leftIndex = Math.floor(this.readIndex);
      const rightIndex = leftIndex + 1;
      if (rightIndex >= this.inputLength) {
        break;
      }

      const weight = this.readIndex - leftIndex;
      const sample =
        this.inputBuffer[leftIndex] * (1 - weight) + this.inputBuffer[rightIndex] * weight;
      this.chunkBuffer[this.chunkLength] = sample;
      this.chunkLength += 1;
      this.readIndex += this.sourceToTargetRatio;

      if (this.chunkLength === this.chunkFrames) {
        this._flushChunk();
      }
    }

    this._compactInputBuffer();
  }

  _compactInputBuffer() {
    const consumed = Math.floor(this.readIndex);
    if (consumed <= 0) {
      return;
    }

    const remaining = this.inputLength - consumed;
    if (remaining > 0) {
      this.inputBuffer.copyWithin(0, consumed, this.inputLength);
    }
    this.inputLength = Math.max(remaining, 0);
    this.readIndex -= consumed;
  }

  _flushChunk(padToFullChunk = false) {
    if (this.chunkLength === 0) {
      return;
    }

    const outputLength = padToFullChunk ? this.chunkFrames : this.chunkLength;
    const pcm16 = new Int16Array(outputLength);
    for (let index = 0; index < this.chunkLength; index += 1) {
      const sample = Math.max(-1, Math.min(1, this.chunkBuffer[index]));
      pcm16[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }

    this.port.postMessage({ type: "chunk", audioBytes: pcm16.buffer }, [pcm16.buffer]);
    this.chunkLength = 0;
  }

  _resetBuffers() {
    this.inputLength = 0;
    this.readIndex = 0;
    this.chunkLength = 0;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
