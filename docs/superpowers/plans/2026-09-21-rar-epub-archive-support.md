# RAR / EPUB アーカイブ対応 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `.rar` / `.cbr` と固定レイアウト `.epub` を、既存の ZIP/CBZ と同じ「仮想フォルダ」としてビューアで読めるようにする。

**Architecture:** アーカイブ処理（形式判別・エントリ列挙・1 エントリ読み出し）を新モジュール `app/archive.py` に集約し、scanner / routes / thumbs / metadata はそこを参照する。RAR は `rarfile`（展開は外部ツール `7zz` / `bsdtar` に委譲）、EPUB は `zipfile` + `xml.etree` で spine 順に画像名を得る。フロントエンドは無変更。

**Tech Stack:** Python 3.14, FastAPI, stdlib `zipfile` / `xml.etree.ElementTree`, `rarfile>=4.2`, Debian `7zip`（Docker ランタイム）, pytest + FastAPI TestClient。

**Spec:** `docs/superpowers/specs/2026-09-21-rar-epub-archive-support-design.md`

## Global Constraints

- 対象拡張子: `ARCHIVE_EXTENSIONS = {".zip", ".cbz", ".rar", ".cbr", ".epub"}`。PDF（`.pdf`）は `app/scanner.py` の別経路のまま触らない。
- アーカイブ処理は **決して 500 を出さない**: 列挙失敗は `[]`、読み出し失敗は `None`（→ 404）、展開ツール不在は `ArchiveToolMissing`（→ 501）。
- エントリ上限 `MAX_ARCHIVE_ENTRIES = 2000`、安全フィルタ `_is_image_entry`（`..`・絶対パス・`__MACOSX`・ドットファイル・非画像拡張子を除外）は全形式共通。
- モジュール依存は一方向: `app/archive.py` → `app/scanner.py`（`natural_key`, `IMAGE_EXTENSIONS`）。scanner は `scan_tree` 内で関数内 import する。scanner に再エクスポートは残さない。
- 対象外: 暗号化 RAR、マルチボリューム RAR、テキスト中心 EPUB のレンダリング、7z/tar、展開キャッシュ。
- コミットは GPG 署名付き（`git commit -S`）。メッセージ末尾に `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- テスト実行コマンド（リポジトリルートで）:
  `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py tests/test_scanner.py tests/test_thumbs.py -q`
- 作業ディレクトリは `/Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b`（git worktree）。`cd` せず絶対パスか `git -C` を使う。

---

## ファイル構成

- Create: `app/archive.py` — アーカイブ形式の判別、エントリ列挙、1 エントリ読み出し（zip / rar / epub）。
- Create: `tests/fixtures/rar5.py` — テスト用に RAR5 無圧縮書庫を手書きするヘルパー（外部 `rar` バイナリ不要）。
- Modify: `app/scanner.py` — zip 関連の関数・定数を archive.py へ移し、`scan_tree` は関数内 import で参照。
- Modify: `app/routes.py` — import 元を `app.archive` へ、`_serve_archive_entry` の zip 読み出しを `read_archive_entry` に置換、501 変換。
- Modify: `app/thumbs.py` — `_load_archive_source` を同様に置換。
- Modify: `app/metadata/index.py` — import 元を `app.archive` へ。
- Modify: `tests/test_archive.py` — import 元の変更、RAR / EPUB テスト追加。
- Modify: `requirements.txt`, `Dockerfile`, `CLAUDE.md`。

---

### Task 1: `app/archive.py` を作り zip 処理を移す（振る舞い不変のリファクタ）

**Files:**
- Create: `app/archive.py`
- Modify: `app/scanner.py:1-27`（import と定数）, `app/scanner.py:87-144`（`_is_safe_entry`〜`is_archive_file`）, `app/scanner.py:164-283`（`scan_tree`）
- Modify: `app/routes.py:23`, `app/routes.py:463`, `app/thumbs.py:93`, `app/metadata/index.py:6`
- Modify: `tests/test_archive.py:28`

**Interfaces:**
- Produces（後続タスクが使う）:
  - `app.archive.ARCHIVE_EXTENSIONS: set[str]`
  - `app.archive.MAX_ARCHIVE_ENTRIES: int`
  - `app.archive.is_archive_file(path: Path) -> bool`
  - `app.archive.list_archive_entries(path: Path) -> list[str]`
  - `app.archive._is_image_entry(name: str) -> bool`
  - `app.archive._finalize(names: list[str], *, keep_order: bool = False) -> list[str]`

- [ ] **Step 1: 現状のテストが通ることを確認（ベースライン）**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py tests/test_scanner.py tests/test_thumbs.py -q`
Expected: 全 PASS。

- [ ] **Step 2: `app/archive.py` を作成**

