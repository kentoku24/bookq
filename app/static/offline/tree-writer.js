importScripts("/static/offline-core.js");

self.onmessage = async function (e) {
  var text = e && e.data && e.data.text;
  var envelope = !!(e && e.data && e.data.envelope);
  try {
    var parsed = JSON.parse(text);
    var data = (envelope && parsed && !Array.isArray(parsed)) ? parsed.tree : parsed;
    if (!Array.isArray(data)) {
      self.postMessage({ ok: false, invalid: true });
      return;
    }
    await self.OfflineTree.putTree(data);
    self.postMessage({ ok: true });
  } catch (err) {
    self.postMessage({
      ok: false,
      invalid: !!(err && err.name === "SyntaxError"),
      error: String((err && err.message) || err),
    });
  }
};
