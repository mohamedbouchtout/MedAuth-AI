/**
 * Fixtures for the nudge overlay's tests.
 *
 * The payload builder is `@medauth/nudge-client/testing` and is re-exported here
 * rather than redefined: it produces the wire shape from CLAUDE.md, "The nudge
 * payload — one shape", in snake_case, because that is what crosses the socket,
 * and one copy per app is how two suites come to agree with each other about a
 * payload neither one receives.
 *
 * The fake socket is deliberately thin: it records what it was constructed with
 * — the URL and the subprotocol list, which is where the credential rides — and
 * lets a test drive `onopen`, `onmessage` and `onclose` by hand. A real
 * WebSocket in jsdom would need a server, and a socket that fails to open is
 * precisely the case these tests exist to cover.
 */

import { encodeBase64Url } from './token';

export { fallbackNudgePayload, nudgePayload } from '@medauth/nudge-client/testing';

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
