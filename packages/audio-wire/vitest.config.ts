import { defineConfig } from 'vitest/config';

/**
 * Node environment, not jsdom: nothing here touches the DOM. The browser-facing
 * half of the wire format (`floatToInt16LE`) is a pure function over typed
 * arrays for exactly that reason — it is the part of `apps/web`'s capture path
 * that can be tested without a fake `AudioContext`.
 *
 * The coverage gate is the 80% CLAUDE.md applies to everything under packages/.
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.ts'],
      // The barrel re-exports and holds no logic; counting it would inflate the
      // number this gate exists to keep honest.
      exclude: ['src/index.ts'],
      thresholds: { branches: 80, functions: 80, lines: 80, statements: 80 },
    },
  },
});