```python
"""Archive works — ZIP/CBZ (and, in later tasks, RAR/CBR and EPUB) as virtual folders.

An archive file is exposed by ``scan_tree`` as a ``folder`` node whose children
are its image entries; ``/image/<archive>/<inner>`` serves one entry on demand.
Nothing here ever raises for a bad archive: listing yields ``[]`` and reading
yields ``None`` so the API renders an empty message / 404 instead of a 500.
"""

import zipfile
from pathlib import Path

from app.scanner import IMAGE_EXTENSIONS, natural_key

ARCHIVE_EXTENSIONS = {".zip", ".cbz"}

# Zip-bomb / memory guard: never enumerate more than this many entries.
MAX_ARCHIVE_ENTRIES = 2000

# Traversal guard: entries containing these segments are never exposed.
_UNSAFE_PARTS = {"", ".", ".."}


def _is_safe_entry(name: str) -> bool:
    """Reject absolute paths, drive letters, and dot-dot traversal segments."""
    if not name or name.startswith(("/", "\\")):
        return False
    if len(name) >= 2 and name[1] == ":":
        return False
    parts = name.replace("\\", "/").split("/")
    if any(p in _UNSAFE_PARTS for p in parts):
        return False
    return True


def _is_image_entry(name: str) -> bool:
    """True if an archive inner path is an exposable image (no dirs, dotfiles, junk)."""
    if not _is_safe_entry(name):
        return False
    if name.endswith("/"):
        return False
    base = name.rsplit("/", 1)[-1]
    if not base or base.startswith("."):
        return False
    if name.startswith("__MACOSX/") or "/__MACOSX/" in name:
        return False
    return Path(base).suffix.lower() in IMAGE_EXTENSIONS


def is_archive_file(path: Path) -> bool:
    """True if *path* is an existing archive file we can open as a work."""
    return path.is_file() and path.suffix.lower() in ARCHIVE_EXTENSIONS


def _finalize(names: list[str], *, keep_order: bool = False) -> list[str]:
    """Shared tail of every lister: image filter, de-dup (first wins), sort, cap."""
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n not in seen and _is_image_entry(n):
            seen.add(n)
            unique.append(n)
    if not keep_order:
        unique.sort(key=lambda n: (natural_key(n), n))
    return unique[:MAX_ARCHIVE_ENTRIES]


def _zip_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return [info.filename for info in zf.infolist()]
    except Exception:
        return []


def list_archive_entries(archive_path: Path) -> list[str]:
    """Return image entry names inside an archive, in reading order.

    ZIP/CBZ entries are naturally sorted. Unsafe entries are skipped. An
    unreadable or empty archive yields ``[]`` — never raises.
    """
    return _finalize(_zip_names(archive_path))
```

- [ ] **Step 3: `app/scanner.py` から移動分を削除し、`scan_tree` を関数内 import に**

`app/scanner.py` で以下を行う。

1. 先頭の `import zipfile` を削除。
2. `IMAGE_EXTENSIONS` の直後にある以下のブロックを削除（`PDF_EXTENSIONS` は残す）:

```python
# Archive support (Roadmap P1-8): ZIP/CBZ are read on-demand via stdlib
# zipfile; PDF pages are rendered server-side (see app/archive_pdf.py).
# Kept as a separate set — IMAGE_EXTENSIONS flow is untouched.
ARCHIVE_EXTENSIONS = {".zip", ".cbz"}
```
→ 置き換え後:
```python
# Archive support (Roadmap P1-8): ZIP/CBZ/RAR/EPUB live in app/archive.py and
# are read on-demand; PDF pages are rendered server-side (app/archive_pdf.py).
# scan_tree imports app.archive lazily (archive.py imports natural_key from here).
```
3. `_UNSAFE_PARTS` と `MAX_ARCHIVE_ENTRIES` の定義、および `_is_safe_entry`・`_is_image_entry`・`list_archive_entries`・`is_archive_file` の 4 関数を削除する（`is_pdf_file` と `_find_first_image` は残す）。
4. `scan_tree` の本体先頭（`if not base.exists():` の直前）に関数内 import を追加:

```python
    from app.archive import ARCHIVE_EXTENSIONS, list_archive_entries
```

- [ ] **Step 4: 利用側の import を書き換える**

`app/routes.py:23`
```python
from app.archive import is_archive_file, list_archive_entries
from app.scanner import is_pdf_file, list_images, scan_tree
```

`app/routes.py:463`（`_serve_archive_entry` 内）
```python
    from app.archive import _is_image_entry
```

`app/thumbs.py:93`（`_load_archive_source` 内）
```python
    from app.archive import _is_image_entry
    from app.scanner import is_pdf_file
```

`app/metadata/index.py:6`
```python
from app.archive import ARCHIVE_EXTENSIONS
from app.scanner import PDF_EXTENSIONS, natural_key
```

`tests/test_archive.py:28`
```python
from app.archive import list_archive_entries  # noqa: E402
from app.scanner import scan_tree  # noqa: E402
```

- [ ] **Step 5: テストが変わらず通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py tests/test_scanner.py tests/test_thumbs.py tests/test_metadata.py -q`
Expected: 全 PASS。`grep -rn "from app.scanner import" app tests | grep -E "ARCHIVE_EXTENSIONS|list_archive_entries|is_archive_file|_is_image_entry"` が 0 件。

- [ ] **Step 6: コミット**

```bash
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b add app/archive.py app/scanner.py app/routes.py app/thumbs.py app/metadata/index.py tests/test_archive.py
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b commit -S -m "refactor(archive): zip 仮想フォルダ処理を app/archive.py に集約

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `read_archive_entry` を追加し、routes / thumbs の重複 zip 読み出しを置換

