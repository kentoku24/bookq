import Foundation

/// Wire types for /ws/reader-sync protocol v1 — ports of
/// app/reader_sync/{protocol,model}.py + viewer/pairModel.js.
/// PageRef {folder,name} is the shared addressing scheme: `folder` is the
/// downloads-relative path, `name` the entry name inside it.
enum PairRole: String, Codable, Equatable {
    case leader, follower
}

enum PairSide: String, Codable, Equatable {
    case right, left
}

enum PairPhase: String, Codable, Equatable {
    case waiting, loading, reading, selecting, paused
}

struct PairPageRef: Codable, Equatable, Hashable {
    var folder: String
    var name: String
}

struct PairSpread: Equatable {
    var id: String            // "c<chapterIdx>:s<spreadIdx>"
    var chapterFolder: String
    var right: PairPageRef
    var left: PairPageRef?
}

/// Committed work manifest (catalog.resolve_work). Only the spread table
/// is consumed — the remaining digest-covered keys are shape-checked on
/// parse (see CanonicalJSON.manifestDigest) but not stored.
struct PairManifest: Equatable {
    var spreadTable: [PairSpread]

    func spreadIndex(byId id: String) -> Int? {
        spreadTable.firstIndex { $0.id == id }
    }

    private static func ref(_ v: Any?) -> PairPageRef? {
        guard let d = v as? [String: Any],
              let f = d["folder"] as? String,
              let n = d["name"] as? String else { return nil }
        return PairPageRef(folder: f, name: n)
    }

    /// Parse the digest-verified manifest object (JSONSerialization dict).
    static func parse(_ obj: [String: Any]) -> PairManifest? {
        guard obj["folder"] is String,
              obj["offset"] is Int,
              obj["digest"] is String,
              obj["chapters"] is [[String: Any]],
              obj["pageRefs"] is [[String: Any]],
              let spreadDicts = obj["spreadTable"] as? [[String: Any]]
        else { return nil }

        var table: [PairSpread] = []
        for s in spreadDicts {
            guard let id = s["id"] as? String,
                  let cf = s["chapterFolder"] as? String,
                  let right = ref(s["right"]) else { return nil }
            table.append(PairSpread(id: id, chapterFolder: cf,
                                    right: right, left: ref(s["left"])))
        }
        return PairManifest(spreadTable: table)
    }
}

/// pairModel.turnIndex — chapter clamp when !crossChapter.
enum PairModel {
    /// Contiguous `chapterFolder` run containing `index` — the chapter's
    /// spread bounds [lo, hi] inclusive. Chapters never share a spread,
    /// so the run is exact (web getChapterSpreadRange for the pair table).
    static func chapterRange(in table: [PairSpread],
                             index: Int) -> (lo: Int, hi: Int)? {
        guard table.indices.contains(index) else { return nil }
        let chapter = table[index].chapterFolder
        var lo = index, hi = index
        while lo > 0 && table[lo - 1].chapterFolder == chapter { lo -= 1 }
        while hi + 1 < table.count && table[hi + 1].chapterFolder == chapter { hi += 1 }
        return (lo, hi)
    }

    static func turnIndex(in table: [PairSpread], index: Int,
                          delta: Int, crossChapter: Bool) -> Int? {
        guard !table.isEmpty, index >= 0, index < table.count else { return nil }
        guard delta == 1 || delta == -1 else { return nil }
        let target = index + delta
        if crossChapter {
            return max(0, min(target, table.count - 1))
        }
        guard let r = chapterRange(in: table, index: index) else { return nil }
        return max(r.lo, min(target, r.hi))
    }

    /// Canonical projection (§3.2): leader renders leaderSide, follower the
    /// opposite. ref == nil → blank side.
    static func project(spread: PairSpread, role: PairRole,
                        leaderSide: PairSide) -> (side: PairSide, ref: PairPageRef?) {
        let side: PairSide = (role == .leader)
            ? leaderSide
            : (leaderSide == .right ? .left : .right)
        return (side, side == .right ? spread.right : spread.left)
    }
}

/// Paired-reader cover rules shared by LibraryView.pairReaderBinding and
/// PairedReaderView.onDisappear (#216).
enum PairReaderGate {
    /// Phases that keep the shared reader mounted. `loading` and `paused`
    /// must stay presented: they are transient (manifest reload on
    /// SET_OFFSET/OPEN_WORK, member away) and the view renders their own
    /// overlays. `waiting` is excluded — the pair sheet owns that UX.
    static func presents(_ phase: PairPhase?) -> Bool {
        phase == .reading || phase == .loading ||
            phase == .paused || phase == .selecting
    }

    /// Whether dismissing the cover in this phase is the leader's intent to
    /// go browse (SET_ACTIVITY selecting). Phase-driven dismissals —
    /// transient loading, paused, ended — are not a browse intent, so the
    /// gate fires only when the close happened while `reading`.
    static func closeSignalsSelecting(_ phase: PairPhase?) -> Bool {
        phase == .reading
    }
}

/// STATE.presence entry.
struct PairPresenceEntry: Equatable {
    var role: PairRole
    var status: String        // active | suspended | offline | pending
    var name: String
    var ready: Bool
}

/// Committed snapshot — STATE frame minus the envelope, also embedded in
/// REJECT (`reject + latest snapshot`, §3.4).
struct PairSnapshot: Equatable {
    var sessionId: String?
    var workEpoch: Int
    var manifestVersion: Int
    var phase: PairPhase
    var spreadId: String?
    var offset: Int
    var leaderSide: PairSide
}

/// This device's current projection into the shared spread table —
/// pairedView.js apply() equivalent.
struct PairProjection: Equatable {
    var spreadIndex: Int
    var spreadTotal: Int
    var ref: PairPageRef?      // nil → blank side
    var chapterFolder: String
}
