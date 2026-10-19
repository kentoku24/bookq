# 低スペック Android 向け描画性能の計測手順

この手順は、bookq のフォルダ一覧（サムネイル）とビューアのページ送りについて、画像のロード／デコード待ちと、ブラウザの描画負荷を切り分けるためのものです。Mac の Chrome は比較・原因調査用であり、実機 Android の GPU、熱制御、メモリ圧迫を正確に再現するものではありません。

## 1. 対象シナリオ

同じデータ・同じ操作を使い、各シナリオを最低 3 回測定して中央値を比較します。

1. **一覧の初期表示**: フォルダ一覧を開き、サムネイルが表示されて安定するまで待つ。
2. **一覧スクロール**: サムネイル一覧を約 10 秒間、一定の速度でスクロールする。
3. **ビューアのページ送り**: 作品を開き、初期表示後に 10 ページ程度を同じ操作で送る。
4. **オプションの俯瞰表示**: ページ一覧／グリッドを開閉し、スクロールする。

実データのフォルダ名や画像 URL を計測レポート、issue、PR に貼らないでください。必要なら合成 fixture を使います。

## 2. 初期条件

Mac の Chrome DevTools で比較を始める場合は、次を記録します。

- Chrome のバージョン、macOS と Mac の機種
- viewport: 360 × 800 CSS px
- Device Toolbar の DPR: 3（Device Toolbar は実機の GPU 性能を再現しません）
- CPU throttling: 6× を基準、10× をストレス条件
- キャッシュ条件: warm（同一セッションで再表示）と cold（キャッシュを明示的に消した状態）を分離
- Network throttling: 描画単体の計測では無効。ロード時間も調べるときだけ別試験として設定
- Service Worker: 制御中かどうかを記録し、比較する試行間で状態をそろえる

Device Toolbar の端末プリセットだけで低スペック端末を再現できたとは判断しないでください。

## 3. 既存のサムネイル計測

フォルダ一覧を表示した状態で URL に `?thumb_perf=1` を付けて再読み込みします。既存の計測パネルが表示されたら JSON を保存します。

この probe は主に、可視サムネイルのロード／decode と一覧が安定するまでの時間を記録します。**描画フレームの滑らかさを直接計測するものではありません。** 同じ指標を別のスクリプトで再実装せず、描画時間は以下の Performance trace と合わせて読みます。

JSON の `environment` と `result` を保存し、各試行のキャッシュ状態を混在させないでください。レポートには画像 URL や作品名を含めないでください。

## 4. Chrome DevTools で描画を調べる

1. DevTools → **Performance** を開きます。
2. 記録を開始し、対象操作（一覧スクロール、ページ送りなど）を行ってから記録を停止します。
3. Main track の `Image Decode`、`Recalculate Style`、`Layout`、`Paint`、`Composite Layers`、Long Task を確認します。
4. **More tools → Rendering** を開き、可能なら **Frame Rendering Stats** と **Paint flashing** を有効にして同じ操作を再現します。
5. スクロール中に大きな領域が繰り返し再描画されるか、フレームが落ちる箇所と長いタスクが一致するかを確認します。

CSS の `backdrop-filter`、影、transform、半透明レイヤーが存在することだけを理由にボトルネックと決めつけないでください。trace で重い箇所を確認してから、画像側と CSS 側の変更を**一度に一種類ずつ**比較します。

## 5. 任意: Console でフレーム間隔と Long Task を採取

DevTools → Console で以下を実行すると、最大 30 秒間、requestAnimationFrame のコールバック間隔と Long Task を記録します。実行後に一覧スクロールやページ送りを行い、終了時に JSON がダウンロードされます。

この値はブラウザがコールバックを実行できた間隔のサンプルであり、GPU の実際の present 時刻や厳密な dropped-frame 数ではありません。DevTools の trace と併用してください。バックグラウンドタブ、端末の省電力状態、他の処理でも値は変化します。

