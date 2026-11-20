import { defineConfig } from 'vitest/config';

// Unit tests for the plain-script modules in web/static/ (no build step).
// The shipped files are IIFEs that publish onto window/globalThis — the
// setup file loads them in index.html defer order so call-time window.*
// lookups resolve exactly like production.
export default defineConfig({
  test: {
    environment: 'happy-dom',
    include: ['web/tests/unit/**/*.test.js'],
    setupFiles: ['web/tests/unit/setup.js'],
    // Each file gets a fresh happy-dom window + module registry, so window.*
    // state mutated by one test file cannot leak into another.
  },
});
