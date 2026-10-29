import SwiftUI

/// A single browse tile/row/card for a folder. Cover = `/thumb/` single
/// image, or `/api/thumb-sprite` composite when `thumbs` has 2+ entries.
struct FolderTileView: View {
    let path: String
    let node: TreeNode
    var layout: AppConfig.LayoutMode = .tile
    var progress: Double = 0

    @EnvironmentObject private var model: AppModel

    var body: some View {
        switch layout {
        case .tile: tileBody
        case .list: rowBody(thumbSize: 44)
        case .card: rowBody(thumbSize: 72)
        }
    }

    private var tileBody: some View {
        VStack(alignment: .leading, spacing: 4) {
            cover
                .aspectRatio(2.0 / 3.0, contentMode: .fit)
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
