# Devices

- iPhone 17（既定、`.claude/skills/ios-simulator` の `SIMULATOR_DEVICE` 未指定時）
- 次の検証端末: Android 15（1920x1200, SoC Unisoc T620 2.2GHz / GPU Mali-G57）— 実機、Simulator 外
- iPad 例: `iPad Pro 13-inch (M4) (16GB)` / `iPad Pro 13-inch (M5) (16GB)` など、M4/M5 は `(16GB)`/`(8GB)` suffix 付き

```bash
# 利用可能なデバイス一覧
xcrun simctl list devicetypes | grep -E "iPhone 17|iPad"
# 正確な名前を確認（suffix 付きに注意）
xcrun simctl list devicetypes | grep iPad

# 複数 Booted の確認
xcrun simctl list | grep Booted
xcrun simctl shutdown <UDID>  # 不要な Simulator を落とす
```

複数台が `Booted` だと `openurl booted` はいずれか1台に解決される。ヘルパーは複数 boot 時に警告を出す。
