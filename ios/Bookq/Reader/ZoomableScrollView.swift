import SwiftUI
import UIKit

/// A zoom container for one reader page. UIScrollView owns pinch zoom +
/// double-tap toggle; its pan gesture is DISABLED at 1x so the outer page
/// pager keeps receiving swipes, and enabled while zoomed for panning.
struct ZoomableScrollView<Content: View>: UIViewRepresentable {
    var onZoomChange: ((Bool) -> Void)? = nil
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

        let doubleTap = UITapGestureRecognizer(
            target: context.coordinator, action: #selector(Coordinator.onDoubleTap(_:)))
        doubleTap.numberOfTapsRequired = 2
        sv.addGestureRecognizer(doubleTap)

        // At 1x the pan gesture must not eat swipes meant for the pager.
        sv.panGestureRecognizer.isEnabled = false
        context.coordinator.scrollView = sv
        context.coordinator.updateZoomState()
        return sv
    }

    func updateUIView(_ sv: UIScrollView, context: Context) {
        context.coordinator.host.rootView = content()
        context.coordinator.onZoomChange = onZoomChange
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(host: UIHostingController(rootView: content()))
    }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        let host: UIHostingController<Content>
        weak var scrollView: UIScrollView?
        var onZoomChange: ((Bool) -> Void)?
        private var zoomed = false

        init(host: UIHostingController<Content>) { self.host = host }

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { host.view }

        func scrollViewDidZoom(_ scrollView: UIScrollView) {
            updateZoomState()
        }

        @objc func onDoubleTap(_ g: UITapGestureRecognizer) {
            guard let sv = scrollView else { return }
            if sv.zoomScale > 1.01 {
                sv.setZoomScale(1, animated: true)
            } else {
                let p = g.location(in: host.view)
                let r = CGRect(x: p.x - 100, y: p.y - 100, width: 200, height: 200)
                sv.zoom(to: r, animated: true)
            }
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
