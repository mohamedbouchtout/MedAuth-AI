/**
 * Test layout mirrors src/, the same convention the Python services use:
 * `src/hooks/useAudioCapture.ts` is covered by
 * `tests/unit/hooks/useAudioCapture.test.ts`.
 *
 * The coverage gate is the same 80% CI applies to services/ and packages/.
 * `App.tsx` and `index.ts` are excluded because they are the Expo entry point
 * and the wiring that hands the session screen its patient source — TASK-025
 * put every decision that wiring depends on inside src/, where it is covered.
 *
 * `@medauth/audio-wire` is mapped to its TypeScript source rather than resolved
 * through node_modules. The package publishes no build output on purpose (see
 * its tsconfig), and jest's default `transformIgnorePatterns` would otherwise
 * skip transforming it because npm links workspace packages under
 * node_modules/. It is not in `collectCoverageFrom` either — the package has
 * its own suite and its own gate, and counting it here would let this app's
 * coverage ride on tests it does not own.
 */
module.exports = {
  preset: 'jest-expo',
  roots: ['<rootDir>/tests'],
  testMatch: ['**/*.test.ts', '**/*.test.tsx'],
  moduleNameMapper: {
    '^@medauth/audio-wire$': '<rootDir>/../../packages/audio-wire/src/index.ts',
  },
  collectCoverage: true,
  collectCoverageFrom: ['src/**/*.{ts,tsx}'],
  coverageThreshold: {
    global: { branches: 80, functions: 80, lines: 80, statements: 80 },
  },
};
