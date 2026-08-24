import {
  PcmFramer,
  PendingAudioOverflow,
} from '../../../src/audio/framing';
import { CHUNK_BYTES, MAX_PENDING_BYTES } from '../../../src/audio/format';

/** A buffer of `bytes` length whose contents are identifiable per-frame. */
function pcm(bytes: number, fill = 0): ArrayBuffer {
  const view = new Uint8Array(bytes);
  view.fill(fill);
  return view.buffer;
}

describe('CHUNK_BYTES', () => {
  it('is 250ms of 16kHz mono int16 audio', () => {
    // 16000 samples/s x 2 bytes x 0.25s. Asserted rather than assumed: the
    // server-side chunking and Transcribe's sample rate both depend on it.
    expect(CHUNK_BYTES).toBe(8000);
  });
});

describe('PcmFramer', () => {
  it('emits nothing until a whole frame is available', () => {
    const framer = new PcmFramer();

    framer.push(pcm(CHUNK_BYTES - 1));

    expect(framer.takeFrames()).toEqual([]);
    expect(framer.pendingBytes).toBe(CHUNK_BYTES - 1);
  });

  it('emits exactly one frame of exactly CHUNK_BYTES', () => {
    const framer = new PcmFramer();

    framer.push(pcm(CHUNK_BYTES));
    const frames = framer.takeFrames();

    expect(frames).toHaveLength(1);
    expect(frames[0]).toHaveLength(CHUNK_BYTES);
    expect(framer.pendingBytes).toBe(0);
  });

  it('holds the partial tail back rather than padding it', () => {
    const framer = new PcmFramer();

    framer.push(pcm(CHUNK_BYTES + 100));
    const frames = framer.takeFrames();

    // Padding the tail with silence would splice quiet into the middle of an
    // encounter; the real remainder arrives on the next callback.
    expect(frames).toHaveLength(1);
    expect(framer.pendingBytes).toBe(100);
  });

  it('assembles one frame from many small buffers', () => {
    const framer = new PcmFramer();
    const pieces = 16;
    const size = CHUNK_BYTES / pieces;

    for (let i = 0; i < pieces; i += 1) {
      expect(framer.takeFrames()).toEqual([]);
      framer.push(pcm(size, i));
    }

    expect(framer.takeFrames()).toHaveLength(1);
  });

  it('splits one oversized buffer into several whole frames', () => {
    const framer = new PcmFramer();

    framer.push(pcm(CHUNK_BYTES * 3 + 7));
    const frames = framer.takeFrames();

    expect(frames).toHaveLength(3);
    expect(frames.every((frame) => frame.length === CHUNK_BYTES)).toBe(true);
    expect(framer.pendingBytes).toBe(7);
  });

  it('preserves byte order and content across the frame boundary', () => {
    const framer = new PcmFramer();

    framer.push(pcm(CHUNK_BYTES / 2, 0xaa));
    framer.push(pcm(CHUNK_BYTES / 2, 0xbb));
    const [frame] = framer.takeFrames();

    expect(frame?.[0]).toBe(0xaa);
    expect(frame?.[CHUNK_BYTES / 2 - 1]).toBe(0xaa);
    expect(frame?.[CHUNK_BYTES / 2]).toBe(0xbb);
    expect(frame?.[CHUNK_BYTES - 1]).toBe(0xbb);
  });

  it('copies incoming audio so a reused native buffer cannot corrupt it', () => {
    const framer = new PcmFramer();
    const native = new Uint8Array(CHUNK_BYTES);
    native.fill(0x11);

    framer.push(native.buffer);
    // The native layer is free to reuse the same ArrayBuffer for the next
    // capture. If the framer retained it rather than copying, the audio still
    // waiting to be sent would be overwritten here.
    native.fill(0x22);

    const [frame] = framer.takeFrames();
    expect(frame?.[0]).toBe(0x11);
  });

  it('throws PendingAudioOverflow past the cap instead of growing forever', () => {
    const framer = new PcmFramer();

    framer.push(pcm(MAX_PENDING_BYTES));

    expect(() => framer.push(pcm(1))).toThrow(PendingAudioOverflow);
  });

  it('names only byte counts in the overflow message, never audio', () => {
    const framer = new PcmFramer(CHUNK_BYTES, 10);

    expect(() => framer.push(pcm(11))).toThrow(/11 bytes exceeds cap of 10/);
  });

  it('drops everything held on clear', () => {
    const framer = new PcmFramer();

    framer.push(pcm(CHUNK_BYTES + 100));
    framer.clear();

    expect(framer.pendingBytes).toBe(0);
    expect(framer.takeFrames()).toEqual([]);
  });
});
