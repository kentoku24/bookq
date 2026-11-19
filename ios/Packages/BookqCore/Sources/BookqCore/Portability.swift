import Foundation

/// Linux (and any non-Apple) fallback for the two CoreGraphics types the
/// pure domain math uses. On Apple platforms `import Foundation` already
/// provides both, so this file defines nothing there.
#if !canImport(CoreGraphics)
public typealias CGFloat = Double

public struct CGSize: Equatable {
    public var width: CGFloat
    public var height: CGFloat
    public init(width: CGFloat, height: CGFloat) {
        self.width = width
        self.height = height
    }
}
#endif
