import { defineConfig } from 'vitest/config';

/**
 * Node environment, not jsdom: nothing here touches the DOM. The HTTP client
 * takes its `fetch` as a parameter rather than reaching for a global, which is
 * what lets this suite run without a browser and what lets each app supply its
 * own — Metro's on mobile, the browser's on web.
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
