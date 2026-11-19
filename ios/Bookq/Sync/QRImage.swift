import CoreImage.CIFilterBuiltins
import SwiftUI
import UIKit

/// QR code for the join deep-link — web encodes `https://<host>/#/join/<code>`
/// at error level L; any camera app then opens the web join page. Native
/// followers can type the code or use 自動接続 instead.
enum QRImage {
    static func image(_ text: String, size: CGFloat = 168) -> UIImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "L"
        guard let ci = filter.outputImage else { return nil }
        let scale = size / max(ci.extent.width, 1)
        let scaled = ci.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let ctx = CIContext()
        guard let cg = ctx.createCGImage(scaled, from: scaled.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