**Files:**
- Modify: `app/archive.py`
- Modify: `app/routes.py:454-518`（`_serve_archive_entry`）
- Modify: `app/thumbs.py:88-133`（`_load_archive_source`）
- Test: `tests/test_archive.py`

**Interfaces:**
- Produces:
  - `app.archive.ArchiveToolMissing(Exception)` — 展開ツール不在。routes / thumbs が 501 に変換する。
  - `app.archive.read_archive_entry(archive_path: Path, inner: str) -> bytes | None` — 非画像・不在・破損は `None`。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_archive.py` の `# ── scanner unit ──` セクション末尾（`test_resolve_archive` の後）に追加:

```python
def test_read_archive_entry_zip(archive_tree: Path):
    from app.archive import read_archive_entry

    data = read_archive_entry(archive_tree / "work.zip", "2.png")
    assert data is not None and data[:8] == b"\x89PNG\r\n\x1a\n"
    assert read_archive_entry(archive_tree / "work.zip", "nope.png") is None
    assert read_archive_entry(archive_tree / "work.zip", "readme.txt") is None
    assert read_archive_entry(archive_tree / "work.zip", "../evil.png") is None
    assert read_archive_entry(archive_tree / "missing.zip", "1.png") is None
```

- [ ] **Step 2: 失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py::test_read_archive_entry_zip -q`
Expected: FAIL — `ImportError: cannot import name 'read_archive_entry'`。

- [ ] **Step 3: 実装**

`app/archive.py` の `is_archive_file` の直前に追加:

```python
class ArchiveToolMissing(Exception):
    """The archive format needs an external tool that is not installed."""
```

ファイル末尾に追加:

```python
def _read_zip(path: Path, inner: str) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as zf:
            if inner not in zf.namelist():
                return None
            with zf.open(inner) as fp:
                return fp.read()
    except Exception:
        return None


def read_archive_entry(archive_path: Path, inner: str) -> bytes | None:
    """Return the raw bytes of one image entry, or ``None`` if it cannot be served.

    Raises :class:`ArchiveToolMissing` only when the format is recognised but
    the external extractor is absent — callers turn that into HTTP 501.
    """
    if not _is_image_entry(inner):
        return None
    return _read_zip(archive_path, inner)
```

- [ ] **Step 4: routes の `_serve_archive_entry` を置換**

`app/routes.py` の `_serve_archive_entry` 内、`# ZIP/CBZ: stream the stored bytes on demand` から `raise HTTPException(status_code=404, detail="File not found")`（`data is None` の分岐）までを次に置き換える。`from app.archive import _is_image_entry` の行は `from app.archive import ArchiveToolMissing, _is_image_entry, read_archive_entry` に変更。

```python
    # ZIP/CBZ/RAR/EPUB: read the stored bytes on demand — DOWNLOADS_DIR is untouched.
    if not _is_image_entry(inner):
        raise HTTPException(status_code=404, detail="File not found")
    etag = f'"{stat.st_mtime:.0f}-{stat.st_size}-{inner}"'
    if _not_modified(request, etag, stat.st_mtime):
        return Response(status_code=304)

    try:
        data = await run_in_threadpool(read_archive_entry, archive, inner)
    except ArchiveToolMissing as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
```
残りの `ctype, _ = mimetypes.guess_type(inner)` 以降はそのまま。

- [ ] **Step 5: thumbs の `_load_archive_source` を置換**

`app/thumbs.py` の `_load_archive_source` 内、`if not _is_image_entry(inner):` から最後の `return data, ...` までを次に置き換える。import 行は `from app.archive import ArchiveToolMissing, _is_image_entry, read_archive_entry` に変更。

```python
    if not _is_image_entry(inner):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        data = await run_in_threadpool(read_archive_entry, archive, inner)
    except ArchiveToolMissing as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    return data, stat.st_mtime, float(stat.st_size), f"{archive}|{inner}|{stat.st_mtime}|{stat.st_size}"
```

`import zipfile` が関数内にしか無かったので、削除後に `zipfile` 参照が残っていないことを `grep -n zipfile app/thumbs.py app/routes.py` で確認（0 件）。

- [ ] **Step 6: テスト通過を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py tests/test_thumbs.py -q`
Expected: 全 PASS（`test_serve_zip_entry_and_etag`, `test_serve_nested_archive_entry`, `test_archive_errors_not_500` を含む）。

- [ ] **Step 7: コミット**

```bash
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b add app/archive.py app/routes.py app/thumbs.py tests/test_archive.py
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b commit -S -m "refactor(archive): read_archive_entry で routes/thumbs の zip 読み出しを一本化

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: RAR / CBR 対応（rarfile + 手書き RAR5 フィクスチャ）

**Files:**
- Create: `tests/fixtures/rar5.py`
- Modify: `app/archive.py`
- Modify: `requirements.txt`, `Dockerfile`
- Test: `tests/test_archive.py`

**Interfaces:**
- Consumes: Task 1 の `_finalize`, `_is_image_entry`; Task 2 の `ArchiveToolMissing`, `read_archive_entry`。
- Produces:
  - `tests.fixtures.rar5.make_rar5_store(path: Path, entries: list[tuple[str, bytes]]) -> None`
  - `app.archive.RAR_EXTENSIONS = {".rar", ".cbr"}`

