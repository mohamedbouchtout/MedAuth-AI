import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * One config for the build and the test run, so the two cannot disagree about
 * how a module resolves. `@medauth/audio-wire` in particular ships TypeScript
 * source rather than a build, and it must be transpiled the same way whether
 * Vite is serving the app or Vitest is running a suite.
 *
 * The coverage gate is the 80% CLAUDE.md applies across the repo. `main.tsx`
 * and `App.tsx` are excluded because they are the entry point and a placeholder
 * screen — TASK-070 builds the real session UI and brings its own tests.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Never inline the AudioWorklet processor. It is imported with `?url` and
    // is small enough to fall under the default 4KB inline limit, which turns
    // it into a `data:text/javascript;base64,...` string — verified by
    // inspecting a production bundle. `audioWorklet.addModule()` would then be
    // fetching a data: URL, which any `script-src 'self'` content security
    // policy blocks, and this app is one that should have one. Emitting it as a
    // hashed asset keeps it a same-origin script.
    assetsInlineLimit: (filePath: string) => (filePath.endsWith('.js') ? false : undefined),
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/App.tsx', 'src/audio/pcm-capture-processor.js'],
      thresholds: { branches: 80, functions: 80, lines: 80, statements: 80 },
    },
  },
});
