/**
 * Fixtures for the nudge overlay's tests.
 *
 * The payload builders are `@medauth/nudge-client/testing` and are re-exported
 * here rather than redefined: they produce the wire shape from CLAUDE.md, "The
 * nudge payload — one shape", in snake_case, because that is what crosses the
 * socket. Building a fixture from this app's parsed `Nudge` would test the
 * component against the parser's output rather than against what nudge-service
 * actually relays, and a copy per app is how two suites come to agree with each
 * other about a payload neither one receives.
 *
 * The fake socket stands in for React Native's WebSocket, which takes a third
 * options argument the DOM's does not — that argument is where the credential
 * rides on this platform, so the fake records it. A test drives `onopen`,
 * `onmessage` and `onclose` by hand: a socket that fails to open is precisely
 * the case these tests exist to cover, and it cannot be produced by a real one.
 */

export { fallbackNudgePayload, nudgePayload } from '@medauth/nudge-client/testing';

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;

  constructor(
    readonly url: string,
    readonly protocols: string[] | undefined,
    readonly options: { headers: Record<string, string> },
  ) {
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  /** Drive the handshake completing. */
  fireOpen(): void {
    this.onopen?.();
  }

  /** Deliver one relayed frame. */
  fireMessage(data: unknown): void {
    this.onmessage?.({ data });
  }

  /** Drive a close. Before `fireOpen` this is a refused upgrade. */
  fireClose(): void {
    this.onclose?.();
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
