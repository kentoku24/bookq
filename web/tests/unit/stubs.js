// Test-env stubs loaded BEFORE the shipped scripts (import order = source
// order). Only fill gaps happy-dom does not provide; never override real DOM.

// spread.js imagePool.init() runs at load and calls `new Image()`.
if (typeof globalThis.Image === 'undefined') {
  globalThis.Image = class {
    constructor() {
      this.decoding = '';
      this.draggable = false;
      this.alt = '';
      this.dataset = {};
      this.classList = {
        _s: new Set(),
        add(c) { this._s.add(c); },
        remove(c) { this._s.delete(c); },
        contains(c) { return this._s.has(c); },
      };
      this.style = { setProperty() {} };
      this.listeners = {};
      this.complete = false;
      this.naturalWidth = 0;
      this.naturalHeight = 0;
    }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    removeEventListener() {}
    getAttribute() { return null; }
  };
}

if (typeof globalThis.requestAnimationFrame === 'undefined') {
  globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
}
