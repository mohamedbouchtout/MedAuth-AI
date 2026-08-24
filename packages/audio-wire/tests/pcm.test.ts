import { describe, expect, it } from 'vitest';

import { floatToInt16LE } from '../src/pcm';

/** Read the converted output back as signed 16-bit little-endian samples. */
function readLE(buffer: ArrayBuffer): number[] {
  const view = new DataView(buffer);
  const out: number[] = [];
  for (let i = 0; i < buffer.byteLength; i += 2) {
    out.push(view.getInt16(i, true));
  }
  return out;
}

describe('floatToInt16LE', () => {
  it('produces two bytes per sample', () => {
    expect(floatToInt16LE(new Float32Array(4)).byteLength).toBe(8);
  });

  it('maps full scale to the int16 limits without wrapping', () => {
    // The wrap is the interesting failure: -1.0 scaled by 32767 and rounded is
    // fine, but scaled by 32768 without a clamp becomes +32768, which stores as
    // -32768's opposite sign and clicks.
    expect(readLE(floatToInt16LE(new Float32Array([1, -1, 0])))).toEqual([32767, -32768, 0]);
  });

  it('clamps beyond full scale rather than wrapping', () => {
    // Web Audio does not promise samples stay inside [-1, 1]; gain or a loud
    // room can push past it, and that happens on a transcript's loudest moment.
    expect(readLE(floatToInt16LE(new Float32Array([2, -2, 1.5])))).toEqual([
      32767, -32768, 32767,
    ]);
  });

  it('writes little-endian byte order explicitly', () => {
    // Asserted on the raw bytes, not through a DataView read, because the point
    // is the order on the wire rather than the round trip. 0x4000 is 1/2 scale.
    const bytes = new Uint8Array(floatToInt16LE(new Float32Array([0.5])));

    expect(bytes[0]).toBe(0x00);
    expect(bytes[1]).toBe(0x40);
  });

  it('rounds rather than truncating toward zero', () => {
    // Truncation biases every sample toward silence — a small DC-ish error
    // spread across an entire encounter rather than an audible artefact.
    const [sample] = readLE(floatToInt16LE(new Float32Array([0.99999])));

    expect(sample).toBe(32767);
  });

  it('handles an empty buffer', () => {
    expect(floatToInt16LE(new Float32Array(0)).byteLength).toBe(0);
  });
});
