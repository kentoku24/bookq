# RAR / EPUB アーカイブ対応 設計

作成日: 2026-09-21

## 背景と目的

ビューアは PR #104 以降、ZIP / CBZ / PDF を「仮想フォルダ」として読める。
アーカイブファイルは `scan_tree` で `type: "folder"` ノードになり、中の画像が
`type: "image"` の子ノードになる。配信は `/image/<archive>/<inner>` で、
`_resolve_archive`（`app/routes.py`）が先頭一致でアーカイブファイルを見つけて
オンデマンドに 1 エントリを返す。フロントエンドは通常フォルダと区別しない。

漫画アーカイブで残る主要形式は RAR / CBR と EPUB である。本設計はこの 2 形式を
同じ仮想フォルダ方式に乗せ、フロントエンド・URL ハッシュ・オフライン保存を
無変更のまま読めるようにする。

## スコープ

対象:

- RAR4 / RAR5 の `.rar` `.cbr`（非暗号化、単一ボリューム）
- 固定レイアウト（画像ベース）の `.epub`

対象外（今回はやらない）:

- 暗号化 RAR、マルチボリューム RAR（`.part1.rar` など）
- テキスト中心 EPUB のレンダリング（画像が無ければ空表示になる）
- 7z / tar
- 展開結果のディスクキャッシュ（PDF と同じくページ毎オンデマンド）

## 全体構成

### 新モジュール `app/archive.py`

アーカイブ形式の判別・エントリ列挙・1 エントリ読み出しをここに集約する。

```python
ARCHIVE_EXTENSIONS = {".zip", ".cbz", ".rar", ".cbr", ".epub"}

def list_archive_entries(path: Path) -> list[str]: ...
def read_archive_entry(path: Path, inner: str) -> bytes | None: ...
```

- `list_archive_entries` は拡張子で分岐する。
  - zip / cbz: 現行どおり `zipfile` で列挙し `natural_key` で自然順。
  - rar / cbr: `rarfile.RarFile.infolist()` で列挙し自然順。
  - epub: spine 順（後述）。
  - どの形式でも `_is_image_entry` の安全フィルタと `MAX_ARCHIVE_ENTRIES`（2000）
    を適用し、重複名は先勝ちで除去する。例外時は `[]` を返し、決して raise しない。
- `read_archive_entry` は zip / epub は `zipfile`、rar は `rarfile.RarFile.read()`。
  エントリ不在・破損・例外時は `None`。
- モジュール依存は一方向にする。`app/archive.py` が `app/scanner.py` から
  `natural_key`・`IMAGE_EXTENSIONS` をトップレベル import する。scanner 側は
  `scan_tree` 内で `from app import archive` を関数内 import する（`archive_pdf` を
  既に同じ方法で読んでいるのに揃える）。これで循環 import は起きない。
- `_is_safe_entry`、`_is_image_entry`、`list_archive_entries`、`is_archive_file`、
  `ARCHIVE_EXTENSIONS`、`MAX_ARCHIVE_ENTRIES` は scanner から archive.py へ移す。
  `routes.py`、`metadata/index.py`、`thumbs.py`、`tests/test_archive.py` の import 元を
  `app.archive` に書き換える。scanner には再エクスポートを残さない（import 元が
  2 つある状態を作らない）。`PDF_EXTENSIONS` と `is_pdf_file` は scanner に残す。

### 既存コードの整理

現在、zip の 1 エントリ読み出しが `routes.py:_serve_archive_entry` と
`thumbs.py:_load_archive_source` に同じコードで 2 箇所ある。両方を
`read_archive_entry` 呼び出しに置き換える。RAR 分岐を 2 箇所に書かないための整理で、
振る舞いは変えない。

### 変更が不要な箇所

- `scan_tree`、`/api/images`、`_resolve_archive`、`metadata/index.py` の
  `_ARCHIVE_SUFFIXES` は `ARCHIVE_EXTENSIONS` を参照しているだけなので、定数拡張だけで
  新形式が仮想フォルダとして流れる。
- フロントエンド（`app/static/*.js`）は無変更。

## RAR

- pip `rarfile>=4.2` を使う。rarfile はヘッダ解析を自前で行い、展開だけ外部ツールに
  委譲する。ツール探索は rarfile 任せ（unrar → unar → bsdtar → 7z / 7zz の順）。
