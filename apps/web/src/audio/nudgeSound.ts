/** A short, quiet notification tone, enabled by the provider's Start visit click. */
let context: AudioContext | null = null;

/** Unlock audio during a user gesture so later WebSocket alerts can play it. */
export function enableNudgeSound(): void {
  try {
    if (context === null || context.state === 'closed') {
      context = new AudioContext();
    }
    if (context.state === 'suspended') {
      void context.resume().catch(() => {});
    }
  } catch {
    // Audio is optional: unsupported browsers still show the visual alert.
  }
}

/** Play one 150 ms sine tone with a soft attack and release to avoid clicks. */
export function playNudgeSound(): void {
  if (context === null || context.state !== 'running') return;
  try {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const start = context.currentTime;
    oscillator.type = 'sine';
    oscillator.frequency.value = 740;
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(0.05, start + 0.015);
    gain.gain.linearRampToValueAtTime(0, start + 0.15);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.onended = () => {
      oscillator.disconnect();
      gain.disconnect();
    };
    oscillator.start(start);
    oscillator.stop(start + 0.15);
  } catch {
    // A sound failure must never prevent the nudge from appearing.
  }
}
