import SwiftUI
import UIKit

/// A zoom container for one reader page. UIScrollView owns pinch zoom +
/// double-tap toggle; its pan gesture is DISABLED at 1x so the outer page
/// pager keeps receiving swipes, and enabled while zoomed for panning.
struct ZoomableScrollView<Content: View>: UIViewRepresentable {
    var onZoomChange: ((Bool) -> Void)? = nil
    /// Fired when a pinch-IN (scale < ~0.75) ends near 1x — overview trigger.
    var onPinchOut: (() -> Void)? = nil
    /// Fired on pinch-gesture state transitions (true = pinch in flight).
    /// Lets the outer pager suspend page drags while a second finger is down.
    var onPinchState: ((Bool) -> Void)? = nil
    @ViewBuilder var content: () -> Content

    func makeUIView(context: Context) -> UIScrollView {
        let sv = UIScrollView()
        sv.minimumZoomScale = 1
        sv.maximumZoomScale = 5
        sv.bouncesZoom = true
        sv.showsVerticalScrollIndicator = false
        sv.showsHorizontalScrollIndicator = false
        sv.delegate = context.coordinator

        let hosted = context.coordinator.host
        hosted.view.backgroundColor = .clear
        hosted.view.translatesAutoresizingMaskIntoConstraints = false
        sv.addSubview(hosted.view)
        NSLayoutConstraint.activate([
            hosted.view.leadingAnchor.constraint(equalTo: sv.contentLayoutGuide.leadingAnchor),
            hosted.view.trailingAnchor.constraint(equalTo: sv.contentLayoutGuide.trailingAnchor),
            hosted.view.topAnchor.constraint(equalTo: sv.contentLayoutGuide.topAnchor),
            hosted.view.bottomAnchor.constraint(equalTo: sv.contentLayoutGuide.bottomAnchor),
            hosted.view.widthAnchor.constraint(equalTo: sv.frameLayoutGuide.widthAnchor),
            hosted.view.heightAnchor.constraint(equalTo: sv.frameLayoutGuide.heightAnchor),
        ])

        // No double-tap zoom (per product decision — pinch + pan only).
        // At 1x the pan gesture must not eat swipes meant for the pager.
        sv.panGestureRecognizer.isEnabled = false
        // A second target on the pinch GR observes pinch-IN endings for the
        // overview gesture — UIScrollView itself just bounces back to 1.
        sv.pinchGestureRecognizer?.addTarget(
            context.coordinator, action: #selector(Coordinator.onPinch(_:)))
        context.coordinator.scrollView = sv
        context.coordinator.updateZoomState()
        return sv
    }

    func updateUIView(_ sv: UIScrollView, context: Context) {
        context.coordinator.host.rootView = content()
        context.coordinator.onZoomChange = onZoomChange
        context.coordinator.onPinchOut = onPinchOut
        context.coordinator.onPinchState = onPinchState
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(host: UIHostingController(rootView: content()))
    }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        let host: UIHostingController<Content>
        weak var scrollView: UIScrollView?
        var onZoomChange: ((Bool) -> Void)?
        var onPinchOut: (() -> Void)?
        var onPinchState: ((Bool) -> Void)?
        private var zoomed = false
        private var pinchActive = false

        init(host: UIHostingController<Content>) { self.host = host }

        @objc func onPinch(_ gr: UIPinchGestureRecognizer) {
            let active = gr.state == .began || gr.state == .changed
            if active != pinchActive {
                pinchActive = active
                onPinchState?(active)
            }
            guard gr.state == .ended || gr.state == .cancelled,
                  let sv = scrollView else { return }
            // Deliberate pinch-in that lands at ~1x → open the page grid.
            if gr.scale < 0.75 && sv.zoomScale <= 1.05 {
                sv.setZoomScale(1, animated: false)
                onPinchOut?()
            }
        }

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { host.view }

        func scrollViewDidZoom(_ scrollView: UIScrollView) {
            updateZoomState()
        }

        func updateZoomState() {
            guard let sv = scrollView else { return }
            // Keep content centered when smaller than bounds.
            let dx = max(0, (sv.bounds.width - sv.contentSize.width) / 2)
            let dy = max(0, (sv.bounds.height - sv.contentSize.height) / 2)
            host.view.center = CGPoint(x: sv.contentSize.width / 2 + dx * 0,
                                       y: sv.contentSize.height / 2 + dy * 0)
            sv.contentInset = UIEdgeInsets(top: dy, left: dx, bottom: dy, right: dx)
            let nowZoomed = sv.zoomScale > 1.05
            if nowZoomed != zoomed {
                zoomed = nowZoomed
                sv.panGestureRecognizer.isEnabled = nowZoomed
                onZoomChange?(nowZoomed)
            }
        }
    }
}
