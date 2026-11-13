import SwiftUI

/// A single browse tile/row/card for a folder. Cover = `/thumb/` single
/// image, or `/api/thumb-sprite` composite when `thumbs` has 2+ entries.
struct FolderTileView: View {
    let path: String
    let node: TreeNode
    var layout: AppConfig.LayoutMode = .tile
    var progress: Double = 0
    /// Download targets for the offline button (empty = no button) —
    /// rendered by the caller as a sibling overlay (a Button inside a
    /// Button label never receives taps).
    var leaves: [String] = []
    /// Search-result metadata — badge row under the title (artist≤2,tag≤4).
    var meta: ComicMetadata? = nil
    /// Visibility presentation (showAll only): badge text + whether
    /// the caller renders the eye toggle.
    var vis: VisibilityStore.Presentation? = nil
    /// spec §6 `.off-unavail` — offline/connecting + no download record:
    /// greyscale + dimmed + "オフライン" badge (tap rejected upstream).
    var unavailable: Bool = false

    @EnvironmentObject private var model: AppModel

    var body: some View {
        Group {
            switch layout {
            case .tile: tileBody
            case .list: rowBody(thumbSize: 44)
            case .card: rowBody(thumbSize: 72)
            }
        }
        .saturation(unavailable ? 0 : 1)
        .opacity(unavailable ? 0.55 : 1)
        .overlay(alignment: .top) { unavailableBadge }
        // Web parity: hidden tiles are NOT dimmed — `is-hidden` has no
        // visual rule; the badge alone marks them.
        .overlay(alignment: .topLeading) { visBadge }
    }

    /// `.off-unavail-badge` equivalent.
    @ViewBuilder
    private var unavailableBadge: some View {
        if unavailable {
            Text("オフライン")
                .font(.system(size: 9, weight: .semibold))
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(.black.opacity(0.7), in: Capsule())
                .foregroundStyle(.white)
                .padding(4)
        }
    }

    @ViewBuilder
    private var visBadge: some View {
        if let badge = vis?.badge {
            Text(badge)
                .font(.system(size: 9, weight: .semibold))
                .padding(.horizontal, 5).padding(.vertical, 2)
                .background(.black.opacity(0.7), in: Capsule())
                .foregroundStyle(.white)
                .padding(4)
        }
    }

    private var tileBody: some View {
        VStack(alignment: .leading, spacing: 4) {
            // Fixed-ratio cover box — a resizable image reports its
            // intrinsic size as the ideal width and would overflow the
            // grid column; the clear box proposes a bounded frame and the
            // image fills+clips inside it.
            Color.clear
                .aspectRatio(2.0 / 3.0, contentMode: .fit)
                .overlay { cover.clipped() }
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(alignment: .bottom) { progressBar }
            titleStack
        }
    }

    private func rowBody(thumbSize: CGFloat) -> some View {
        HStack(spacing: 10) {
            cover
                .frame(width: thumbSize * 2 / 3, height: thumbSize)
                .clipShape(RoundedRectangle(cornerRadius: 4))
            titleStack
            Spacer()
            Image(systemName: "chevron.right")
                .font(.caption).foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
        .overlay(alignment: .bottom) { progressBar }
    }

    private var cover: some View {
        RemoteImageView(url: coverURL, contentMode: .fill)
            .clipped()
    }

    private var coverURL: URL? {
        guard let api = model.api else { return nil }
        let pathIsEmpty = path.isEmpty
        if let thumbs = node.thumbs, thumbs.count >= 2,
           let spec = BrowseModel.multiThumbSpec(thumbs.count) {
            return api.thumbSpriteURL(folder: path,
                                      names: Array(thumbs.prefix(9)),
                                      rows: spec.rows,
                                      w: 400)
        }
        guard let thumb = node.thumb, !thumb.isEmpty else { return nil }
        // Root-level loose images have no parent folder; thumb is root-relative.
        return api.thumbURL(folder: pathIsEmpty ? "" : path, name: thumb, w: 400)
    }

    private var displayName: (leaf: String, parent: String?) {
        BrowseModel.splitDisplayName(node.name)
    }

    private var titleStack: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(displayName.leaf)
                .font(.caption)
                .lineLimit(2)
            if let parent = displayName.parent {
                Text(parent)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            if let meta, meta.has_metadata {
                metadataBadges(meta)
            }
        }
    }

    /// `tile-metadata` equivalent — artist ≤2 (🎨), tags ≤4 (🏷). Badges
    /// WRAP like the web's flex row (a rigid HStack would demand a minimum
    /// width larger than the tile column and overflow the grid cell).
    @ViewBuilder
    private func metadataBadges(_ meta: ComicMetadata) -> some View {
        let badges = meta.artist.prefix(2).map { ("🎨", $0) }
            + meta.tags.prefix(4).map { ("🏷", $0) }
        if !badges.isEmpty {
            FlowLayout(spacing: 3) {
                ForEach(Array(badges.enumerated()), id: \.offset) { _, b in
                    Text(b.0 + " " + b.1)
                        .font(.system(size: 8))
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(.quaternary, in: Capsule())
                        .lineLimit(1)
                }
            }
        }
    }

    @ViewBuilder
    private var progressBar: some View {
        if progress > 0 {
            GeometryReader { geo in
                Rectangle()
                    .fill(Color.accentColor)
                    .frame(width: geo.size.width * min(progress, 1), height: 3)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 3)
        }
    }
}
