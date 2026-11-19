// StrykerJS mutation testing for the plain-script core layer of web/static/.
// Scope is deliberately the transport-free / near-pure modules — DOM-heavy
// files (app.js, browse/render.js, viewer/gesture.js, ...) are covered by the
// Playwright suite instead. See docs/mutation-testing.md.
export default {
  packageManager: 'npm',
  mutate: [
    'web/static/offline-core.js',
    'web/static/api/endpoints.js',
    'web/static/api/v1.js',
    'web/static/thumbs/resolveCover.js',
    'web/static/viewer/pairModel.js',
    'web/static/viewer/spread.js',
    'web/static/search/facets.js',
    'web/static/browse/hash.js',
  ],
  testRunner: 'vitest',
  vitest: {
    configFile: 'vitest.config.mjs',
  },
  // Instrumented plain scripts + happy-dom: keep concurrency modest — this
  // repo's dev box also runs cargo-mutants.
  concurrency: 4,
  // perTest coverage keeps mutant runs proportional to the tests that can
  // possibly kill them (the vitest runner supports it for these files).
  coverageAnalysis: 'perTest',
  reporters: ['clear-text', 'progress', 'html', 'json'],
  htmlReporter: { fileName: '.reports/mutation/stryker.html' },
  jsonReporter: { fileName: '.reports/mutation/stryker.json' },
  timeoutMS: 30000,
  // Vitest's own threshold is irrelevant here; Stryker enforces its own.
  thresholds: { high: 80, low: 60, break: null },
};
