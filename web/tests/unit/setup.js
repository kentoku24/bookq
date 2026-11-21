// Loads the shipped web/static scripts under test, in the same relative
// order as web/templates/index.html (offline-core → endpoints → v1 →
// feature modules). Import order is execution order for side effects, so
// load-time reads like facets.js's `window.OfflineCore.escapeHtml` resolve.
import './stubs.js';
import '../../static/offline-core.js';
import '../../static/api/endpoints.js';
import '../../static/api/v1.js';
import '../../static/thumbs/resolveCover.js';
import '../../static/viewer/pairModel.js';
import '../../static/viewer/spread.js';
import '../../static/search/facets.js';
import '../../static/browse/hash.js';
