---
name: android-emulator
description: Standard bookq Android dev/test loop on the emulator (AVD bookq_tab) — dev-env connection, unit/UI tests, screenshots, and the mandatory teardown that keeps the ~9GB AVD + ~4GB Gradle daemons from orphaning. Trigger on "エミュレータで確認", "Android emulator", "androidTest をエミュレータで", "Android simulator". Not for the physical CP08-J1 tablet or FrameTimingMetric benchmarks.
---

# Android Emulator dev/test workflow

Standard loop for developing/verifying the native app (`android/`) without
physical hardware. Deep detail lives in `docs/android-app.md` — this skill is
the operational layer: the commands plus the cleanup contract.

**The emulator is expensive (~9GB resident while booted) and Gradle daemons
linger at 2–4GB.** On a memory-pressured host these orphans push the machine
into swap. The cleanup section is not optional.

## Prerequisites

```bash
export JAVA_HOME=$(brew --prefix openjdk@21)
export ANDROID_HOME=$HOME/Library/Android/sdk
# android/local.properties (gitignored): sdk.dir=$HOME/Library/Android/sdk
```

## Quick Reference

| Task | Command |
|---|---|
| Create AVD (once) | `avdmanager create avd -n bookq_tab -k "system-images;android-35;google_apis;arm64-v8a" -d pixel_tablet` |
| Start emulator | `emulator -avd bookq_tab -no-snapshot-save -gpu auto -skin 2560x1600 &` |
| Wait for boot | `adb wait-for-device && adb shell getprop sys.boot_completed` → `1` |
| Install app | `cd android && ./gradlew :app:installDebug` |
| Launch w/ dev env | `adb shell am start -n dev.bookq.reader.debug/dev.bookq.reader.MainActivity --es serverURL "$(./dev.sh url <name>)"` |
| Launch w/ local server | `--es serverURL http://10.0.2.2:8080` (host loopback bridge) |
| Screenshot | `adb -s emulator-5554 exec-out screencap -p > /tmp/emu.png` |
| Unit tests + lint (no emu) | `./gradlew :core:test :data:testDebugUnitTest :app:testDebugUnitTest :app:lintDebug :app:assembleDebug` |
| UI tests (emu required) | `./gradlew :app:connectedDebugAndroidTest -Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=<url>` |
| **Stop emulator (必須)** | `adb -s emulator-5554 emu kill` |
| **Stop daemons (必須)** | `./gradlew --stop` |

## Flow

1. **Have a server**: `./dev.sh up <name>` (tailnet HTTPS) or a TEST_MODE
   uvicorn on `:8080`. The emulator's NAT rides the host network, so
   `https://dev-XX.<tailnet>.ts.net` works directly — **no Tailscale app
   needed** (unlike the physical CP08-J1).
2. **Boot the AVD** (Quick Ref) and wait for `sys.boot_completed=1`. First
   boot takes a while; `adb wait-for-device` alone returns before the OS is
   usable.
3. **Install + launch** with `--es serverURL <url>` (debug/benchmark builds
   only — release refuses the extra). For a host TEST_MODE server use
   `http://10.0.2.2:8080`; cleartext is allowed for `10.0.2.2`/localhost in
   the debug network config.
4. **Iterate**: code change → `./gradlew :app:installDebug` → `am start`
   again (or `adb shell am force-stop dev.bookq.reader.debug` first for a
   clean cold start).
5. **Verify**: screenshots via `exec-out screencap`; UI assertions via
   `connectedDebugAndroidTest` (`bookqExpectedFolder=<name>` overrides the
   expected fixture folder, default `Series/vol1`).
6. **Clean up** — see below.

## Cleanup contract (mandatory)

Stop what **you** started, when you stop needing it — not only at session
end. Idle gaps between test rounds count.

```bash
adb -s emulator-5554 emu kill     # AVD ~9GB — biggest single win
cd android && ./gradlew --stop    # Gradle + Kotlin daemons, 2–4GB
./dev.sh down <name>              # dev env slot (keeps tailscale state)
```

- Finish stray `adb`/`logcat`/`screencap`/`sleep`-loop commands you spawned
  before ending the session.
- **Only kill your own processes.** Parallel sessions share this host —
  check `adb devices`, `docker ps` (`com.bookq.name` label), and `ps`
  parentage before stopping anything you did not start.
- Emulator cold-boot is minutes, not seconds — if you know you're coming
  back in a moment, a running AVD is legitimate. The rule is: never leave
  one orphaned with no owner.

## Constraints / gotchas

- **FrameTimingMetric benchmarks do NOT work on the emulator** — its
  SurfaceFlinger emits no frame timeline ("no expect/actual slices").
  `TraceSectionMetric` (`tapTurn`/`readerOpen`) works with
  `-Pandroid.testInstrumentationRunnerArguments.androidx.benchmark.suppressErrors=EMULATOR`.
  Frame timing → physical CP08-J1 only (`android-perf` skill).
- minSdk 35 — the AVD must be API 35+.
- `connectedDebugAndroidTest` needs a running device; it fails at install
  on a bare `adb devices` list.
- Long `adb exec-out` (screencap/logcat) commands can outlive their caller —
  always bound or reap them.

## Related

- `docs/android-app.md` — full module map, auth flow, benchmark gates
- `android-perf` skill — physical-device FPS measurement
- `dev-lifecycle` skill — dev env up/test/down
- `AGENTS.md` §7 Android — required env vars and the same orphan rules
