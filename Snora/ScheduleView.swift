import SwiftUI

/// ── CHRON: the farm's schedules ─────────────────────────────────────────
/// Schedules only ever ENQUEUE — the same queue as every other render, no
/// priority — and quiet hours are enforced by the farm, including on "run now".
/// This tab lists them, shows which Discord each one posts into, and lets one be
/// created, paused, run or deleted from the phone.

struct ScheduleView: View {
    private let token = API.token

    @State private var schedules: [Schedule] = []
    @State private var describe: [String: String] = [:]
    @State private var busy = false
    @State private var err = ""
    @State private var note = ""
    @State private var showAdd = false
    @State private var editing: Schedule?

    var body: some View {
        NavigationStack {
            ZStack {
                SnoraBackground()
                ScrollView {
                    VStack(spacing: 14) {
                        if !err.isEmpty { banner(err, color: Theme.ember) }
                        if !note.isEmpty { banner(note, color: Theme.crt) }
                        if schedules.isEmpty && !busy {
                            empty
                        }
                        ForEach(schedules) { s in
                            card(s)
                        }
                        addButton
                    }
                    .padding(16)
                }
                .refreshable { await load() }
            }
            .navigationTitle("Chron")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await load() } } label: {
                        Image(systemName: "arrow.clockwise").tint(Theme.cream)
                    }
                }
            }
            .task { await load() }
            .sheet(isPresented: $showAdd) {
                ScheduleEditView(existing: nil) { await load() }
            }
            .sheet(item: $editing) { s in
                ScheduleEditView(existing: s) { await load() }
            }
        }
    }

    // ── pieces ──────────────────────────────────────────────────────────

    private func banner(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(color)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
    }

    private var empty: some View {
        VStack(spacing: 10) {
            Image(systemName: "clock.arrow.circlepath")
                .font(.system(size: 34)).foregroundStyle(Theme.sand)
            Text("no schedules yet")
                .font(.snoraTag).foregroundStyle(Theme.cream).kerning(1.2)
            Text("A schedule renders on a timer and delivers the clip to a Discord "
                 + "channel, to this app, or both. It only queues — it never jumps "
                 + "ahead of your live renders.")
                .font(.footnote).foregroundStyle(Theme.sand)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(24)
        .snoraCard()
    }

    private func card(_ s: Schedule) -> some View {
        SectionCard(title: s.name, footnote: s.deliveredText) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    badge(s.isOn ? "ON" : "PAUSED",
                          color: s.isOn ? Theme.crt : Theme.sand)
                    badge(s.kindLabel, color: Theme.sand)
                    if s.quietNow { badge("QUIET NOW", color: Theme.ember) }
                    Spacer(minLength: 0)
                }
                Text(s.cadence)
                    .font(.footnote).foregroundStyle(Theme.cream)
                HStack(spacing: 12) {
                    Label(s.nextText, systemImage: "clock")
                        .font(.snoraTag).foregroundStyle(Theme.crt)
                    Text(s.lastText).font(.snoraTag).foregroundStyle(Theme.sand)
                }
                if let title = s.last_song_title {
                    Text("last: \(title)"
                         + (s.last_song_lang.map { " (\($0))" } ?? ""))
                        .font(.snoraTag).foregroundStyle(Theme.sand)
                }
                if let targets = s.targets, !targets.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("delivers to").font(.snoraLabel)
                            .foregroundStyle(Theme.sand).kerning(1.0)
                        ForEach(targets) { t in
                            HStack(spacing: 6) {
                                Image(systemName: t.kind == "app"
                                      ? "iphone" : "bubble.left.and.bubble.right")
                                    .font(.caption2).foregroundStyle(Theme.sand)
                                Text(t.label)
                                    .font(.snoraTag).foregroundStyle(Theme.cream)
                            }
                        }
                    }
                }
                HStack(spacing: 8) {
                    Chip(title: s.isOn ? "PAUSE" : "RESUME", selected: false, compact: true) {
                        Task { await act(s, "toggle") }
                    }
                    Chip(title: "RUN NOW", selected: false, compact: true) {
                        Task { await act(s, "run") }
                    }
                    Chip(title: "EDIT", selected: false, compact: true) {
                        editing = s
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private func badge(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.snoraTag).kerning(1.0)
            .foregroundStyle(color)
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(color.opacity(0.14), in: Capsule())
    }

    private var addButton: some View {
        Button { showAdd = true } label: {
            HStack(spacing: 8) {
                Image(systemName: "plus.circle.fill")
                Text("NEW SCHEDULE").kerning(1.5)
            }
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(Theme.cream)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .background(Theme.signal, in: RoundedRectangle(cornerRadius: 12))
        }
        .padding(.top, 4)
    }

    // ── actions ─────────────────────────────────────────────────────────

    private func load() async {
        busy = true; err = ""
        do {
            let (list, desc) = try await API.schedules(token: token)
            schedules = list
            describe = desc
        } catch {
            err = "couldn't load schedules — \(API.friendlyURLError(error as? URLError ?? URLError(.unknown)))"
        }
        busy = false
    }

    private func act(_ s: Schedule, _ action: String) async {
        err = ""; note = ""
        do {
            _ = try await API.scheduleAction(token: token, id: s.id, action: action)
            note = action == "run" ? "queued — it renders in turn, at normal priority"
                                   : "updated"
            await load()
        } catch {
            err = error.localizedDescription
        }
    }
}

