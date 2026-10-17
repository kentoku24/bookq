"""Success-aware visible-thumbnail timing against an isolated loopback dev app."""

import argparse
import datetime
import json
import statistics
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROBE = Path(__file__).resolve().parents[2] / "app/perf/thumbnail-probe.js"
PROFILES = {
    "iphone": {
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
    },
    "desktop": {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 1},
}


def local_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.username or parsed.password
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("Use an isolated loopback dev root URL only.")
    return value.rstrip("/") + "/"


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be positive.")
    return number


def measure_once(browser, base_url, profile, timeout_ms):
    context = browser.new_context(**PROFILES[profile], service_workers="block")
    try:
        page = context.new_page()
        page.add_init_script("window.__thumbnailProbeConfig = " + json.dumps({
            "timeoutMs": timeout_ms, "cacheCondition": "fresh_browser_context",
        }) + ";\n" + PROBE.read_text(encoding="utf-8"))
        response = page.goto(base_url, wait_until="commit", timeout=timeout_ms)
        if not response or response.status != 200 or page.url.rstrip("/") != base_url.rstrip("/"):
            return {"status": "navigation_error", "visible_ready_ms": None}
        try:
            page.wait_for_function(
                "() => window.__thumbnailProbe && window.__thumbnailProbe.done",
                timeout=timeout_ms,
            )
            status = None
        except PlaywrightTimeoutError:
            status = "timeout"
        result = page.evaluate("() => window.__thumbnailProbe") or {}
        status = status or result.get("status", "timeout")
        if result.get("broken_images", 0):
            status = "broken_images"
        result["status"] = status
        if status != "ok":
            result["visible_ready_ms"] = None
        return result
    except PlaywrightError:
        # Browser exceptions can contain private live folder URLs: never serialize them.
        return {"status": "browser_error", "visible_ready_ms": None}
    finally:
        context.close()


def summarize(runs):
    successful = [r for r in runs if r["status"] == "ok"]
    all_ok = len(successful) == len(runs) and bool(runs)
    return {
        "successful_runs": len(successful),
        "total_runs": len(runs),
        # Do not hide failed runs by reporting only a surviving subset's median.
        "median_visible_ready_ms": (
            statistics.median(r["visible_ready_ms"] for r in runs) if all_ok else None
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", type=local_url)
    parser.add_argument("--profile", choices=PROFILES, default="iphone")
    parser.add_argument("--runs", type=positive_int, default=3)
    parser.add_argument("--timeout-ms", type=positive_int, default=30000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out = args.out or Path(".omo/evidence") / ("thumbnail-speed-" + stamp)
    # Refuse to overwrite any existing evidence directory.
    out.mkdir(parents=True, exist_ok=False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            runs = [
                measure_once(browser, args.base_url, args.profile, args.timeout_ms)
                for _ in range(args.runs)
            ]
        finally:
            browser.close()
    report = {
        "metric_version": 1,
        "profile": args.profile,
        "browser": "chromium",
        "browser_cache": "fresh context per run; service workers blocked",
        "server_cache": "persistent across runs; not reset by this command",
        "scope": "top browse, viewport-visible images after full tree ready",
        "runs": runs,
        "summary": summarize(runs),
    }
    text = json.dumps(report, indent=2)
    (out / "thumbnails.json").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["summary"]["successful_runs"] == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