- [ ] **Step 1: 依存を追加**

`requirements.txt` の末尾に追記:
```
rarfile>=4.2
```

`Dockerfile` のランタイムステージ、`FROM python:3.14-slim`（2 つ目）の直後に追加:
```dockerfile
# RAR/CBR extraction backend for rarfile (Debian main; ~7 MB). rarfile
# auto-detects 7z/7zz on PATH — no non-free unrar needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends 7zip \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: RAR5 フィクスチャヘルパーを作成**

`tests/fixtures/rar5.py`（検証済み: `bsdtar`・`rarfile`+`7zz` の双方で読める）:

```python
"""Hand-written RAR5 *store* (uncompressed) archives for tests.

macOS ships no ``rar`` binary and the Debian one is non-free, so instead of
committing an opaque fixture we write the RAR5 container by hand. Only what
rarfile / 7zz / bsdtar need to list and extract a stored file is emitted:
signature, main header, one file header + data per entry, end header.
Format reference: https://www.rarlab.com/technote.htm
"""

import struct
import zlib
from pathlib import Path


def _vint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _header(body: bytes) -> bytes:
    size = _vint(len(body))
    crc = zlib.crc32(size + body) & 0xFFFFFFFF
    return struct.pack("<I", crc) + size + body


def make_rar5_store(path: Path, entries: list[tuple[str, bytes]]) -> None:
    """Write *entries* (``(inner_name, data)``) as a non-solid RAR5 store archive."""
    out = bytearray(b"Rar!\x1a\x07\x01\x00")
    # Main archive header: type=1, header flags=0, archive flags=0.
    out += _header(_vint(1) + _vint(0) + _vint(0))
    for name, data in entries:
        nb = name.encode("utf-8")
        body = (
            _vint(2)  # header type: file
            + _vint(0x0002)  # header flags: data area present
            + _vint(len(data))  # data size
            + _vint(0x0004)  # file flags: CRC32 present
            + _vint(len(data))  # unpacked size
            + _vint(0x20)  # attributes (archive bit)
            + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
            + _vint(0)  # compression info: version 0, method store
            + _vint(1)  # host OS: unix
            + _vint(len(nb))
            + nb
        )
        out += _header(body) + data
    # End of archive header: type=5, header flags=0, end flags=0.
    out += _header(_vint(5) + _vint(0) + _vint(0))
    path.write_bytes(bytes(out))
```

- [ ] **Step 3: 失敗するテストを書く**

`tests/test_archive.py` の import 群（`from app.scanner import scan_tree` の下）に追加:

```python
from tests.fixtures.rar5 import make_rar5_store  # noqa: E402


def _require_rar_tool() -> None:
    """Skip when rarfile has no extractor (unrar / unar / bsdtar / 7z) on PATH."""
    rarfile = pytest.importorskip("rarfile")
    try:
        rarfile.tool_setup()
    except rarfile.RarCannotExec as exc:  # pragma: no cover - env dependent
        pytest.skip(f"no RAR extractor available: {exc}")
```

`archive_tree` フィクスチャの `_make_pdf(tmp_path / "doc.pdf", pages=2)` の直後に追加:

```python
    make_rar5_store(
        tmp_path / "book.cbr",
        [
            ("10.png", (tmp_path / "src_10.png").read_bytes()),
            ("2.png", (tmp_path / "src_2.png").read_bytes()),
            ("ch1/1.png", (tmp_path / "src_1.png").read_bytes()),
            ("../evil.png", b"x"),
            ("readme.txt", b"hi"),
        ],
    )
    (tmp_path / "broken.rar").write_bytes(b"Rar!\x1a\x07\x01\x00garbage")
```

`# ── scanner unit ──` セクションの末尾（`test_read_archive_entry_zip` の後）に追加:

```python
def test_list_rar_entries_sorted_and_safe(archive_tree: Path):
    _require_rar_tool()
    entries = list_archive_entries(archive_tree / "book.cbr")
    assert entries == ["2.png", "10.png", "ch1/1.png"]


def test_list_rar_entries_broken_is_empty(archive_tree: Path):
    assert list_archive_entries(archive_tree / "broken.rar") == []


def test_read_rar_entry(archive_tree: Path):
    _require_rar_tool()
    from app.archive import read_archive_entry

    data = read_archive_entry(archive_tree / "book.cbr", "2.png")
    assert data == (archive_tree / "src_2.png").read_bytes()
    assert read_archive_entry(archive_tree / "book.cbr", "nope.png") is None
    assert read_archive_entry(archive_tree / "book.cbr", "readme.txt") is None
    assert read_archive_entry(archive_tree / "broken.rar", "2.png") is None


def test_read_rar_entry_tool_missing_raises(archive_tree: Path, monkeypatch):
    rarfile = pytest.importorskip("rarfile")
    from app.archive import ArchiveToolMissing, read_archive_entry

    def _boom(self, *_a, **_k):
        raise rarfile.RarCannotExec("no tool")

    monkeypatch.setattr(rarfile.RarFile, "read", _boom)
    with pytest.raises(ArchiveToolMissing):
        read_archive_entry(archive_tree / "book.cbr", "2.png")


def test_scan_tree_lists_rar_as_work(archive_tree: Path):
    _require_rar_tool()
    tree = scan_tree(archive_tree)
    by_name = {n["name"]: n for n in tree}
    assert by_name["book.cbr"]["type"] == "folder"
    assert by_name["book.cbr"]["thumb"] == "2.png"
    assert [c["name"] for c in by_name["book.cbr"]["children"]] == ["2.png", "10.png", "ch1/1.png"]
    assert by_name["broken.rar"]["children"] == []
    assert by_name["broken.rar"]["thumb"] is None
```

