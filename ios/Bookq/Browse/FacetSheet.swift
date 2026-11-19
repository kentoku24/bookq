import SwiftUI

/// Filter bottom sheet — `#bottom-sheet` equivalent: 作者/タグ facet chips
/// (multi-select) + 並び順 + リセット/適用. Facets are refetched on open and
/// on showAll toggles (`include_hidden` affects the server-side set).
struct FacetSheet: View {
    @ObservedObject var search: SearchModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("作者") {
                    chipFlow(search.facets?.artists ?? [],
                             selected: search.artists) { search.toggleArtist($0) }
                }
                Section("タグ") {
                    chipFlow(search.facets?.tags ?? [],
                             selected: search.tags) { search.toggleTag($0) }
                }
                Section("並び順") {
                    // Server supports relevance only (web's sole radio).
                    Label("関連度", systemImage: "checkmark")
                        .font(.subheadline)
                }
            }
            .navigationTitle("絞り込み")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("リセット") {
                        search.resetFilters()
                        search.filtersChanged()
                    }
                    .accessibilityIdentifier("facetReset")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("適用") {
                        search.filtersChanged()
                        dismiss()
                    }
                    .fontWeight(.semibold)
                    .accessibilityIdentifier("facetApply")
                }
            }
        }
        .presentationDetents([.medium, .large])
        .task { await search.loadFacets() }
    }

    /// `.facet-chips` equivalent — flow-wrapped toggle chips.
    @ViewBuilder
    private func chipFlow(_ values: [String], selected: Set<String>,
                          toggle: @escaping (String) -> Void) -> some View {
        if values.isEmpty {
            Text("なし").font(.caption).foregroundStyle(.secondary)
        } else {
            FlowLayout(spacing: 6) {
                ForEach(values, id: \.self) { v in
                    let on = selected.contains(v)
                    Button { toggle(v) } label: {
                        Text(v)
                            .font(.caption)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(on ? Color.accentColor : Color.secondary.opacity(0.15),
                                        in: Capsule())
                            .foregroundStyle(on ? .white : .primary)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

/// Minimal flow layout for chips (wraps HStacks like the web flex row).
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews,
                      cache: inout ()) -> CGSize {
        let w = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > w, x > 0 { x = 0; y += rowH + spacing; rowH = 0 }
            x += s.width + spacing
            rowH = max(rowH, s.height)
        }
        return CGSize(width: w, height: y + rowH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowH: CGFloat = 0
        let maxW = bounds.width
        for v in subviews {
            var s = v.sizeThatFits(.unspecified)
            if s.width > maxW { s.width = maxW }   // clamp lone wide chips
            if x + s.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX; y += rowH + spacing; rowH = 0
            }
            v.place(at: CGPoint(x: x, y: y),
                    proposal: .init(width: s.width, height: nil))
            x += s.width + spacing
            rowH = max(rowH, s.height)
        }
    }
}
