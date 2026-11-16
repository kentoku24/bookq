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
        .target(name: "BookqCore"),
        .testTarget(name: "BookqCoreTests", dependencies: ["BookqCore"]),
    ]
)
