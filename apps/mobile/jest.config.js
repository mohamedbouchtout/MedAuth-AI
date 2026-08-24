/**
 * Test layout mirrors src/, the same convention the Python services use:
 * `src/audio/framing.ts` is covered by `tests/unit/audio/framing.test.ts`.
 *
 * The coverage gate is the same 80% CI applies to services/ and packages/.
 * `App.tsx` and `index.ts` are excluded because they are the Expo entry point
 * and a placeholder screen — TASK-025 builds the real session UI and brings its
 * own tests. Everything that carries logic is inside src/.
 */
module.exports = {
  preset: 'jest-expo',
  roots: ['<rootDir>/tests'],
  testMatch: ['**/*.test.ts', '**/*.test.tsx'],
  collectCoverage: true,
  collectCoverageFrom: ['src/**/*.{ts,tsx}'],
  coverageThreshold: {
    global: { branches: 80, functions: 80, lines: 80, statements: 80 },
  },
};
