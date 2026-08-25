/**
 * The AudioWorklet half of browser audio capture — deliberately the smallest
 * thing that can work.
 *
 * It copies each render quantum's input channel and posts it to the main
 * thread. It converts nothing, buffers nothing and decides nothing, because
 * `AudioWorkletProcessor` does not exist in jsdom: anything implemented here is
 * code TASK-023's tests cannot reach. Conversion (`floatToInt16LE`) and framing
 * (`PcmFramer`) live in `packages/audio-wire` as pure functions with their own
 * suite, and the format checks live in `useAudioCapture`.
 *
 * Plain JavaScript rather than TypeScript, and loaded by URL rather than
 * imported: `audioWorklet.addModule()` fetches a real module into a separate
 * global scope, so it is an asset the bundler copies, not part of the app graph.
 *
 * `sampleRate` and `currentFrame` are globals of `AudioWorkletGlobalScope`.
 * `sampleRate` is reported on every message because it is the rate the audio was
 * actually rendered at — the second half of the two-stage check `useAudioCapture`
 * runs before it opens the socket.
 */

class PcmCaptureProcessor extends AudioWorkletProcessor {
  /**
   * Nothing is written to `outputs`, which Web Audio zero-fills every quantum.
   * The node has an output only so the graph can reach the destination and be
   * guaranteed a `process()` call in every engine; what travels down it is
   * silence, and `useAudioCapture` puts a zero gain in the way as well.
   *
   * @param {Float32Array[][]} inputs — one entry per input, each a list of channels.
   * @returns {boolean} true, always: the node stays alive until it is disconnected.
   */
  process(inputs) {
    const input = inputs[0];
    const channel = input && input[0];
    if (!channel || channel.length === 0) {
      // A quantum with no connected input. Not an error — stay alive.
      return true;
    }

    // Copied because the render quantum's buffer is reused on the next call,
    // and because the copy is what gets transferred to the main thread.
    const samples = new Float32Array(channel);
    this.port.postMessage({ sampleRate, channels: input.length, samples }, [samples.buffer]);
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
