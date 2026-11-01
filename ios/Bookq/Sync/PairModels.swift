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

struct PairChapter: Equatable {
    var name: String
    var folder: String
    var pages: [PairPageRef]
}

struct PairSpread: Equatable {
    var id: String            // "c<chapterIdx>:s<spreadIdx>"
    var chapterFolder: String
    var right: PairPageRef
    var left: PairPageRef?
}

/// Committed work manifest (catalog.resolve_work). `digest` covers only
/// the content keys — see CanonicalJSON.manifestDigest.
struct PairManifest: Equatable {
    var folder: String
    var offset: Int
    var chapters: [PairChapter]
    var pageRefs: [PairPageRef]
    var spreadTable: [PairSpread]
    var digest: String
    var initialSpreadId: String?

    func spreadIndex(byId id: String) -> Int? {
        spreadTable.firstIndex { $0.id == id }
    }

    /// `spreadIndexForPage` — first spread containing the ref on either side.
    func spreadIndex(forPage ref: PairPageRef) -> Int? {
        spreadTable.firstIndex {
            $0.right == ref || $0.left == ref
        }
    }

    private static func ref(_ v: Any?) -> PairPageRef? {
        guard let d = v as? [String: Any],
              let f = d["folder"] as? String,
              let n = d["name"] as? String else { return nil }
        return PairPageRef(folder: f, name: n)
    }

    /// Parse the digest-verified manifest object (JSONSerialization dict).
    static func parse(_ obj: [String: Any]) -> PairManifest? {
        guard let folder = obj["folder"] as? String,
              let offset = obj["offset"] as? Int,
              let digest = obj["digest"] as? String,
              let chapterDicts = obj["chapters"] as? [[String: Any]],
              let refDicts = obj["pageRefs"] as? [[String: Any]],
              let spreadDicts = obj["spreadTable"] as? [[String: Any]]
        else { return nil }

        var chapters: [PairChapter] = []
        for c in chapterDicts {
            guard let name = c["name"] as? String,
                  let cf = c["folder"] as? String,
                  let pageDicts = c["pages"] as? [[String: Any]] else { return nil }
            chapters.append(PairChapter(
                name: name, folder: cf,
                pages: pageDicts.compactMap(ref)))
        }
        var table: [PairSpread] = []
        for s in spreadDicts {
            guard let id = s["id"] as? String,
                  let cf = s["chapterFolder"] as? String,
                  let right = ref(s["right"]) else { return nil }
            table.append(PairSpread(id: id, chapterFolder: cf,
                                    right: right, left: ref(s["left"])))
        }
        let initial = (obj["initial"] as? [String: Any])?["spreadId"] as? String
        return PairManifest(
            folder: folder, offset: offset, chapters: chapters,
            pageRefs: refDicts.compactMap(ref), spreadTable: table,
            digest: digest, initialSpreadId: initial)
    }
}

/// pairModel.turnIndex — chapter clamp when !crossChapter.
enum PairModel {
    static func turnIndex(in table: [PairSpread], index: Int,
                          delta: Int, crossChapter: Bool) -> Int? {
        guard !table.isEmpty, index >= 0, index < table.count else { return nil }
        guard delta == 1 || delta == -1 else { return nil }
        let target = index + delta
        if crossChapter {
            return max(0, min(target, table.count - 1))
        }
        let chapter = table[index].chapterFolder
        var lo = index, hi = index
        while lo > 0 && table[lo - 1].chapterFolder == chapter { lo -= 1 }
        while hi + 1 < table.count && table[hi + 1].chapterFolder == chapter { hi += 1 }
        return max(lo, min(target, hi))
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
    var revision: Int
    var workEpoch: Int
    var manifestVersion: Int
    var phase: PairPhase
    var spreadId: String?
    var offset: Int
    var leaderSide: PairSide
    var dataPlane: String     // "server-ws" | "webrtc-dc"
}

/// This device's current projection into the shared spread table —
/// pairedView.js apply() equivalent.
struct PairProjection: Equatable {
    var spreadIndex: Int
    var spreadTotal: Int
    var side: PairSide
    var ref: PairPageRef?      // nil → blank side
    var anchor: PairPageRef    // first real page of the spread
    var chapterName: String
    var chapterFolder: String
}