- [ ] **Step 4: 失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py -q -k "rar"`
Expected: FAIL — `list_archive_entries(book.cbr)` が `[]`（zip として開けない）、`scan_tree` に `book.cbr` が現れず `KeyError`、`read_archive_entry` が `None`。

- [ ] **Step 5: 実装**

`app/archive.py` の `ARCHIVE_EXTENSIONS` を次に変更:

```python
RAR_EXTENSIONS = {".rar", ".cbr"}
ARCHIVE_EXTENSIONS = {".zip", ".cbz"} | RAR_EXTENSIONS
```

`_zip_names` の直後に追加:

```python
def _rar_names(path: Path) -> list[str]:
    try:
        import rarfile

        with rarfile.RarFile(str(path)) as rf:
            return [info.filename for info in rf.infolist() if not info.is_dir()]
    except Exception:
        return []
```

`list_archive_entries` を次に置き換え:

```python
def list_archive_entries(archive_path: Path) -> list[str]:
    """Return image entry names inside an archive, in reading order.

    ZIP/CBZ and RAR/CBR entries are naturally sorted. Unsafe entries are
    skipped. An unreadable or empty archive yields ``[]`` — never raises.
    """
    suffix = archive_path.suffix.lower()
    if suffix in RAR_EXTENSIONS:
        return _finalize(_rar_names(archive_path))
    return _finalize(_zip_names(archive_path))
```

`_read_zip` の直後に追加:

```python
def _read_rar(path: Path, inner: str) -> bytes | None:
    try:
        import rarfile
    except ImportError as exc:
        raise ArchiveToolMissing("RAR support requires the rarfile package") from exc
    try:
        with rarfile.RarFile(str(path)) as rf:
            if inner not in rf.namelist():
                return None
            return rf.read(inner)
    except rarfile.RarCannotExec as exc:
        raise ArchiveToolMissing(
            "RAR extraction requires 7zip (7zz) or unrar on PATH"
        ) from exc
    except Exception:
        return None
```

`read_archive_entry` の末尾 `return _read_zip(archive_path, inner)` を次に置き換え:

```python
    if archive_path.suffix.lower() in RAR_EXTENSIONS:
        return _read_rar(archive_path, inner)
    return _read_zip(archive_path, inner)
```

- [ ] **Step 6: テスト通過を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py tests/test_scanner.py -q`
Expected: 全 PASS（mac では `bsdtar` バックエンドで動く。skip が出た場合は `rarfile.tool_setup()` の失敗理由を確認する）。

- [ ] **Step 7: コミット**

