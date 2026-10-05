#!/usr/bin/env python3
"""
video_debug.py — ブラウザ操作を動画で捉えて Muse Spark 1.3 に診断させるハーネス

Phase 0 で確定した迂回路をコード化:
  動画(.webm/.mp4) → ffmpeg fps=2-3 → 複数PNG → opencode run -f で spark に時系列診断

使い方:
  # 1. 既存の動画を診断 (最も手軽)
  uv run python scripts/video_debug.py diagnose /tmp/capture.webm --fps 2 --prompt "ページめくりで黒い隙間が出るか見て"

  # 2. bookq を自動で録画して診断 (Playwright)
  uv run python scripts/video_debug.py capture --url http://127.0.0.1:8080 --scenario browse_then_open
  # → /tmp/bookq-*.webm が生成されるので、そのまま diagnose に渡す

  # 3. フレームだけ欲しい
  uv run python scripts/video_debug.py frames /tmp/capture.webm --fps 2 --out /tmp/frames

前提:
  - ffmpeg が PATH にある (brew install ffmpeg)
   - opencode がインストール済み (opencode run -m opencode-go/muse-spark-1.3-contributor)
  - capture は playwright が必要 (uv run --with playwright)

ADR: docs/adr/0001-video-debugging-via-frame-extraction.md
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "opencode-go/muse-spark-1.3-contributor"
DEFAULT_FPS = 2


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)
    return subprocess.run(cmd, check=True, **kwargs)


def extract_frames(video: Path, out_dir: Path, fps: int = DEFAULT_FPS) -> list[Path]:
    """ffmpeg で動画からフレーム抽出。out_dir/frame_001.png ... を返す。"""
    if not video.exists():
        raise FileNotFoundError(f"video not found: {video}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 既存フレームを掃除
    for p in out_dir.glob("frame_*.png"):
        p.unlink()

    pattern = str(out_dir / "frame_%03d.png")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        f"fps={fps}",
        pattern,
    ]
    result = run(cmd, capture_output=True, text=True)
    # ffmpeg は warning を stderr に出すので、失敗だけ見る
    frames = sorted(out_dir.glob("frame_*.png"))
    if not frames:
        print(result.stderr[-2000:], file=sys.stderr)
        raise RuntimeError(f"no frames extracted from {video} (fps={fps})")
    print(f"extracted {len(frames)} frames @ {fps}fps → {out_dir}", file=sys.stderr)
    for f in frames:
        print(f"  {f} ({f.stat().st_size} bytes)", file=sys.stderr)
    return frames


def diagnose(
    video: Path,
    prompt: str,
    fps: int = DEFAULT_FPS,
    model: str = DEFAULT_MODEL,
    keep_frames: bool = False,
) -> int:
    """
    動画 → フレーム抽出 → opencode run -f で spark に投げる。
    opencode の file reader は画像のみ対応なので、動画バイナリは渡さない。
    """
    video = video.resolve()
    if not video.exists():
        print(f"video not found: {video}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="bookq-vframes-") as tmp:
        tmp_path = Path(tmp)
        frames = extract_frames(video, tmp_path, fps=fps)

        # フレーム数制限: spark の context は大きいが、10枚超はコスト増。デフォルトは fps=2 なので
        # 5秒動画なら10枚。超えたら警告して先頭/末尾を優先する戦略もありだが、まずは全枚数で投げる。
        if len(frames) > 12:
            print(
                f"warning: {len(frames)} frames is many — consider --fps 1 or shorter video",
                file=sys.stderr,
            )

        # opencode run コマンドを組み立て
        cmd = ["opencode", "run", "-m", model]
        for f in frames:
            cmd.extend(["-f", str(f)])
        cmd.extend(["--format", "json", prompt])

        print(f"\n=== diagnose via {model} ({len(frames)} frames, fps={fps}) ===", file=sys.stderr)
        print(f"prompt: {prompt}", file=sys.stderr)
        print(f"video: {video} → {len(frames)} frames", file=sys.stderr)
        print("", file=sys.stderr)

        # opencode の JSON ストリームをそのまま流す + text だけ抜粋も表示
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        text_parts: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    if evt.get("type") == "text":
                        t = evt.get("part", {}).get("text") or evt.get("text", "")
                        if t:
                            text_parts.append(t)
                            print(t)
                    elif evt.get("type") == "error":
                        print(f"[error] {evt}", file=sys.stderr)
                except json.JSONDecodeError:
                    print(line)
            proc.wait(timeout=120)
        finally:
            if proc.poll() is None:
                proc.terminate()

        # keep_frames が指定されたらコピー
        if keep_frames:
            dest = Path.cwd() / f"frames-{video.stem}"
            dest.mkdir(exist_ok=True)
            # tmp は消えるので再抽出
            extract_frames(video, dest, fps=fps)
            print(f"frames kept at {dest}", file=sys.stderr)

        if proc.returncode != 0:
            print(f"opencode run failed: exit {proc.returncode}", file=sys.stderr)
            if proc.stderr:
                print(proc.stderr.read()[-3000:], file=sys.stderr)
            return proc.returncode or 1

        # サマリ
        print("\n--- diagnose done ---", file=sys.stderr)
        return 0


def capture(
    url: str,
    scenario: str = "browse_then_open",
    out: Path | None = None,
    headed: bool = False,
) -> Path:
    """
    Playwright で bookq を操作しながら recordVideo で録画。
    scenario:
      - browse_then_open: 一覧表示 → 最初のタイルをクリック→ビューアで1ページめくる
      - scroll_only: 一覧を上下スクロールのみ
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. Run: uv run --with playwright python scripts/video_debug.py ...", file=sys.stderr)
        sys.exit(1)

    if out is None:
        out = Path(tempfile.gettempdir()) / f"bookq-{scenario}.webm"
    out = out.resolve()
    # recordVideo は context 単位なので、一時ディレクトリに保存してから移動
    with tempfile.TemporaryDirectory(prefix="bookq-vcap-") as tmp:
        tmp_path = Path(tmp)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                record_video_dir=str(tmp_path),
                record_video_size={"width": 390, "height": 844},
            )
            page = context.new_page()
            print(f"goto {url}", file=sys.stderr)
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(".tile", timeout=5000)
            except Exception:
                print("no .tile found — page may be login or empty", file=sys.stderr)
                page.wait_for_timeout(1000)

            if scenario == "browse_then_open":
                # 最初のタイルをクリック → ビューア遷移
                tiles = page.locator(".tile")
                if tiles.count() > 0:
                    print(f"click first tile (count={tiles.count()})", file=sys.stderr)
                    tiles.first.click()
                    page.wait_for_timeout(1500)
                    # ビューアのページめくり: 左タップゾーンをクリック (次ページ)
                    # bookq は左45%が「次」、右が「前」
                    try:
                        page.mouse.click(80, 400)
                        page.wait_for_timeout(800)
                        page.mouse.click(80, 400)
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
                else:
                    page.wait_for_timeout(2000)

            elif scenario == "scroll_only":
                page.mouse.wheel(0, 600)
                page.wait_for_timeout(800)
                page.mouse.wheel(0, -600)
                page.wait_for_timeout(800)

            else:
                print(f"unknown scenario: {scenario}", file=sys.stderr)
                page.wait_for_timeout(2000)

            context.close()
            browser.close()

        # recordVideo は context.close() 後にファイルが確定する。tmp 内の .webm を探す
        vids = list(tmp_path.glob("*.webm"))
        if not vids:
            raise RuntimeError(f"no video recorded in {tmp_path}")
        # 最新のものを out にコピー
        src = max(vids, key=lambda p: p.stat().st_mtime)
        out.parent.mkdir(parents=True, exist_ok=True)
        # ffmpeg で yuv420p に再エンコードしておくと後段が安定
        run(["ffmpeg", "-y", "-i", str(src), "-pix_fmt", "yuv420p", str(out)], capture_output=True)
        print(f"video saved: {out} ({out.stat().st_size} bytes)", file=sys.stderr)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="bookq video debugging harness (spark 1.3 + frames)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # diagnose
    p_diag = sub.add_parser("diagnose", help="動画→フレーム→spark診断")
    p_diag.add_argument("video", type=Path, help="入力動画 (.webm/.mp4)")
    p_diag.add_argument("--fps", type=int, default=DEFAULT_FPS, help=f"抽出fps (default: {DEFAULT_FPS})")
    p_diag.add_argument("--prompt", type=str, default="この動画でブラウザに何が起きたか、時系列で正確に説明して。不具合があれば指摘して。", help="spark へのプロンプト")
    p_diag.add_argument("--model", type=str, default=DEFAULT_MODEL, help=f"モデル (default: {DEFAULT_MODEL})")
    p_diag.add_argument("--keep-frames", action="store_true", help="抽出フレームをカレントに残す")

    # frames
    p_frames = sub.add_parser("frames", help="動画からフレーム抽出のみ")
    p_frames.add_argument("video", type=Path, help="入力動画")
    p_frames.add_argument("--fps", type=int, default=DEFAULT_FPS)
    p_frames.add_argument("--out", type=Path, default=Path("/tmp/bookq-frames"), help="出力ディレクトリ")

    # capture
    p_cap = sub.add_parser("capture", help="Playwrightで録画")
    p_cap.add_argument("--url", type=str, required=True, help="bookq URL (例: http://127.0.0.1:8080)")
    p_cap.add_argument("--scenario", type=str, default="browse_then_open", choices=["browse_then_open", "scroll_only"])
    p_cap.add_argument("--out", type=Path, default=None, help="出力 .webm パス")
    p_cap.add_argument("--headed", action="store_true", help="headless=false で目視確認")

    args = ap.parse_args()

    if args.cmd == "diagnose":
        sys.exit(diagnose(args.video, args.prompt, fps=args.fps, model=args.model, keep_frames=args.keep_frames))
    elif args.cmd == "frames":
        frames = extract_frames(args.video.resolve(), args.out.resolve(), fps=args.fps)
        print(f"done: {len(frames)} frames in {args.out}")
    elif args.cmd == "capture":
        out = capture(args.url, scenario=args.scenario, out=args.out, headed=args.headed)
        print(f"capture done: {out}")
        print(f"next: uv run python scripts/video_debug.py diagnose {out} --fps 2")


if __name__ == "__main__":
    main()
