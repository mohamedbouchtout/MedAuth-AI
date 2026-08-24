/**
 * Float samples to the int16 little-endian PCM Transcribe Medical reads.
 *
 * The browser is the only current caller: the Web Audio API works in
 * normalised `Float32Array` samples, so a capture path that ends in an
 * `AudioWorkletProcessor` has to do this conversion itself. Mobile does not —
 * `expo-audio` fills an int16 buffer natively and `useAudioCapture` forwards
 * those bytes untouched. It lives beside the rest of the wire format anyway,
 * because what it produces *is* the wire format, and a second float-sourced
 * client would otherwise write it again.
 *
 * Two properties are the whole point of the function:
 *
 * - **Little-endian is written explicitly, not inherited.** `DataView.setInt16`
 *   takes the byte order as an argument, so the output is identical on a
 *   big-endian host. That is why the browser path needs no `isLittleEndian()`
 *   guard, and why the wrong answer here would be inaudible noise rather than a
 *   crash — the same failure mode that guard exists for on mobile.
 * - **Out-of-range input is clamped, not wrapped.** Web Audio does not promise
 *   samples stay inside [-1, 1]; gain, or simply a loud room, can push past it.
 *   Multiplying and truncating without a clamp turns a peak into a sample of
 *   the opposite sign, which is an audible click on a transcript's loudest
 *   moment — usually the part someone wants transcribed most.
 */

/** The most negative and most positive values an int16 sample can hold. */
const INT16_MIN = -32_768;
const INT16_MAX = 32_767;

/**
 * Convert normalised float samples to int16 little-endian bytes.
 *
 * Positive and negative full scale are scaled by their own limits rather than
 * by a single factor, so +1.0 maps to `INT16_MAX` and -1.0 to `INT16_MIN`
 * without either end wrapping.
 */
export function floatToInt16LE(samples: Float32Array): ArrayBuffer {
  const out = new ArrayBuffer(samples.length * 2);
  const view = new DataView(out);
  // Iterated by value rather than by index: `noUncheckedIndexedAccess` would
  // otherwise force a `?? 0` fallback that a Float32Array can never take,
  // leaving a permanently uncovered branch in the middle of the hot loop.
  let offset = 0;
  for (const sample of samples) {
    const scaled = sample < 0 ? sample * -INT16_MIN : sample * INT16_MAX;
    const clamped = Math.max(INT16_MIN, Math.min(INT16_MAX, Math.round(scaled)));
    // `true` is the byte order, and it is an argument rather than a host
    // property on purpose. See the module comment.
    view.setInt16(offset, clamped, true);
    offset += 2;
  }
  return out;
}