```bash
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b add app/archive.py requirements.txt Dockerfile tests/fixtures/rar5.py tests/test_archive.py
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b commit -S -m "feat(archive): RAR/CBR を仮想フォルダとして読めるように（rarfile + 7zip）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: EPUB 対応（spine 順、フォールバック付き）

**Files:**
- Modify: `app/archive.py`
- Test: `tests/test_archive.py`

**Interfaces:**
- Consumes: Task 1 の `_finalize`, `_zip_names`; Task 2 の `_read_zip`。
- Produces: `app.archive.EPUB_EXTENSIONS = {".epub"}`、`app.archive._epub_spine_images(zf: zipfile.ZipFile) -> list[str]`。

- [ ] **Step 1: EPUB フィクスチャビルダーと失敗するテストを書く**

`tests/test_archive.py` の `_make_pdf` の直後に追加（`from urllib.parse import quote` を import 群に追加）:

```python
def _make_epub(
    path: Path,
    pages: list[tuple[str, str]],
    png: bytes,
    *,
    svg: bool = False,
    broken: bool = False,
) -> None:
    """Write a fixed-layout EPUB. *pages* is ``[(xhtml_name, image_name)]`` in spine order.

    XHTML lives in ``OEBPS/Text/``, images in ``OEBPS/Images/`` so every ``src``
    is a ``../`` relative path. Image names are URL-encoded in the markup so
    ``%20`` decoding is exercised. ``svg=True`` wraps the image in an SVG
    ``<image xlink:href>`` (the common fixed-layout manga pattern).
    ``broken=True`` omits container.xml and the OPF so the reader must fall
    back to natural order.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        if not broken:
            zf.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0"?>'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
        items: list[str] = []
        refs: list[str] = []
        for i, (xhtml, img) in enumerate(pages):
            items.append(f'<item id="pg{i}" href="Text/{xhtml}" media-type="application/xhtml+xml"/>')
            items.append(f'<item id="im{i}" href="Images/{quote(img)}" media-type="image/png"/>')
            refs.append(f'<itemref idref="pg{i}"/>')
            src = "../Images/" + quote(img)
            if svg:
                body = (
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 40 60">'
                    f'<image width="40" height="60" xlink:href="{src}"/></svg>'
                )
            else:
                body = f'<img src="{src}" alt=""/>'
            zf.writestr(
                f"OEBPS/Text/{xhtml}",
                '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                f"<head><title>p</title></head><body>{body}</body></html>",
            )
            zf.writestr(f"OEBPS/Images/{img}", png)
        if not broken:
            zf.writestr(
                "OEBPS/content.opf",
                '<?xml version="1.0"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">'
                f'<metadata/><manifest>{"".join(items)}</manifest>'
                f'<spine>{"".join(refs)}</spine></package>',
            )
```

`archive_tree` フィクスチャの `(tmp_path / "broken.rar").write_bytes(...)` の直後に追加:

```python
    png = (tmp_path / "src_1.png").read_bytes()
    # Spine order (cover, p1, p2) deliberately disagrees with filename order.
    _make_epub(
        tmp_path / "manga.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png"), ("a.xhtml", "m_p2.png")],
        png,
    )
    _make_epub(
        tmp_path / "svg.epub",
        [("p1.xhtml", "page 1.png"), ("p2.xhtml", "page 2.png")],
        png,
        svg=True,
    )
    _make_epub(
        tmp_path / "broken.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png")],
        png,
        broken=True,
    )
```

`# ── scanner unit ──` セクション末尾に追加:

```python
def test_epub_entries_follow_spine_order(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "manga.epub")
    assert entries == [
        "OEBPS/Images/z_cover.png",
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/m_p2.png",
    ]


def test_epub_entries_svg_wrapper_and_percent_encoding(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "svg.epub")
    assert entries == ["OEBPS/Images/page 1.png", "OEBPS/Images/page 2.png"]


def test_epub_without_opf_falls_back_to_natural_order(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "broken.epub")
    assert entries == ["OEBPS/Images/a_p1.png", "OEBPS/Images/z_cover.png"]


def test_read_epub_entry(archive_tree: Path):
    from app.archive import read_archive_entry

    data = read_archive_entry(archive_tree / "manga.epub", "OEBPS/Images/a_p1.png")
    assert data == (archive_tree / "src_1.png").read_bytes()
    assert read_archive_entry(archive_tree / "manga.epub", "OEBPS/Text/a.xhtml") is None


def test_scan_tree_lists_epub_with_cover_thumb(archive_tree: Path):
    tree = scan_tree(archive_tree)
    by_name = {n["name"]: n for n in tree}
    assert by_name["manga.epub"]["type"] == "folder"
    assert by_name["manga.epub"]["thumb"] == "OEBPS/Images/z_cover.png"
    assert len(by_name["manga.epub"]["children"]) == 3
```

- [ ] **Step 2: 失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py -q -k "epub"`
Expected: FAIL — `.epub` が `ARCHIVE_EXTENSIONS` に無いので `scan_tree` に現れず `KeyError`、`list_archive_entries` が `[]`。

- [ ] **Step 3: 実装**

`app/archive.py` の import に追加:

```python
import posixpath
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit
```

定数を次に変更:

```python
RAR_EXTENSIONS = {".rar", ".cbr"}
EPUB_EXTENSIONS = {".epub"}
ARCHIVE_EXTENSIONS = {".zip", ".cbz"} | RAR_EXTENSIONS | EPUB_EXTENSIONS

_NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_NS_OPF = "{http://www.idpf.org/2007/opf}"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
```

`_rar_names` の直後に追加:

```python
def _epub_resolve(base_dir: str, href: str) -> str:
    """Resolve an (possibly percent-encoded) href relative to *base_dir* into a zip name."""
    rel = unquote(urlsplit(href).path)
    if not rel:
        return ""
    joined = posixpath.join(base_dir, rel) if base_dir else rel
    return posixpath.normpath(joined)


def _epub_spine_images(zf: zipfile.ZipFile) -> list[str]:
    """Image zip names in spine order (document order within each page).

    Raises on a missing/invalid container.xml or OPF — the caller falls back
    to natural order. A single unreadable page document is skipped.
    """
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(f".//{_NS_CONTAINER}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB container has no rootfile")
    opf_path = posixpath.normpath(rootfile.get("full-path"))
    opf_dir = posixpath.dirname(opf_path)
    opf = ET.fromstring(zf.read(opf_path))
    manifest = {
        item.get("id"): item.get("href")
        for item in opf.iter(f"{_NS_OPF}item")
        if item.get("id") and item.get("href")
    }
    out: list[str] = []
    for ref in opf.iter(f"{_NS_OPF}itemref"):
        href = manifest.get(ref.get("idref") or "")
        if not href:
            continue
        doc_path = _epub_resolve(opf_dir, href)
        try:
            root = ET.fromstring(zf.read(doc_path))
        except Exception:
            continue
        doc_dir = posixpath.dirname(doc_path)
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
            if tag == "img":
                src = el.get("src")
            elif tag == "image":
                src = el.get(_XLINK_HREF) or el.get("href")
            else:
                continue
            if src:
                out.append(_epub_resolve(doc_dir, src))
    return out


def _epub_entries(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            raw = [info.filename for info in zf.infolist()]
            try:
                spine = _epub_spine_images(zf)
            except Exception:
                spine = []
    except Exception:
        return []
    present = set(raw)
    ordered = _finalize([n for n in spine if n in present], keep_order=True)
    if ordered:
        return ordered
    return _finalize(raw)
```

`list_archive_entries` を次に置き換え:

```python
def list_archive_entries(archive_path: Path) -> list[str]:
    """Return image entry names inside an archive, in reading order.

    ZIP/CBZ and RAR/CBR entries are naturally sorted; EPUB entries follow the
    OPF spine (falling back to natural order when the package is unreadable).
    Unsafe entries are skipped. An unreadable or empty archive yields ``[]``
    — never raises.
    """
    suffix = archive_path.suffix.lower()
    if suffix in RAR_EXTENSIONS:
        return _finalize(_rar_names(archive_path))
    if suffix in EPUB_EXTENSIONS:
        return _epub_entries(archive_path)
    return _finalize(_zip_names(archive_path))
```

`read_archive_entry` は EPUB を zip として読むので変更不要。

- [ ] **Step 4: テスト通過を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py tests/test_scanner.py -q`
Expected: 全 PASS。

- [ ] **Step 5: コミット**

```bash
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b add app/archive.py tests/test_archive.py
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b commit -S -m "feat(archive): 固定レイアウト EPUB を spine 順の仮想フォルダとして読めるように

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: API レベルの検証（folders / images / image / thumb / metadata / 501）

**Files:**
- Modify: `app/metadata/index.py:224`（コメントのみ）
- Test: `tests/test_archive.py`

**Interfaces:**
- Consumes: Task 3・4 の `archive_tree` フィクスチャ（`book.cbr`, `manga.epub`, `svg.epub`, `broken.rar`, `broken.epub`）。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_archive.py` の `# ── API level ──` セクション末尾（`test_regular_folder_unaffected` の後）に追加:

```python
def test_api_folders_shows_rar_and_epub(client):
    _require_rar_tool()
    by_name = {n["name"]: n for n in client.get("/api/folders").json()}
    assert by_name["book.cbr"]["thumb"] == "2.png"
    assert by_name["manga.epub"]["thumb"] == "OEBPS/Images/z_cover.png"
    assert by_name["broken.rar"]["thumb"] is None
    assert by_name["broken.epub"]["thumb"] == "OEBPS/Images/a_p1.png"


def test_api_images_rar_and_epub(client):
    _require_rar_tool()
    res = client.get("/api/images", params={"folder": "book.cbr"})
    assert res.status_code == 200 and res.json() == ["2.png", "10.png", "ch1/1.png"]
    res = client.get("/api/images", params={"folder": "manga.epub"})
    assert res.status_code == 200
    assert res.json() == [
        "OEBPS/Images/z_cover.png",
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/m_p2.png",
    ]
    res = client.get("/api/images", params={"folder": "broken.rar"})
    assert res.status_code == 200 and res.json() == []


def test_serve_rar_entry_and_errors(client, archive_tree: Path):
    _require_rar_tool()
    res = client.get("/image/book.cbr/ch1/1.png")
    assert res.status_code == 200
    assert res.content == (archive_tree / "src_1.png").read_bytes()
    assert res.headers.get("ETag") and res.headers.get("Accept-Ranges") == "bytes"
    res2 = client.get("/image/book.cbr/ch1/1.png", headers={"If-None-Match": res.headers["ETag"]})
    assert res2.status_code == 304
    assert client.get("/image/book.cbr/nope.png").status_code == 404
    assert client.get("/image/book.cbr/readme.txt").status_code == 404
    assert client.get("/image/broken.rar/2.png").status_code == 404


def test_serve_epub_entry_percent_encoded(client, archive_tree: Path):
    res = client.get("/image/svg.epub/OEBPS/Images/page%201.png")
    assert res.status_code == 200
    assert res.content == (archive_tree / "src_1.png").read_bytes()
    assert client.get("/image/manga.epub/OEBPS/Text/a.xhtml").status_code == 404


def test_serve_rar_entry_tool_missing_is_501(client, monkeypatch):
    rarfile = pytest.importorskip("rarfile")

    def _boom(self, *_a, **_k):
        raise rarfile.RarCannotExec("no tool")

    monkeypatch.setattr(rarfile.RarFile, "read", _boom)
    res = client.get("/image/book.cbr/2.png")
    assert res.status_code == 501
    assert "7zip" in res.json()["detail"]


def test_thumb_from_rar_and_epub(client):
    _require_rar_tool()
    res = client.get("/thumb/book.cbr/2.png?w=200")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/")
    res = client.get("/thumb/manga.epub/OEBPS/Images/z_cover.png?w=200")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/")


def test_metadata_indexes_rar_and_epub(tmp_path: Path):
    make_rar5_store(tmp_path / "book.cbr", [("1.png", b"fake")])
    _make_epub(tmp_path / "m.epub", [("a.xhtml", "1.png")], b"fake")
    idx = MetadataIndex()
    idx.rebuild(tmp_path)
    paths = {c.folder_path for c in idx.comics}
    assert {"book.cbr", "m.epub"} <= paths
```

- [ ] **Step 2: 実行して結果を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py -q`
Expected: Task 1〜4 が正しければ全 PASS。失敗があれば当該タスクの実装を直す（テストの期待値を緩めない）。`test_thumb_from_rar_and_epub` が失敗する場合は `app/thumbs.py:_load_archive_source` の import 置換（Task 2 Step 5）を再確認する。

- [ ] **Step 3: metadata のコメント更新**

`app/metadata/index.py:224` 付近のコメント `# Archive works (ZIP/CBZ/PDF) are indexed as comics by name.` を
`# Archive works (ZIP/CBZ/RAR/CBR/EPUB/PDF) are indexed as comics by name.` に変更。

- [ ] **Step 4: 関連スイート全体を実行**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py tests/test_scanner.py tests/test_thumbs.py tests/test_metadata.py tests/test_traversal.py -q`
Expected: 全 PASS。

- [ ] **Step 5: コミット**

```bash
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b add tests/test_archive.py app/metadata/index.py
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b commit -S -m "test(archive): RAR/EPUB の API・サムネイル・索引・501 を検証

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: ドキュメント更新と dev 環境での実機確認

**Files:**
- Modify: `CLAUDE.md`（「The folder tree is the tricky part」セクション）
- Create（一時、コミットしない）: スクラッチパッド配下 `sample_downloads/` にサンプル書庫

- [ ] **Step 1: CLAUDE.md に形式の説明を追記**

`CLAUDE.md` の「Known gap, covered by a characterization test:」の段落の直前に追加:

```markdown
**Archives are virtual folders.** `app/archive.py` owns `ARCHIVE_EXTENSIONS` (`.zip .cbz .rar .cbr .epub`): `scan_tree` turns each archive file into a `folder` node whose children are its image entries, `/api/images` lists them, and `/image/<archive>/<inner>` serves one entry on demand via `read_archive_entry` (never a 500 — bad archives list as `[]`, bad entries 404). ZIP/RAR entries sort naturally; EPUB follows the OPF spine (`<img>` / SVG `<image>` per page, `%20` decoded) and falls back to natural order when the package is unreadable. RAR needs an extractor on PATH — the Docker image installs Debian `7zip`, macOS uses its built-in `bsdtar`; without one, listing is empty and serving answers 501. PDF stays in `app/archive_pdf.py`. Test fixtures write RAR5 by hand (`tests/fixtures/rar5.py`), so no `rar` binary is needed to run the suite.
```

- [ ] **Step 2: サンプル書庫を生成**

スクラッチパッド `<worktree>/scratchpad/make_samples.py` を作り実行:

```python
import sys
from pathlib import Path

repo = Path("/Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b")
sys.path.insert(0, str(repo))
from tests.fixtures.rar5 import make_rar5_store  # noqa: E402
from tests.test_archive import _make_epub, _make_png  # noqa: E402

out = Path(__file__).parent / "sample_downloads"
out.mkdir(exist_ok=True)
_make_png(out / "_src.png")
png = (out / "_src.png").read_bytes()
(out / "_src.png").unlink()
make_rar5_store(out / "Sample RAR.cbr", [(f"{i:03d}.png", png) for i in range(1, 6)])
_make_epub(out / "Sample EPUB.epub", [(f"p{i}.xhtml", f"page {i}.png") for i in range(1, 4)], png)
print(out)
```

Run: `uv run --with-requirements /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b/requirements-dev.txt --with pytest python <scratchpad>/make_samples.py`
Expected: `sample_downloads/` に `.cbr` と `.epub` が 1 つずつ。

- [ ] **Step 3: dev 環境を起動して確認**

```bash
DOWNLOADS_HOST_DIR=<scratchpad>/sample_downloads /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b/dev.sh up rar-epub --local --port 8091
```

起動後:

```bash
curl -s http://127.0.0.1:8091/api/folders | python3 -c 'import json,sys; [print(n["name"], n["thumb"], len(n["children"] or [])) for n in json.load(sys.stdin)]'
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' 'http://127.0.0.1:8091/image/Sample%20RAR.cbr/001.png'
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' 'http://127.0.0.1:8091/image/Sample%20EPUB.epub/OEBPS/Images/page%201.png'
```

Expected: folders に両書庫が `children` 5 と 3 で出る。両 `/image/...` が `200 image/png`（RAR は Docker 内の `7zz` で展開されている）。

ブラウザ確認: 内蔵ブラウザで `http://127.0.0.1:8091/` を開き、`Sample RAR.cbr` のタイルをクリックしてページ送りができること、`Sample EPUB.epub` も同様に開けることを確認する。

- [ ] **Step 4: dev 環境を落とす**

```bash
/Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b/dev.sh down rar-epub
```

- [ ] **Step 5: 全テストスイート実行**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -x -n 4`
Expected: 全 PASS（Playwright を含む。Chromium 未導入なら先に `uv run --with playwright playwright install chromium`）。

- [ ] **Step 6: コミット**

```bash
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b add CLAUDE.md
git -C /Users/example-user/Documents/GitHub/bookq/.claude/worktrees/viewer-archive-formats-b0459b commit -S -m "docs: アーカイブ仮想フォルダ（zip/rar/epub）の説明を CLAUDE.md に追記

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