- Docker ランタイムには Debian 13 main の `7zip`（バイナリ名は `7z`）を入れる。非 free の
  `unrar` は使わない。一覧表示と store（無圧縮）エントリの読み出しはツールなしでどの
  ホストでも動くが、圧縮エントリの読み出しには 7-Zip か unrar が要る。mac では
  `brew install sevenzip`（`7zz`）を使う — rarfile 4.5 同梱の `bsdtar` バックエンドは
  自前の argv（`-f -- <archive>`）を誤解釈して壊れたデータを返すため、実用不可。
- 失敗時の扱い（500 を出さない方針は現行踏襲）:
  - 破損・暗号化・マルチボリュームなど rarfile が例外を投げる場合: 列挙は `[]`、
    読み出しは `None`（→ 404）。
  - バックエンドツールが無い（`rarfile.RarCannotExec`）場合でも、一覧表示は空に
    ならない — rarfile はヘッダ解析を自前の Python で行うため、ツールなしでも
    `namelist()` は動き、store（無圧縮）エントリはそのままツールなしで読める。
    圧縮されたエントリを実際に読もうとしたときだけ **501** と導入ヒント
    （「RAR の展開には 7-Zip (7z) か unrar が必要」）を返す。判別は
    `read_archive_entry` が `RarCannotExec` を捕捉して専用の例外
    `ArchiveToolMissing` を投げ直し、routes / thumbs がそれを 501 に変換する。

## EPUB

EPUB は zip なので、`zipfile` + `xml.etree.ElementTree` だけで解析する。

1. `META-INF/container.xml` の `rootfile/@full-path` から OPF パスを得る。
2. OPF の `manifest/item`（id → href, media-type）と `spine/itemref`（idref の順）を読む。
3. spine 順に各 XHTML を開き、`<img src>` と SVG `<image href>` / `<image xlink:href>`
   を文書順に抽出する（固定レイアウト漫画は SVG ラッパーが多い）。
4. href は XHTML の位置からの相対パスとして解決し（`posixpath.normpath`）、
   URL エンコードされていれば `unquote` し、フラグメント・クエリを落として
   zip 内エントリ名に正規化する。
5. 重複を除去し、`_is_image_entry` でフィルタする。

結果が 1 枚以上なら spine 順を採用する。OPF 欠損・XML 破損・画像ゼロの場合は
「zip 内の画像を自然順」にフォールバックする（zip / cbz と同じ経路）。
テキスト中心 EPUB はフォールバック後も空になりうる。それは仕様とし、空表示にする。

返すのは実際の zip エントリ名なので、配信は zip 経路がそのまま使える。
並び順だけが zip と異なる。

## 依存とデプロイ

- `requirements.txt`: `rarfile>=4.2` を追加。`requirements-dev.txt` は
  `-r requirements.txt` を含むので追記不要。
- `Dockerfile` ランタイムステージ:
  `apt-get update && apt-get install -y --no-install-recommends 7zip && rm -rf /var/lib/apt/lists/*`。
- `CLAUDE.md` の該当箇所（アーカイブ形式）を更新する。

## テスト

`tests/test_archive.py` に追加する。

- RAR フィクスチャはバイナリをコミットせず、`tests/fixtures/rar5.py` の
  `make_rar5_store(path, [(name, bytes), ...])` でテスト内に生成する。これは RAR5 の
  無圧縮（store）書庫をヘッダから手書きするヘルパーで、`bsdtar`（mac）・`7z`（Docker）・
  `rarfile` の 3 経路で読めることを設計時に検証済み。mac に rar 作成ツールが無くても
  スイートが完結し、外部バイナリのダウンロードも不要になる。
  内容は極小 PNG 3 枚（`10.png`, `2.png`, `ch1/1.png`。自然順 `1 < 2 < 10` を検証できる）
  と `../evil.png`（安全フィルタ検証）と `readme.txt`。
- RAR テストは `rarfile.tool_setup()` が失敗する環境では `pytest.skip`。
- EPUB フィクスチャはテスト内で `zipfile` から生成する:
  - (a) spine 順がファイル名順と逆の正常 EPUB（`<img>` 型）
  - (b) SVG `<image>` ラッパー型、相対パスと URL エンコードを含む
  - (c) OPF 欠損の壊れ EPUB（フォールバックで自然順になることを検証）
- 検証項目: 列挙順、安全フィルタ、`/api/folders` への出現、
  `/image/<archive>/<inner>` の配信と 404、サムネイル経路（`thumbs.py`）、
  メタデータ検索対象化、ツール不在時の 501。
- 既存の zip / pdf テストと `tests/test_scanner.py` が全通過することを確認する。
- 最後に `./dev.sh up` で実環境の Docker イメージ（`7z` 入り）で RAR と EPUB を
  開けることを確認し、`./dev.sh down` する。
