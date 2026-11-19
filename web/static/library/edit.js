/* bookq library/edit — ライブラリ編集 (追加 + 削除) UI.
 *
 * `api/openapi.v1.yaml` の `/api/v1/library/*` クライアント。
 * - 編集モード: ヘッダの ✏️ でトグル。サイドバーにアップロードバー +
 *   各タイルに 🗑 (2タップ確定) が出る。OFF では一切のマークアップを出さない
 *   (visibility-toggle と同じ方針)。
 * - アップロード: 32MiB 以下は 1 multipart リクエストにまとめ、それ超は
 *   分割アップロード (init → PUT Upload-Offset 順 → finalize)。Cloudflare
 *   Free の ~100MB リクエスト上限対策 — 大容量は必ず分割経路。
 * - 確定後は loadFolders({preservePath:true}) でツリーを取り直す
 *   (サーバ側が index refresh を schedule 済み — 202 再試行は loadFolders
 *   側が既に持つ)。
 */
(function () {
  var CHUNK_THRESHOLD = 32 * 1024 * 1024;
  var CHUNK_SIZE = 8 * 1024 * 1024;
  var CONFIRM_MS = 4000;               // 削除2タップの有効時間
  var UPLOAD_EXT = /\.(zip|cbz|rar|cbr|epub|pdf)$/i;

  var _editing = false;
  var _uploading = 0;                  // in-flight ファイル数
  var _confirmAt = {};                 // path -> first-tap ts (削除2タップ)

  function _esc(s) { return window.OfflineCore.escapeHtml(s); }
  function _toastErr(m) { try { window.showErrorToast(m); } catch (e) {} }
  function _toastOk(m) {
    try {
      if (window.showToastSuccess) window.showToastSuccess(m);
      else window.showToast(m);
    } catch (e) {}
  }
  function _rerender() { try { if (window.renderView) window.renderView(); } catch (e) {} }
  function _reloadTree() {
    try { if (window.loadFolders) window.loadFolders({ preservePath: true }); } catch (e) {}
  }
  function _grid() { return document.getElementById("tile-grid"); }
  function _dir() {
    var p = window.currentBrowsePath;
    return (typeof p === "string") ? p : "";
  }
  function _bar() { return document.getElementById("lib-edit-bar"); }
  function _status() { return document.getElementById("lib-edit-status"); }

  // ── error plumbing ─────────────────────────────────────────────────────────

  function _err(status, body) {
    var d = (body && typeof body === "object" && typeof body.detail === "string")
      ? body.detail : "";
    var c = (body && typeof body === "object" && typeof body.code === "string")
      ? body.code : null;
    return new window.BookqApi.ApiError(status, c, d || ("request failed: " + status));
  }
  function _errText(e) {
    return (e && e.detail) ? e.detail : String(e || "error");
  }

  // ── uploads ────────────────────────────────────────────────────────────────

  // multipart 一括: XHR (fetch には upload progress が無い)。
  function _postMultipart(dir, files, onprogress) {
    return new Promise(function (resolve, reject) {
      var fd = new FormData();
      for (var i = 0; i < files.length; i++) fd.append("file", files[i], files[i].name);
      var xhr = new XMLHttpRequest();
      xhr.open("POST", window.BookqApi.endpoints.libraryFiles + "?dir=" + encodeURIComponent(dir));
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && onprogress) onprogress(e.loaded, e.total);
      };
      xhr.onload = function () {
        var body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(body || { files: [], errors: [] });
        else reject(_err(xhr.status, body));
      };
      xhr.onerror = function () { reject(_err(0, null)); };
      xhr.send(fd);
    });
  }

  function _putChunk(url, blob, offset) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("Upload-Offset", String(offset));
      xhr.onload = function () {
        var body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) {}
        resolve({ status: xhr.status, body: body });
      };
      xhr.onerror = function () { resolve({ status: 0, body: null }); };
      xhr.send(blob);
    });
  }

  // 分割アップロード: サーバ offset を正として進める (409 時は再同期)。
  function _postChunked(dir, file, onprogress) {
    var api = window.BookqApi;
    return api.fetchJson(api.endpoints.libraryUploads, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dir: dir, name: file.name, size: file.size }),
    }).then(function (out) {
      if (out.status !== 201 || !out.body || typeof out.body.upload_id !== "string") {
        throw _err(out.status, out.body);
      }
      var id = out.body.upload_id;
      var url = api.endpoints.libraryUpload.replace("{id}", encodeURIComponent(id));
      var fin = api.endpoints.libraryUploadFinalize.replace("{id}", encodeURIComponent(id));
      var offset = 0;
      var retries = 0;
      function step() {
        if (offset >= file.size) {
          return api.fetchJson(fin, { method: "POST" }).then(function (o) {
            if (o.status !== 200) throw _err(o.status, o.body);
            return { name: (o.body && o.body.name) || file.name };
          });
        }
        var end = Math.min(offset + CHUNK_SIZE, file.size);
        return _putChunk(url, file.slice(offset, end), offset).then(function (res) {
          if (res.status === 409 && res.body && typeof res.body.offset === "number") {
            offset = res.body.offset;      // サーバの受領位置へ再同期
          } else if (res.status === 200) {
            offset = end;
          } else if (res.status === 0 || res.status >= 500) {
            // ネットワーク断: 再開 probe で正しい offset を取り直す。
            if (++retries > 3) throw _err(res.status || 599, res.body);
            return api.fetchJson(url, {}).then(function (o) {
              if (o.status !== 200 || !o.body || typeof o.body.offset !== "number") {
                throw _err(o.status, o.body);
              }
              offset = o.body.offset;
              return step();
            });
          } else {
            throw _err(res.status, res.body);
          }
          retries = 0;
          if (onprogress) onprogress(offset, file.size);
          return step();
        });
      }
      return step();
    });
  }

  var _CODE_TEXT = {
    invalid_name: "ファイル名が不正です",
    unsupported_type: "対応していない形式です",
    too_large: "サイズ上限を超えています",
    io_error: "書き込みに失敗しました",
    conflict: "同名ファイルが多すぎます",
  };

  // 選択/drop された FileList → dir 直下へ。小ファイルは 1 multipart に
  // まとめ、大ファイルは個別の分割セッションで直列に流す。
  function uploadFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    var dir = _dir();
    var small = [], large = [], rejected = [];
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (!UPLOAD_EXT.test(f.name)) rejected.push(f.name);
      else if (f.size > CHUNK_THRESHOLD) large.push(f);
      else small.push(f);
    }
    for (var r = 0; r < rejected.length; r++) {
      _toastErr(rejected[r] + ": " + _CODE_TEXT.unsupported_type);
    }
    if (!small.length && !large.length) return;

    // 1 リクエストの合計も CHUNK_THRESHOLD まで — 個別に閾値以下でも
    // まとめた body が CF の request-body 上限を超えると 413 になる。
    var batches = [], cur = [], curBytes = 0;
    for (var s = 0; s < small.length; s++) {
      if (cur.length && curBytes + small[s].size > CHUNK_THRESHOLD) {
        batches.push(cur); cur = []; curBytes = 0;
      }
      cur.push(small[s]); curBytes += small[s].size;
    }
    if (cur.length) batches.push(cur);

    var totalBytes = 0, sentBytes = 0;
    for (var j = 0; j < files.length; j++) totalBytes += files[j].size;
    var idx = 0, jobs = batches.length + large.length;
    function _tick(n) {
      idx++;
      if (_status()) _status().textContent = "アップロード中 " + idx + "/" + jobs + " (" + n + "%)";
    }
    _uploading++;
    var done = 0, failed = [];
    function finish() {
      _uploading--;
      if (_status()) _status().textContent = "";
      if (done) _reloadTree();
      for (var k = 0; k < failed.length; k++) _toastErr(failed[k]);
      if (done && !failed.length) _toastOk(done + " 件追加しました");
    }
    var chain = Promise.resolve();
    for (var b = 0; b < batches.length; b++) (function (batch) {
      var batchBytes = totalBytesOf(batch);
      chain = chain.then(function () {
        var base = sentBytes;
        return _postMultipart(dir, batch, function (sent, total) {
          _tick(Math.round(100 * (base + sent) / totalBytes));
        }).then(function (res) {
          sentBytes += batchBytes;
          done += (res && Array.isArray(res.files)) ? res.files.length : 0;
          var errs = (res && Array.isArray(res.errors)) ? res.errors : [];
          for (var e = 0; e < errs.length; e++) {
            failed.push(errs[e].name + ": " + (_CODE_TEXT[errs[e].code] || errs[e].code));
          }
        });
      });
    })(batches[b]);
    for (var li = 0; li < large.length; li++) (function (f) {
      var base = sentBytes;
      chain = chain.then(function () {
        base = sentBytes;
        return _postChunked(dir, f, function (off) {
          _tick(Math.round(100 * (base + off) / totalBytes));
        }).then(function () { sentBytes += f.size; done++; });
      });
    })(large[li]);
    chain.catch(function (e) { failed.push(_errText(e)); })
      .then(finish);
  }
  function totalBytesOf(list) {
    var n = 0;
    for (var i = 0; i < list.length; i++) n += list[i].size;
    return n;
  }

  // ── delete ────────────────────────────────────────────────────────────────

  function deletePath(path) {
    var api = window.BookqApi;
    return api.fetchJson(api.endpoints.libraryFiles + "?path=" + encodeURIComponent(path), {
      method: "DELETE",
    }).then(function (out) {
      if (out.status !== 200) throw _err(out.status, out.body);
      return out.body;
    });
  }

  function _doDelete(path) {
    var node = window.getNodeAt ? window.getNodeAt(path) : null;
    var isDir = !!(node && node.type === "folder" && !node._syntheticLeaf);
    if (isDir && !window.confirm("フォルダ「" + path + "」を中身ごと削除します。よろしいですか？")) return;
    deletePath(path).then(function () {
      _toastOk("削除しました");
      _reloadTree();
    }, function (e) { _toastErr(_errText(e)); });
  }

  function _onDelTap(btn, tile) {
    var path = tile ? tile.getAttribute("data-path") : null;
    if (!path) return;
    var now = Date.now();
    if (_confirmAt[path] && now - _confirmAt[path] < CONFIRM_MS) {
      delete _confirmAt[path];
      _doDelete(path);
      return;
    }
    _confirmAt[path] = now;
    btn.classList.add("lib-del-arm");
    setTimeout(function () {
      delete _confirmAt[path];
      btn.classList.remove("lib-del-arm");
    }, CONFIRM_MS);
  }

  // render.js のタイル HTML へ差し込むバッジ (編集 OFF/検索中では空文字)。
  // 検索タイルは browse ツリーの実体と _fullPath がずれることがあるので
  // 検索結果には削除ボタンを出さない。
  function delBadgeHtml() {
    if (!_editing || window.searchMode) return "";
    return '<button type="button" class="lib-del" aria-label="削除" title="削除 (2タップで確定)">🗑</button>';
  }

  // ── edit mode ─────────────────────────────────────────────────────────────

  function setEditing(on) {
    _editing = !!on;
    document.body.classList.toggle("lib-editing", _editing);
    var btn = document.getElementById("lib-edit-btn");
    if (btn) btn.setAttribute("aria-pressed", _editing ? "true" : "false");
    var bar = _bar();
    if (bar) bar.hidden = !_editing;
    for (var k in _confirmAt) delete _confirmAt[k];
    if (_status()) _status().textContent = "";
    _rerender();
  }
  function isEditing() { return _editing; }

  function _bindDom() {
    var btn = document.getElementById("lib-edit-btn");
    if (btn) btn.addEventListener("click", function () { setEditing(!_editing); });
    var up = document.getElementById("lib-upload-btn");
    var input = document.getElementById("lib-upload-input");
    if (up && input) up.addEventListener("click", function () { input.click(); });
    if (input) input.addEventListener("change", function () {
      uploadFiles(input.files);
      input.value = "";
    });
    var done = document.getElementById("lib-edit-done");
    if (done) done.addEventListener("click", function () { setEditing(false); });

    // 削除タップ: capture で tile ナビより先に拾う (bubble 側の delegated
    // browse handler より先に発火させる必要がある)。
    var grid = _grid();
    if (grid) grid.addEventListener("click", function (e) {
      var b = e.target && e.target.closest ? e.target.closest(".lib-del") : null;
      if (!b) return;
      e.preventDefault();
      e.stopPropagation();
      var tile = b.closest(".tile,.item,.card");
      _onDelTap(b, tile);
    }, true);

    // drag & drop — 編集モード中のみ受け付ける (誤 drop での追加防止)。
    var sc = document.getElementById("sidebar-scroll");
    if (sc) {
      sc.addEventListener("dragover", function (e) {
        if (!_editing) return;
        e.preventDefault();
        sc.classList.add("lib-drop");
      });
      sc.addEventListener("dragleave", function (e) {
        if (e.target === sc) sc.classList.remove("lib-drop");
      });
      sc.addEventListener("drop", function (e) {
        sc.classList.remove("lib-drop");
        if (!_editing) return;
        e.preventDefault();
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
          uploadFiles(e.dataTransfer.files);
        }
      });
    }
  }

  window.LibraryEdit = {
    setEditing: setEditing,
    isEditing: isEditing,
    delBadgeHtml: delBadgeHtml,
    uploadFiles: uploadFiles,
    deletePath: deletePath,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bindDom);
  } else {
    _bindDom();
  }
})();
