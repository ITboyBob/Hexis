class HexisCaptureProcessor extends AudioWorkletProcessor {
  process(inputs, outputs) {
    const channels = inputs[0];
    if (!channels || channels.length === 0) return true;
    const frames = channels[0].length;
    const mono = new Float32Array(frames);
    for (let frame = 0; frame < frames; frame += 1) {
      let value = 0;
      for (let channel = 0; channel < channels.length; channel += 1) {
        value += channels[channel][frame] || 0;
      }
      mono[frame] = value / channels.length;
    }
    const outputChannels = outputs[0];
    if (outputChannels && outputChannels[0]) outputChannels[0].set(mono);
    this.port.postMessage(
      { samples: mono, startFrame: currentFrame, observedAtMs: performance.now() },
      [mono.buffer],
    );
    return true;
  }
}

registerProcessor("hexis-voice-capture", HexisCaptureProcessor);
