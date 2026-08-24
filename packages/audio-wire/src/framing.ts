/**
 * Re-chunks the variable-sized PCM buffers a capture layer delivers into the
 * fixed 250ms frames the audio-ingestion WebSocket expects.
 *
 * Both clients need this and neither controls its input size: `expo-audio`'s
 * `onBuffer` hands over whatever the native layer picked, and an
 * `AudioWorkletProcessor` hands over 128-sample render quanta. The framer is
 * the same either way, which is why it is here rather than in an app.
 *
 * This is deliberately a plain class with no React and no I/O: the chunking
 * boundary is the part most likely to be wrong in a way tests can catch, so it
 * is kept where a test can drive it directly.
 */

import { CHUNK_BYTES, MAX_PENDING_BYTES } from './format';

/** Raised when audio accumulates past the cap because the socket never opened. */
export class PendingAudioOverflow extends Error {
  constructor(pendingBytes: number, maxBytes: number) {
    // Byte counts only. Never the audio, and never anything derived from it.
    super(`pending audio ${pendingBytes} bytes exceeds cap of ${maxBytes}`);
    this.name = 'PendingAudioOverflow';
  }
}

export class PcmFramer {
  #buffer: Uint8Array = new Uint8Array(0);

  constructor(
    private readonly frameBytes: number = CHUNK_BYTES,
    private readonly maxPendingBytes: number = MAX_PENDING_BYTES,
  ) {}

  /** Bytes held but not yet emitted as a whole frame. */
  get pendingBytes(): number {
    return this.#buffer.length;
  }

  /**
   * Copy one captured buffer in.
   *
   * The copy is not incidental: on mobile `AudioStreamBuffer.data` comes from
   * the native layer and nothing promises the same ArrayBuffer is not handed
   * back on the next callback. Retaining it directly would let a later capture
   * overwrite audio still waiting to be sent.
   */
  push(data: ArrayBuffer): void {
    const incoming = new Uint8Array(data);
    const total = this.#buffer.length + incoming.length;
    if (total > this.maxPendingBytes) {
      throw new PendingAudioOverflow(total, this.maxPendingBytes);
    }
    const next = new Uint8Array(total);
    next.set(this.#buffer);
    next.set(incoming, this.#buffer.length);
    this.#buffer = next;
  }

  /**
   * Take every whole frame currently available, leaving any partial tail behind.
   *
   * The tail is held rather than padded: padding would insert silence into the
   * middle of an encounter, and the real tail arrives on the next callback.
   */
  takeFrames(): Uint8Array[] {
    const frames: Uint8Array[] = [];
    let offset = 0;
    while (this.#buffer.length - offset >= this.frameBytes) {
      frames.push(this.#buffer.slice(offset, offset + this.frameBytes));
      offset += this.frameBytes;
    }
    if (offset > 0) {
      this.#buffer = this.#buffer.slice(offset);
    }
    return frames;
  }

  /**
   * Drop everything held.
   *
   * Called on stop, on unmount and on every failure path — this buffer is the
   * only place this app holds encounter audio, and "audio never persists" means
   * it does not outlive the capture that produced it.
   */
  clear(): void {
    this.#buffer = new Uint8Array(0);
  }
}
