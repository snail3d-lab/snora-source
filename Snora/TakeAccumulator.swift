import Foundation

/// One take's worth of transcript, built to survive what the on-device speech
/// recognizer actually does (proved against the user's phone log, 2026-09-09):
///
///   * it hands back `isFinal` with an **empty** `formattedString` on long takes
///   * mid-take it can **restart** `formattedString` instead of continuing it
///
/// The old ear trusted `latest`, so the empty final overwrote words that had
/// been on screen — the card "cleared itself" and the sentence was gone. Here an
/// empty hypothesis can never win, and finished segments are banked separately.
struct TakeAccumulator {
    /// text from segments that are already over
    private(set) var banked = ""
    /// the segment still being spoken
    private(set) var current = ""
    /// did we ever see actual words? (the old flag counted empty callbacks too,
    /// which turned "the mic heard nothing" into a misleading message)
    private(set) var sawWords = false

    /// what we would hand to the planner right now
    var best: String {
        let a = banked.trimmingCharacters(in: .whitespacesAndNewlines)
        let b = current.trimmingCharacters(in: .whitespacesAndNewlines)
        if a.isEmpty { return b }
        if b.isEmpty { return a }
        return a + " " + b
    }

    mutating func note(_ raw: String) {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }          // an empty result never wins
        sawWords = true
        guard !current.isEmpty else { current = t; return }
        // a normal continuation extends (or corrects) what we already have, so it
        // shares a prefix. no shared prefix = the model restarted mid-segment;
        // keep the longer of the two rather than losing either.
        let continuing = t.hasPrefix(current) || current.hasPrefix(t)
        if continuing {
            if t.count >= current.count { current = t }
        } else {
            current = current + " " + t
        }
    }

    /// the recognizer is about to be restarted for a fresh segment — bank what we have
    mutating func sealSegment() {
        let t = current.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { current = ""; return }
        banked = banked.isEmpty ? t : banked + " " + t
        current = ""
    }
}