// ── create / edit one schedule ──────────────────────────────────────────

struct ScheduleEditView: View {
    let existing: Schedule?
    let onDone: () async -> Void

    @Environment(\.dismiss) private var dismiss
    private let token = API.token

    @State private var name = ""
    @State private var kind = "singing_dog"
    @State private var interval = 2
    @State private var quietStart = 0
    @State private var quietEnd = 8
    @State private var duration = 8
    @State private var quality = "standard"

    @State private var toApp = false
    @State private var guilds: [DiscordGuild] = []
    @State private var channels: [DiscordChannel] = []
    @State private var guild: DiscordGuild?
    @State private var channel: DiscordChannel?
    @State private var extraTargets: [ScheduleTarget] = []

    @State private var busy = false
    @State private var err = ""

    private let intervals = [1, 2, 3, 4, 6, 8, 12, 24]

    var body: some View {
        NavigationStack {
            ZStack {
                SnoraBackground()
                ScrollView {
                    VStack(spacing: 14) {
                        if !err.isEmpty {
                            Text(err).font(.footnote).foregroundStyle(Theme.ember)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        SectionCard(title: "what") {
                            VStack(alignment: .leading, spacing: 10) {
                                TextField("name (e.g. Viking Singing Dogs)", text: $name)
                                    .textFieldStyle(.plain)
                                    .padding(10)
                                    .background(Theme.ink.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
                                    .foregroundStyle(Theme.cream)
                                ChipRow(label: "kind", options: [
                                    ("singing dog", "singing_dog"),
                                    ("custom", "custom"),
                                ], selection: $kind)
                            }
                        }
                        SectionCard(title: "when") {
                            VStack(alignment: .leading, spacing: 12) {
                                ChipRow(label: "every", options:
                                    intervals.map { ("\($0)h", $0) }, selection: $interval)
                                ChipRow(label: "quiet starts", options:
                                    [0, 6, 8, 10, 20, 22, 23].map { (String(format: "%02d:00", $0), $0) },
                                    selection: $quietStart)
                                ChipRow(label: "quiet ends", options:
                                    [6, 7, 8, 9, 10, 12].map { (String(format: "%02d:00", $0), $0) },
                                    selection: $quietEnd)
                                Text("Nothing fire between those hours — quiet hours are "
                                     + "enforced by the farm, including on RUN NOW.")
                                    .font(.snoraTag).foregroundStyle(Theme.sand)
                            }
                        }
                        SectionCard(title: "shot") {
                            VStack(alignment: .leading, spacing: 12) {
                                ChipRow(label: "length", options:
                                    [( "5s", 5), ("8s", 8), ("10s", 10), ("15s", 15)],
                                    selection: $duration)
                                ChipRow(label: "quality", options:
                                    [("fast", "fast"), ("standard", "standard"), ("best", "best")],
                                    selection: $quality)
                                Text("Singing dogs are always 9:16 vertical.")
                                    .font(.snoraTag).foregroundStyle(Theme.sand)
                            }
                        }
                        deliverCard
                        SectionCard(title: "added targets") {
                            if extraTargets.isEmpty {
                                Text("none yet — pick a server and channel above to add one")
                                    .font(.snoraTag).foregroundStyle(Theme.sand)
                            } else {
                                ForEach(extraTargets) { t in
                                    HStack {
                                        Text(t.label).font(.snoraTag).foregroundStyle(Theme.cream)
                                        Spacer()
                                        Button { extraTargets.removeAll { $0.id == t.id } } label: {
                                            Image(systemName: "xmark.circle.fill")
                                                .foregroundStyle(Theme.ember)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    .padding(16)
                }
            }
            .navigationTitle(existing == nil ? "New Schedule" : "Edit Schedule")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }.tint(Theme.sand)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await save() } } label: {
                        if busy { ProgressView().tint(Theme.cream) }
                        else { Text("Save").bold().tint(Theme.cream) }
                    }
                    .disabled(busy || name.isEmpty)
                }
            }
            .task { await bootstrap() }
        }
    }

    private var deliverCard: some View {
        SectionCard(title: "deliver to",
                    footnote: "Every connected Discord is listed. Pick a server, then a "
                             + "channel, and ADD it — you can add several.") {
            VStack(alignment: .leading, spacing: 12) {
                ChipRow(label: "this app", options: [("on", true), ("off", false)],
                        selection: $toApp)
                if !guilds.isEmpty {
                    ChipRow(label: "server", options: guilds.map { ($0.name, $0.id) },
                            selection: Binding(
                                get: { guild?.id ?? "" },
                                set: { v in
                                    guild = guilds.first { $0.id == v }
                                    channel = nil
                                    Task { await loadChannels() }
                                }))
                }
                if !channels.isEmpty {
                    ChipRow(label: "channel", options: channels.map { ($0.name, $0.id) },
                            selection: Binding(
                                get: { channel?.id ?? "" },
                                set: { v in channel = channels.first { $0.id == v } }))
                }
                if guild != nil && channel != nil {
                    Chip(title: "ADD TARGET", selected: false, compact: true) {
                        if let g = guild, let c = channel,
                           !extraTargets.contains(where: { $0.channel_id == c.id }) {
                            extraTargets.append(ScheduleTarget(
                                kind: "discord", guild_id: g.id, guild_name: g.name,
                                channel_id: c.id, channel_name: c.name))
                        }
                    }
                }
            }
        }
    }

    private func bootstrap() async {
        if let s = existing {
            name = s.name
            kind = s.kind ?? "singing_dog"
            interval = s.interval
            quietStart = s.quietStart
            quietEnd = s.quietEnd
            duration = s.duration ?? 8
            quality = s.quality ?? "standard"
            let t = s.targets ?? []
            toApp = t.contains { $0.kind == "app" }
            extraTargets = t.filter { $0.kind == "discord" }
        }
        do { guilds = try await API.guilds(token: token) }
        catch { err = "couldn't reach Discord — \(error.localizedDescription)" }
    }

    private func loadChannels() async {
        guard let g = guild else { channels = []; return }
        do { channels = try await API.channels(token: token, guildID: g.id) }
        catch { channels = []; err = "couldn't list channels for \(g.name)" }
    }

    private func save() async {
        busy = true; err = ""
        var targets: [[String: Any]] = extraTargets.map {
            ["kind": "discord", "guild_id": $0.guild_id ?? "", "guild_name": $0.guild_name ?? "",
             "channel_id": $0.channel_id ?? "", "channel_name": $0.channel_name ?? ""]
        }
        if toApp { targets.append(["kind": "app"]) }
        if targets.isEmpty {
            err = "pick at least one delivery target — a Discord channel, this app, or both"
            busy = false
            return
        }
        let body: [String: Any] = [
            "name": name, "kind": kind, "interval_hours": interval,
            "quiet_start": quietStart, "quiet_end": quietEnd,
            "duration": duration, "quality": quality,
            "aspect": "vertical",
            "tz": TimeZone.current.identifier,
            "targets": targets,
        ]
        do {
            if let s = existing {
                // edits go through the same create door (the farm replaces the record)
                _ = try await API.scheduleAction(token: token, id: s.id, action: "delete")
            }
            _ = try await API.scheduleAction(token: token, body: body)
            await onDone()
            dismiss()
        } catch {
            err = error.localizedDescription
        }
        busy = false
    }
}