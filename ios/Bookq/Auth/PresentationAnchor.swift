import AuthenticationServices
import UIKit

/// Supplies the window anchor required by ASWebAuthenticationSession on iOS.
final class PresentationAnchor: NSObject, ASWebAuthenticationPresentationContextProviding {
    static let shared = PresentationAnchor()

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        for scene in UIApplication.shared.connectedScenes {
            guard let ws = scene as? UIWindowScene else { continue }
            if let key = ws.keyWindow { return key }
            if let first = ws.windows.first { return first }
        }
        return ASPresentationAnchor(frame: .zero)
    }
}