```js
(() => {
  if (window.__bookqRenderProbe?.stop) {
    console.warn("Render probe is already running");
    return;
  }

  const startedAt = performance.now();
  const frameIntervals = [];
  const longTasks = [];
  let previousFrame = null;
  let rafId = 0;
  let stopped = false;
  let observer = null;

  try {
    if ("PerformanceObserver" in window &&
        PerformanceObserver.supportedEntryTypes?.includes("longtask")) {
      observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          longTasks.push({
            start_ms: Math.round((entry.startTime - startedAt) * 10) / 10,
            duration_ms: Math.round(entry.duration * 10) / 10,
          });
        }
      });
      observer.observe({ type: "longtask", buffered: true });
    }
  } catch (_) {
    observer = null;
  }

  function frame(now) {
    if (stopped) return;
    if (previousFrame !== null) {
      const interval = now - previousFrame;
      frameIntervals.push(Math.round(interval * 10) / 10);
    }
    previousFrame = now;
    if (now - startedAt < 30000) rafId = requestAnimationFrame(frame);
    else stop();
  }

  function percentile(values, p) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.ceil(p * sorted.length) - 1)];
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    cancelAnimationFrame(rafId);
    observer?.disconnect();
    const report = {
      metric_version: 1,
      environment: {
        user_agent: navigator.userAgent,
        viewport: {
          width: innerWidth,
          height: innerHeight,
          dpr: devicePixelRatio,
        },
        visibility_state: document.visibilityState,
        duration_ms: Math.round((performance.now() - startedAt) * 10) / 10,
      },
      frames: {
        samples: frameIntervals.length,
        interval_p50_ms: percentile(frameIntervals, 0.50),
        interval_p95_ms: percentile(frameIntervals, 0.95),
        interval_p99_ms: percentile(frameIntervals, 0.99),
        over_16_7ms: frameIntervals.filter((v) => v > 16.7).length,
        over_33_3ms: frameIntervals.filter((v) => v > 33.3).length,
        max_interval_ms: frameIntervals.length ? Math.max(...frameIntervals) : null,
      },
      long_tasks: {
        count: longTasks.length,
        max_duration_ms: longTasks.length
          ? Math.max(...longTasks.map((t) => t.duration_ms)) : null,
        entries: longTasks,
      },
    };
    window.__bookqRenderProbe = { stop: null, report };
    const blob = new Blob([JSON.stringify(report, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "bookq-render-perf.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    console.info("bookq render probe report", report);
  }

  window.__bookqRenderProbe = { stop };
  rafId = requestAnimationFrame(frame);
  console.info("Render probe started: perform the scenario now; auto-stops after 30s. Run __bookqRenderProbe.stop() to stop early.");
})();
```

手動で早く終了する場合は Console で `__bookqRenderProbe.stop()` を実行します。測定 JSON は試行ごとに別名で保存してください。

## 6. 比較・判断のルール

- 各条件で最低 3 回測定し、中央値を比較する。単発の最良値だけで判断しない。
- warm / cold、CPU throttling、Network throttling、Service Worker 状態の違う結果を同じ母集団に混ぜない。
- 60 Hz では 1 フレームの予算は約 16.7 ms ですが、Mac 上の CPU throttling の結果をそのまま実機の合否基準にしない。
- 50 ms を超える Main-thread task は操作遅延の候補として調べる。長い task がないことだけで滑らかさを保証しない。
- まずベースラインを記録し、画像ロード／decode、JS、style/layout、paint/composite のどこが支配的かを判断する。画像のみ、CSS のみなど変更を分けて再計測する。
- 最終的な合否基準は、実機のベースラインとユーザーが許容できる操作感を確認してから決める。

## 7. 実機確認

可能なら低価格 Android 端末の Chrome Stable で同じシナリオを実施します。Chrome Remote Debugging で Performance trace を取得し、端末名、Android / Chrome バージョン、画面サイズ、キャッシュ状態を記録します。端末の温度や省電力状態で性能が変わるため、連続測定前に状態をそろえます。

Mac DevTools は比較と原因特定、Android Emulator は viewport / ブラウザ互換性の確認、実機は SoC / GPU / メモリ / 熱制御を含めた最終確認に使い分けます。

## 参考

- [Chrome DevTools Performance](https://developer.chrome.com/docs/devtools/performance/)
- [Chrome DevTools Device Mode](https://developer.chrome.com/docs/devtools/device-mode/)
- [Chrome DevTools Remote Debugging](https://developer.chrome.com/docs/devtools/remote-debugging/)
- [web.dev: Optimize long tasks](https://web.dev/articles/optimize-long-tasks)
