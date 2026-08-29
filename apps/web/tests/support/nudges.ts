/**
 * Fixtures for the nudge overlay's tests.
 *
 * The payload builder produces the wire shape from CLAUDE.md, "The nudge payload
 * — one shape", in snake_case, because that is what crosses the socket. Building
 * it from the app's own `Nudge` type would test the component against the
 * parser's output rather than against what nudge-service actually relays.
 *
 * The fake socket is deliberately thin: it records what it was constructed with
 * — the URL and the subprotocol list, which is where the credential rides — and
 * lets a test drive `onopen`, `onmessage` and `onclose` by hand. A real
 * WebSocket in jsdom would need a server, and a socket that fails to open is
 * precisely the case these tests exist to cover.
 */

import { encodeBase64Url } from './token';

/** One nudge as track-b-rag publishes it. */
export function nudgePayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'PAYER_RULE_ALERT',
    nudge_id: '0b7f0000-0000-4000-8000-000000000001',
    procedure: 'knee MRI',
    cpt_code: '73721',
    message: 'Prior authorization required for knee MRI. Still undocumented: six weeks of therapy.',
    missing_criteria: ['six weeks of conservative therapy'],
    denial_risk: 'high',
    haptic: true,
    ...overrides,
  };
}

/** A token shaped like a session JWT. No signature is real. */
export function tokenExpiringAt(nowMs: number, secondsFromNow: number): string {
  const claims = { session_id: 'test', exp: Math.floor(nowMs / 1000) + secondsFromNow };
  return `header.${encodeBase64Url(JSON.stringify(claims))}.signature`;
}

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;

  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  /** The most recently constructed socket, which is the one under test. */
  static get last(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (socket === undefined) {
      throw new Error('no WebSocket was constructed');
    }
    return socket;
  }

  static reset(): void {
    FakeWebSocket.instances = [];
  }
}

/** Install the fake as the global `WebSocket` and return a restore function. */
export function installFakeWebSocket(): () => void {
  const original = globalThis.WebSocket;
  FakeWebSocket.reset();
  (globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket;
  return () => {
    (globalThis as { WebSocket: unknown }).WebSocket = original;
  };
}
