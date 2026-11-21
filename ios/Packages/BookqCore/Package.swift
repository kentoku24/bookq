// swift-tools-version: 5.9
import PackageDescription

// Pure domain rules shared with the web client and the server (#282):
// browse lookup, reader chapters/spreads, pair wire model, canonical
// JSON, path encoding. No UI, no simulator — `swift test` runs on
// macOS and Linux.
let package = Package(
    name: "BookqCore",
    products: [
        .library(name: "BookqCore", targets: ["BookqCore"]),
    ],
    targets: [
        // -enable-library-evolution keeps the module resilient so app
        // clients never deserialize our serialized SIL — Xcode 27.2's
        // -O pipeline crashes in PerformanceSILLinker doing so (#539).
        .target(
            name: "BookqCore",
            swiftSettings: [
                .unsafeFlags(["-Xfrontend", "-enable-library-evolution"]),
            ]
        ),
        .testTarget(name: "BookqCoreTests", dependencies: ["BookqCore"]),
    ]
)
