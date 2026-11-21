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

public struct CGRect: Equatable {
    public var x: CGFloat
    public var y: CGFloat
    public var width: CGFloat
    public var height: CGFloat
    public init(x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }
    public var minX: CGFloat { x }
    public var maxX: CGFloat { x + width }
}
#endif
