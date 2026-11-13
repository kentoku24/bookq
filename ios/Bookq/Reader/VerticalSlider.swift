import SwiftUI

/// Vertical scrubber for the landscape reader rail. DragGesture-based —
/// a rotated SwiftUI Slider has unreliable hit-testing, and its RTL
/// handling doesn't map to a vertical axis anyway. Top = range.lowerBound
/// (chapter start), bottom = range.upperBound (chapter end); dragging
/// down advances, matching "position in chapter" read top→bottom.
struct VerticalSlider: View {
    @Binding var value: Double
    /// Slider domain — the caller passes the chapter-local spread range.
    var range: ClosedRange<Double> = 0...1
    var enabled: Bool = true

    private let thumbDiameter: CGFloat = 26
    private let trackWidth: CGFloat = 4

    var body: some View {
        GeometryReader { geo in
            let span = max(range.upperBound - range.lowerBound, .ulpOfOne)
            let frac = CGFloat(min(max((value - range.lowerBound) / span, 0), 1))
            let travel = max(geo.size.height - thumbDiameter, 1)
            ZStack(alignment: .top) {
                Capsule()
                    .fill(.white.opacity(0.22))
                    .frame(width: trackWidth)
                Circle()
                    .fill(.white)
                    .frame(width: thumbDiameter, height: thumbDiameter)
                    .overlay(Circle().stroke(.black.opacity(0.45), lineWidth: 1))
                    .shadow(color: .black.opacity(0.4), radius: 2)
                    .offset(y: frac * travel)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { v in update(v.location.y, height: geo.size.height) }
                    .onEnded { v in update(v.location.y, height: geo.size.height) }
            )
        }
        .opacity(enabled ? 1 : 0.35)
        .accessibilityElement()
        .accessibilityLabel("ページスライダー")
        .accessibilityValue("\(Int(value))")
        .accessibilityAdjustableAction { direction in
            guard enabled else { return }
            switch direction {
            case .increment:
                value = min(value + 1, range.upperBound)
            case .decrement:
                value = max(value - 1, range.lowerBound)
            @unknown default:
                break
            }
        }
    }

    /// y → value. The thumb's center travels [0, height - thumbDiameter];
    /// subtracting half the thumb keeps a grab from jumping.
    private func update(_ y: CGFloat, height: CGFloat) {
        guard enabled, height > 0 else { return }
        let span = max(range.upperBound - range.lowerBound, .ulpOfOne)
        let travel = max(height - thumbDiameter, 1)
        let clamped = min(max(y - thumbDiameter / 2, 0), travel)
        value = range.lowerBound + (clamped / travel) * span
    }
}
