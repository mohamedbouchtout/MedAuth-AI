import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist/**', 'coverage/**', 'node_modules/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.browser },
    },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    // The AudioWorklet processor runs in AudioWorkletGlobalScope, which has
    // neither `window` nor the DOM. Plain JS on purpose — see the comment at
    // the top of the file.
    files: ['src/audio/pcm-capture-processor.js'],
    languageOptions: {
      globals: { AudioWorkletProcessor: 'readonly', registerProcessor: 'readonly' },
    },
  },
);
