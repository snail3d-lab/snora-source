import Foundation

/// ── the chron lane: farm schedules ──────────────────────────────────────
/// A schedule produces one clip on a timer and delivers it to a Discord
/// channel and/or to this app. It only ENQUEUES — the same queue, no priority —
/// and it never fires inside its quiet hours.

struct ScheduleTarget: Codable, Identifiable, Hashable {
    var kind: String                 // "discord" | "app"
    var guild_id: String?
    var guild_name: String?
    var channel_id: String?
    var channel_name: String?
    var id: String { "\(kind):\(guild_id ?? "")/\(channel_id ?? "")" }
    /// what to show in a list
    var label: String {
        if kind == "app" { return "this app" }
        let g = guild_name ?? ""
        let c = channel_name ?? channel_id ?? "?"
        return g.isEmpty ? "#\(c)" : "\(g) · #\(c)"
    }
}

struct Schedule: Codable, Identifiable {
    let id: String
    var name: String
    var kind: String?
    var enabled: Bool?
    var interval_hours: Int?
    var quiet_start: Int?
    var quiet_end: Int?
    var tz: String?
    var targets: [ScheduleTarget]?
    var created: Double?
    var last_run: Double?
    var last_job_id: String?
    var last_song_title: String?
    var last_song_lang: String?
    var next_run: Double?
    var delivered: Bool?
    var delivered_to: [String]?
    var duration: Int?
    var aspect: String?
    var quality: String?

    var isOn: Bool { enabled ?? false }
    var interval: Int { max(1, interval_hours ?? 2) }
    var quietStart: Int { quiet_start ?? 0 }
    var quietEnd: Int { quiet_end ?? 0 }

    /// "every 2h · quiet 00:00–08:00" — the shape of the schedule
    var cadence: String {
        let q = quietStart == quietEnd
            ? "always on"
            : String(format: "quiet %02d:00–%02d:00", quietStart, quietEnd)
        return "every \(interval)h · \(q)"
    }

    /// next fire time in the schedule's own zone, or "—"
    var nextText: String {
        guard let n = next_run else { return "—" }
        return Schedule.whenText(n, tz: tz)
    }
    var lastText: String {
        guard let l = last_run else { return "never run" }
        return "last " + Schedule.whenText(l, tz: tz)
    }
    /// true while the clock is inside the quiet window
    var quietNow: Bool {
        guard quietStart != quietEnd else { return false }
        let h = Schedule.hourNow(tz)
        return quietStart < quietEnd
            ? (h >= quietStart && h < quietEnd)
            : (h >= quietStart || h < quietEnd)
    }
    var kindLabel: String {
        switch kind ?? "singing_dog" {
        case "singing_dog": return "singing dog"
        case "custom": return "custom"
        default: return kind ?? "?"
        }
    }
    var deliveredText: String? {
        guard let d = delivered, d, let to = delivered_to, !to.isEmpty else { return nil }
        return "last sent to " + to.joined(separator: ", ")
    }

    static func whenText(_ ts: Double, tz: String?) -> String {
        let f = DateFormatter()
        f.dateFormat = "EEE HH:mm"
        f.timeZone = TimeZone(identifier: tz ?? "America/Denver") ?? .current
        return f.string(from: Date(timeIntervalSince1970: ts))
    }
    static func hourNow(_ tz: String?) -> Int {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: tz ?? "America/Denver") ?? .current
        return cal.component(.hour, from: Date())
    }
}

struct ScheduleList: Codable {
    let schedules: [Schedule]
    let describe: [String: String]?
    let now: Double?
    let tz: String?
}

struct DiscordGuild: Codable, Identifiable, Hashable {
    let id: String
    let name: String
}
struct GuildList: Codable { let guilds: [DiscordGuild] }

struct DiscordChannel: Codable, Identifiable, Hashable {
    let id: String
    let name: String
}
struct ChannelList: Codable { let channels: [DiscordChannel] }