import Foundation

/// Append-only trace inside the app's own container, so a bug that only happens
/// on the phone can be read from the Mac:
///   xcrun devicectl device copy from --device <UDID> \
///     --domain-type appDataContainer --domain-identifier com.snail3d.snora \
///     --source Documents/assist_trace.log --destination /tmp/assist_trace.log
/// Keep it cheap: one line per step, flushed per write so a crash loses nothing.
enum AssistTrace {
    private static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    private static var url: URL? {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first?
            .appendingPathComponent("assist_trace.log")
    }

    /// `phase` is not stored — callers pass a short label so the log reads like a story
    static func note(_ text: String) {
        guard let url else { return }
        let line = "\(stamp.string(from: Date())) \(text)\n"
        if let h = try? FileHandle(forWritingTo: url) {
            defer { try? h.close() }
            try? h.seekToEnd()
            try? h.write(contentsOf: Data(line.utf8))
        } else {
            try? line.data(using: .utf8)?.write(to: url)
        }
    }

    /// Truncated body text — long planner payloads would drown the log.
    static func clip(_ s: String, _ n: Int = 120) -> String {
        let t = s.replacingOccurrences(of: "\n", with: " ")
        return t.count <= n ? t : String(t.prefix(n)) + "…"
    }
}
