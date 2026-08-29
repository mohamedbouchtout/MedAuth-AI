import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

import '@testing-library/jest-dom/vitest';

/**
 * Unmount between tests, explicitly.
 *
 * React Testing Library registers its own cleanup automatically **only** when
 * `afterEach` is a global, which is `globals: true` in a Vitest config. This
 * project imports its test functions instead, so nothing was unmounting: every
 * render stayed in `document.body` and later tests queried a DOM holding earlier
 * tests' components. That reads as a component bug — duplicate alerts, a stale
 * banner that will not go away — rather than as missing cleanup, which is why it
 * is fixed here once rather than per file.
 */
afterEach(cleanup);
