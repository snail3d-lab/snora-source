import Photos
import AVKit
import AVFoundation
import SwiftUI
import Combine

/// ── root tabs ─────────────────────────────────────────────────────────
struct RootView: View {
    @ObservedObject private var bridge = PhotoBridge.shared
    @State private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            ReelView()
                .tabItem { Label("Reel", systemImage: "play.rectangle.on.rectangle") }
                .tag(0)
            CreateView()
                .tabItem { Label("Create", systemImage: "video.badge.plus") }
                .tag(1)
            PhotosView()
                .tabItem { Label("Photos", systemImage: "photo.on.rectangle") }
                .tag(2)
        }
        .tint(Theme.signal)
        .preferredColorScheme(.dark)
        // a photo tapped "USE FOR VIDEO" rides the bridge into Create
        .onChange(of: bridge.pendingReference != nil) { _, isSet in
            if isSet { tab = 1 }
        }
    }
}

/// ── the reel: every clip, newest first ────────────────────────────────
struct ReelView: View {
    let token = API.token
    @StateObject var poller = Poller.shared
    @State private var jobs: [Job] = []
    @State private var showWorking = false
    @State private var playing: Job?
    @State private var query = ""
    @State private var farmNote: String?
    @State private var expandedIDs: Set<String> = []
    @State private var stopTarget: Job?
    @State private var showClearQueue = false
    @State private var deleteTarget: Job?
    @State private var queueNote: String?

    var visible: [Job] {
        // ALL shows everything — failed jobs included, because they now explain
        // themselves. COOKING narrows to what's queued or rendering.
        var list = showWorking ? jobs.filter { $0.isWorking } : jobs
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        if !q.isEmpty { list = list.filter { $0.prompt.lowercased().contains(q) } }
        return list
    }

    /// how many jobs are sitting in line — the "clear queue" offer and its
    /// confirmation both speak this number
    var queuedCount: Int {
        jobs.filter { $0.status == "queued" }.count
    }

    var body: some View {
        NavigationStack {
            ZStack {
                SnoraBackground()
                if jobs.isEmpty {
                    emptyState
                } else if visible.isEmpty {
                    VStack(spacing: 10) {
                        Image(systemName: "magnifyingglass").font(.title)
                            .foregroundStyle(Theme.sand.opacity(0.6))
                        Text("nothing matches \"\(query)\"")
                            .font(.snoraLabel).foregroundStyle(Theme.sand)
                            .multilineTextAlignment(.center)
                    }
                    .padding()
                } else {
                    List(visible) { job in
                        JobRow(job: job, token: token,
                               expanded: expandedIDs.contains(job.id),
                               onTap: { playing = job },
                               onToggle: { flip(job.id) },
                               onUnqueue: { Task { await unqueue(job.id) } },
                               onStop: { stopTarget = job },
                               onDelete: { deleteTarget = job })
                            .listRowSeparator(.hidden)
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 5, leading: 16, bottom: 5, trailing: 16))
                            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                if job.status == "queued" {
                                    Button(role: .destructive) {
                                        Task { await unqueue(job.id) }
                                    } label: { Label("unqueue", systemImage: "slash.circle") }
                                        .tint(Theme.ember)
                                } else if job.status == "rendering" {
                                    // stopping a drawing is deliberate — confirm first
                                    Button(role: .destructive) {
                                        stopTarget = job
                                    } label: { Label("stop render", systemImage: "stop.circle.fill") }
                                        .tint(Theme.ember)
                                } else {
                                    Button(role: .destructive) {
                                        Task { await destroy(job.id) }
                                    } label: { Label("delete", systemImage: "trash") }
                                }
                            }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
            .navigationTitle("sNora")
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "search the reel")
            .toolbar {
                // clear the queue — only offered when something is actually waiting
                ToolbarItem(placement: .topBarLeading) {
                    if queuedCount > 0 {
                        Button { showClearQueue = true } label: {
                            Text("CLEAR QUEUE")
                                .font(.snoraTag).kerning(1.2)
                                .foregroundStyle(Theme.cream)
                                .padding(.horizontal, 11).padding(.vertical, 6)
                                .background(Capsule().fill(Theme.ember.opacity(0.85)))
                                .overlay(Capsule().stroke(Theme.ember, lineWidth: 1))
                        }
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 6) {
                        Chip(title: "ALL", selected: !showWorking, compact: true) {
                            withAnimation { showWorking = false }
                        }
                        Chip(title: "COOKING", selected: showWorking, compact: true) {
                            withAnimation { showWorking = true }
                        }
                    }
                }
            }
            .refreshable { await load() }
            .alert("the farm said no", isPresented: Binding(
                get: { farmNote != nil },
                set: { if !$0 { farmNote = nil } })) {
                Button("ok", role: .cancel) {}
            } message: {
                Text(farmNote ?? "")
            }
            .confirmationDialog("stop this render?", isPresented: Binding(
                get: { stopTarget != nil },
                set: { if !$0 { stopTarget = nil } }), titleVisibility: .visible) {
                Button("stop it", role: .destructive) {
                    if let j = stopTarget { Task { await stopRender(j.id) } }
                }
                Button("let it keep drawing", role: .cancel) {}
            } message: {
                Text("the farm is in the middle of drawing this video. stopping means this one gets thrown away.")
            }
            .confirmationDialog("delete this for good?", isPresented: Binding(
                get: { deleteTarget != nil },
                set: { if !$0 { deleteTarget = nil } }), titleVisibility: .visible) {
                Button("delete it", role: .destructive) {
                    if let j = deleteTarget { Task { await destroy(j.id) } }
                }
                Button("keep it", role: .cancel) {}
            } message: {
                Text("the video goes away on your Mac and in the cloud too. the photos you put into it stay.")
            }
            .confirmationDialog("clear the queue?", isPresented: $showClearQueue,
                                titleVisibility: .visible) {
                Button("take them out of line", role: .destructive) {
                    Task { await clearQueue() }
                }
                Button("keep them waiting", role: .cancel) {}
            } message: {
                Text("take \(queuedCount) waiting job\(queuedCount == 1 ? "" : "s") out of line? the video being drawn right now keeps going.")
            }
            .alert("queue cleared", isPresented: Binding(
                get: { queueNote != nil },
                set: { if !$0 { queueNote = nil } })) {
                Button("ok", role: .cancel) {}
            } message: {
                Text(queueNote ?? "")
            }
            .sheet(item: $playing) { job in PlayerSheet(job: job, token: token) }
            .task { await load(); poller.start(token: token) }
        }
        .preferredColorScheme(.dark)
    }

    /// yank a queued job out of line (a running render can't be un-started)
    private func unqueue(_ id: String) async {
        do {
            try await API.cancel(token: token, id: id)
            withAnimation { jobs.removeAll { $0.id == id } }
        } catch {
            farmNote = error.localizedDescription
        }
    }

    /// permanent delete: farm record + the clip on the Mac + the cloud copy
    private func destroy(_ id: String) async {
        do {
            try await API.delete(token: token, id: id)
            withAnimation { jobs.removeAll { $0.id == id } }
        } catch {
            farmNote = error.localizedDescription
        }
    }

    /// flip a card open in place — tap the same row again to close it
    private func flip(_ id: String) {
        withAnimation(.spring(duration: 0.28)) {
            if !expandedIDs.insert(id).inserted {
                expandedIDs.remove(id)
            }
        }
    }

    /// stopping a render kills the drawing mid-flight; the job comes back
    /// as failed, so reload the whole reel instead of pulling one row out.
    private func stopRender(_ id: String) async {
        do {
            try await API.cancel(token: token, id: id)
            await load()
        } catch {
            farmNote = error.localizedDescription
        }
    }

    /// pull every waiting job out of line; the one rendering keeps going
    private func clearQueue() async {
        do {
            let out = try await API.clearQueue(token: token)
            withAnimation { jobs.removeAll { out.contains($0.id) } }
            queueNote = "\(out.count) job\(out.count == 1 ? "" : "s") came out of the queue"
        } catch {
            farmNote = error.localizedDescription
        }
    }

    private var emptyState: some View {
        VStack(spacing: 18) {
            PlayBadge(size: 92)
            Text("nothing on the reel")
                .font(.title3.weight(.semibold))
                .foregroundStyle(Theme.cream)
            Text("head to create and make your first one")
                .font(.snoraLabel)
                .foregroundStyle(Theme.sand)
                .kerning(0.6)
                .textCase(.uppercase)
        }
    }

    private func load() async {
        jobs = (try? await API.jobs(token: token)) ?? jobs
        await poller.check(token: token)
    }
}

/// ── one broadcast per card ────────────────────────────────────────────
// Deliberately NO inline AVPlayer: players were being re-created on every
// SwiftUI redraw, which left them failed-to-load (the "no entry" glyph).
// Rows show a first-frame poster; the one real player lives in PlayerSheet.
struct JobRow: View {
    let job: Job
    let token: String
    var expanded: Bool
    var onTap: () -> Void
    var onToggle: () -> Void
    /// labelled buttons inside an open card — a hidden swipe is not a control
    /// anyone can find
    var onUnqueue: () -> Void
    var onStop: () -> Void
    var onDelete: () -> Void

    @State private var poster: UIImage?
    @State private var posterFailed = false
    @State private var preparing = false
    @State private var note: String?

    /// the danger button for this job's state, spelled out in words
    @ViewBuilder private var cardActions: some View {
        if job.status == "queued" {
            Button { onUnqueue() } label: {
                Label("take out of the queue", systemImage: "slash.circle")
            }
            .buttonStyle(EmberButtonStyle())
        } else if job.status == "rendering" {
            Button { onStop() } label: {
                Label("stop this render", systemImage: "stop.circle.fill")
            }
            .buttonStyle(EmberButtonStyle())
        } else {
            Button { onDelete() } label: {
                Label("delete this", systemImage: "trash")
            }
            .buttonStyle(EmberButtonStyle())
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // tapping the words flips the card open; tapping the poster plays
            Button(action: onToggle) {
                HStack(alignment: .top, spacing: 8) {
                    Text(job.prompt)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.cream)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    Spacer(minLength: 4)
                    statusPill
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Theme.sand.opacity(0.7))
                        .padding(.top, 6)
                }
            }
            .buttonStyle(.plain)
            HStack(spacing: 6) {
                if job.has_image == true {
                    MetaIcon(system: "photo")
                }
                if job.has_voice == true {
                    MetaIcon(system: "mic.fill")
                }
                if job.has_end == true {
                    MetaTag(text: "BEGIN\u{2192}END")
                }
                if let q = job.quality {
                    MetaTag(text: q.uppercased())
                }
                if let a = job.aspect {
                    MetaTag(text: aspectLabel(a))
                }
                if let d = job.duration {
                    MetaTag(text: "\(d)S")
                }
                Spacer(minLength: 0)
                if let g = job.genSeconds {
                    MetaTag(text: renderLabel(job))
                }
            }
            if job.isDone {
                ZStack {
                    RoundedRectangle(cornerRadius: 12)
                        .fill(Color.black)
                    if let p = poster {
                        Image(uiImage: p).resizable().scaledToFit()
                    } else if preparing {
                        ProgressView().tint(Theme.signal)
                    } else {
                        Image(systemName: "play.rectangle.on.rectangle")
                            .font(.system(size: 30))
                            .foregroundStyle(Theme.sand.opacity(posterFailed ? 0.35 : 0.6))
                    }
                    Image(systemName: "play.circle.fill")
                        .font(.system(size: 46))
                        .foregroundStyle(Theme.signal.opacity(0.95), Theme.bgBottom.opacity(0.55))
                }
                .aspectRatio(job.ratio, contentMode: .fit)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))
                .contentShape(Rectangle())
                .onTapGesture { onTap() }
                .task(id: job.id) { await preparePoster() }
                if let n = note {
                    Text(n).font(.caption).foregroundStyle(Theme.sand).lineLimit(2)
                }
            }
            if job.isWorking {
                if job.status == "rendering", let p = job.progress, p.pct != nil {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 8) {
                            BlinkDot()
                            Text("the farm is on air — \(p.stage ?? "rendering")")
                                .font(.caption).foregroundStyle(Theme.sand).lineLimit(1)
                            Spacer()
                            Text("\u{25B0}\u{25B1} \(Int(p.clamped))%")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(Theme.sand)
                        }
                        ProgressView(value: p.clamped, total: 100)
                            .tint(Theme.signal)
                        if let eta = p.etaText {
                            Text(eta).font(.caption2)
                                .foregroundStyle(Theme.sand.opacity(0.7))
                        }
                    }
                } else {
                    HStack(spacing: 8) {
                        BlinkDot()
                        Text(job.status == "rendering" ? "the farm is on air…" : "in the queue…")
                            .font(.caption).foregroundStyle(Theme.sand)
                        Spacer()
                        ProgressView().scaleEffect(0.8).tint(Theme.signal)
                    }
                }
            }
            // short version on a closed card; the full story is inside
            if !expanded, job.status == "failed", let r = job.failureReason {
                Text(r).font(.caption).foregroundStyle(Theme.ember.opacity(0.9)).lineLimit(2)
            }
            if expanded {
                JobDetail(job: job, token: token)
                cardActions
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .snoraCard()
        .padding(.vertical, 4)
    }

    /// "1m54s" / "48s"
    private func clock(_ s: Double) -> String {
        let t = Int(s.rounded())
        return t >= 60 ? "\(t / 60)m\(t % 60)s" : "\(t)s"
    }

    /// render clock is GPU-only; when queueing/upload made the wall clock
    /// meaningfully longer, say so — the user times these with a stopwatch
    private func renderLabel(_ job: Job) -> String {
        guard let g = job.genSeconds else { return "" }
        var label = "RENDER \(clock(g))"
        if let c = job.created, let f = job.finished, f - c - g >= 15 {
            label += " · WAIT \(clock(f - c - g))"
        }
        return label
    }

    private func aspectLabel(_ a: String) -> String {
        switch a {
        case "portrait": return "TALL"
        case "square": return "SQ"
        default: return "WIDE"
        }
    }

    private var statusPill: some View {
        StatusPill(status: job.status)
    }

    private func preparePoster() async {
        guard job.isDone, poster == nil, !posterFailed, !preparing else { return }
        preparing = true
        defer { preparing = false }
        do {
            let file = try await API.videoFile(token: token, id: job.id, remote: job.url ?? "")
            poster = try await API.firstFrame(of: file)
        } catch {
            posterFailed = true
            note = "preview didn't load — \(error.localizedDescription)"
        }
    }
}

struct MetaIcon: View {
    let system: String
    var body: some View {
        Image(systemName: system)
            .font(.system(size: 10))
            .foregroundStyle(Theme.sand)
            .padding(6)
            .background(Circle().fill(Theme.bgBottom.opacity(0.6)))
            .overlay(Circle().stroke(Theme.cardStroke, lineWidth: 1))
    }
}

/// ── fullscreen playback: one player, save + share ─────────────────────
struct PlayerSheet: View {
    let job: Job
    let token: String
    @Environment(\.dismiss) var dismiss
    @State private var player: AVPlayer?
    @State private var err: String?
    @State private var saving = false
    @State private var saved = false
    @State private var shareURL: URL?

    var body: some View {
        ZStack {
            SnoraBackground()
            VStack(spacing: 16) {
                HStack {
                    Button { dismiss() } label: {
                        Text("CLOSE")
                            .font(.snoraTag).kerning(1.2)
                            .foregroundStyle(Theme.cream)
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .background(Capsule().fill(Theme.signal.opacity(0.85)))
                    }
                    Spacer()
                    Button { saveVideo() } label: {
                        HStack(spacing: 5) {
                            if saving {
                                ProgressView().tint(Theme.cream).scaleEffect(0.7)
                            } else {
                                Image(systemName: saved ? "checkmark" : "square.and.arrow.down")
                                Text(saved ? "SAVED" : "SAVE")
                            }
                        }
                        .font(.snoraTag).kerning(1.2)
                        .foregroundStyle(Theme.cream)
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(Capsule().fill(saved ? Theme.crt.opacity(0.8) : Theme.signal.opacity(0.85)))
                    }
                    .disabled(saving || saved)
                    Button { shareVideo() } label: {
                        Image(systemName: "square.and.arrow.up")
                            .font(.snoraTag)
                            .foregroundStyle(Theme.cream)
                            .padding(10)
                            .background(Circle().fill(Theme.card.opacity(0.9)))
                            .overlay(Circle().stroke(Theme.cardStroke, lineWidth: 1))
                    }
                    .disabled(player == nil)
                }
                .padding(.horizontal)
                if let e = err {
                    VStack(spacing: 12) {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.title).foregroundStyle(Theme.ember)
                        Text(e).font(.footnote).foregroundStyle(Theme.cream)
                            .multilineTextAlignment(.center)
                        Chip(title: "TRY AGAIN", selected: false) { start() }
                    }
                    .padding()
                    .frame(maxWidth: .infinity, minHeight: 220)
                } else if let p = player {
                    VideoPlayer(player: p).aspectRatio(job.ratio, contentMode: .fit).padding(.horizontal)
                        .onAppear { p.play() }
                } else {
                    ProgressView().tint(Theme.signal).frame(height: 200)
                }
                Text(job.prompt).font(.footnote).foregroundStyle(Theme.sand).padding(.horizontal)
                Spacer()
            }
            .padding(.top, 8)
        }
        .sheet(isPresented: Binding(get: { shareURL != nil }, set: { if !$0 { shareURL = nil } })) {
            if let u = shareURL {
                ShareSheet(items: [u])
            }
        }
        .task { start() }
    }

    /// one player, created once; failures land on screen in plain words
    private func start() {
        err = nil
        player = nil
        Task {
            do {
                let u = try await API.videoFile(token: token, id: job.id, remote: job.url ?? "")
                // after a voice take the session is still .playAndRecord, which routes
                // video audio to the earpiece — force the loudspeaker path
                try? AVAudioSession.sharedInstance().setCategory(.playback)
                try? AVAudioSession.sharedInstance().setActive(true)
                let p = AVPlayer(url: u)
                player = p
                guard let item = p.currentItem else { return }
                for await st in item.publisher(for: \.status).values {
                    if st == .readyToPlay { break }
                    if st == .failed {
                        err = item.error?.localizedDescription ?? "the video couldn't be played"
                        break
                    }
                }
            } catch {
                err = error.localizedDescription
            }
        }
    }

    /// download the clip into the iPhone's photo library
    private func saveVideo() {
        guard !saving, !saved else { return }
        saving = true
        err = nil
        Task {
            do {
                let u = try await API.videoFile(token: token, id: job.id, remote: job.url ?? "")
                try await PHPhotoLibrary.shared().performChanges {
                    PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: u)
                }
                saved = true
            } catch {
                err = "save failed — \(error.localizedDescription)"
            }
            saving = false
        }
    }

    /// hand the downloaded file to the system share sheet (messages, mail, …)
    private func shareVideo() {
        Task {
            do {
                shareURL = try await API.videoFile(token: token, id: job.id, remote: job.url ?? "")
            } catch {
                err = error.localizedDescription
            }
        }
    }
}

/// ── the system share sheet ────────────────────────────────────────────
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_: UIActivityViewController, context _: Context) {}
}

/// ── the flip-open detail: prompt, settings, timeline, why, pictures ────
struct JobDetail: View {
    let job: Job
    let token: String

    /// clock format for the timestamps — "2:04 pm"
    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.timeStyle = .short
        f.dateStyle = .none
        return f
    }()

    private static func time(_ ts: Double?) -> String? {
        guard let ts else { return nil }
        return clock.string(from: Date(timeIntervalSince1970: ts))
    }

    /// "1m24s" / "48s"
    private func span(_ s: Double) -> String {
        let t = Int(s.rounded())
        guard t >= 0 else { return "0s" }
        return t >= 60 ? "\(t / 60)m\(t % 60)s" : "\(t)s"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionCard(title: "the whole prompt") {
                Text(job.prompt)
                    .font(.subheadline)
                    .foregroundStyle(Theme.cream)
                    .textSelection(.enabled) // long prompts can be copied
                    .fixedSize(horizontal: false, vertical: true)
            }
            SectionCard(title: "the settings",
                        footnote: "how the farm was told to draw this one") {
                settings
            }
            if Self.time(job.created) != nil || Self.time(job.started) != nil {
                SectionCard(title: "the timeline") {
                    timeline
                }
            }
            if let reason = job.failureReason {
                SectionCard(title: "why it failed") {
                    Text(reason)
                        .font(.subheadline)
                        .foregroundStyle(Theme.ember.opacity(0.95))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if job.refCount > 0 {
                SectionCard(title: "the reference pictures",
                            footnote: "the photos this video was drawn from — tap one to see it full screen") {
                    RefStrip(job: job, token: token)
                }
            }
        }
    }

    private var settings: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 76), spacing: 6)],
                  alignment: .leading, spacing: 6) {
            if let d = job.duration {
                MetaTag(text: "\(d) SECONDS")
            }
            if let q = job.quality {
                MetaTag(text: q.uppercased())
            }
            if let a = job.aspect {
                MetaTag(text: shape(a))
            }
            if let r = job.ref_mode {
                MetaTag(text: r == "identity" ? "SAME PERSON" : "NEW SCENE")
            }
            if let b = job.backend {
                MetaTag(text: b.uppercased())
            }
        }
    }

    private func shape(_ a: String) -> String {
        switch a {
        case "portrait": return "TALL"
        case "square": return "SQUARE"
        default: return "WIDE"
        }
    }

    private var timeline: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let v = Self.time(job.created) {
                Text("put in line at \(v)")
            }
            if let v = Self.time(job.started) {
                Text("the farm started drawing at \(v)")
            }
            if let v = Self.time(job.finished) {
                Text(job.status == "failed" ? "it gave up at \(v)" : "finished at \(v)")
            }
            if let c = job.created, let s = job.started, s > c {
                Text("waited in line \(span(s - c))")
            }
            if let g = job.genSeconds {
                Text("drawing took \(span(g))")
            }
        }
        .font(.subheadline)
        .foregroundStyle(Theme.sand)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// ── the photos a job was drawn from (loads only when the card is open) ─
struct RefStrip: View {
    let job: Job
    let token: String

    @State private var refs: [RefInfo]?
    @State private var failed = false
    @State private var zoom: UIImage?

    var body: some View {
        content
            .task(id: job.id) { await load() }
            .sheet(isPresented: Binding(get: { zoom != nil }, set: { if !$0 { zoom = nil } })) {
                if let img = zoom {
                    PhotoZoom(image: img)
                }
            }
    }

    @ViewBuilder private var content: some View {
        if let list = refs {
            if list.isEmpty {
                Text("the photos are gone from the farm")
                    .font(.caption).foregroundStyle(Theme.sand)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(list) { info in
                            RefThumb(token: token, jobID: job.id, info: info) { img in zoom = img }
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        } else if failed {
            VStack(alignment: .leading, spacing: 8) {
                Text("the pictures didn't load")
                    .font(.caption).foregroundStyle(Theme.ember.opacity(0.9))
                Chip(title: "TRY AGAIN", selected: false, compact: true) { load() }
            }
        } else {
            HStack(spacing: 8) {
                ProgressView().tint(Theme.signal).scaleEffect(0.8)
                Text("finding the pictures…")
                    .font(.caption).foregroundStyle(Theme.sand)
            }
        }
    }

    private func load() {
        failed = false
        Task {
            do {
                refs = try await API.refs(token: token, id: job.id)
            } catch {
                failed = true
            }
        }
    }
}

/// one reference thumbnail — bytes come from the farm once and are cached
struct RefThumb: View {
    let token: String
    let jobID: String
    let info: RefInfo
    var onZoom: (UIImage) -> Void

    @State private var full: UIImage?
    @State private var thumb: UIImage?
    @State private var broken = false

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 10).fill(Theme.bgBottom.opacity(0.6))
            if let thumb {
                Image(uiImage: thumb).resizable().scaledToFill()
            } else if broken {
                Image(systemName: "photo")
                    .font(.system(size: 22))
                    .foregroundStyle(Theme.sand.opacity(0.4))
            } else {
                ProgressView().tint(Theme.signal).scaleEffect(0.7)
            }
        }
        .frame(width: 96, height: 96)
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.cardStroke, lineWidth: 1))
        .onTapGesture { if let full { onZoom(full) } }
        .task(id: info.i) { await load() }
    }

    private func load() async {
        guard full == nil, !broken else { return }
        do {
            let d = try await API.refImage(token: token, id: jobID, i: info.i)
            guard let img = UIImage(data: d) else { throw SubmitError(status: nil, detail: "the picture wasn't readable") }
            full = img
            thumb = RefThumb.thumbnail(img)
        } catch {
            broken = true
        }
    }

    /// small copy for the strip so the list stays light on memory
    static func thumbnail(_ img: UIImage, maxDim: CGFloat = 192) -> UIImage {
        let s = min(1, maxDim / max(img.size.width, img.size.height))
        guard s < 1 else { return img }
        let size = CGSize(width: img.size.width * s, height: img.size.height * s)
        let fmt = UIGraphicsImageRendererFormat.default()
        fmt.scale = 1
        return UIGraphicsImageRenderer(size: size, format: fmt).image { _ in
            img.draw(in: CGRect(origin: .zero, size: size))
        }
    }
}

/// ── fullscreen look at a reference picture ────────────────────────────
struct PhotoZoom: View {
    let image: UIImage
    @Environment(\.dismiss) var dismiss

    var body: some View {
        ZStack {
            SnoraBackground()
            VStack(spacing: 16) {
                Button { dismiss() } label: {
                    Text("CLOSE")
                        .font(.snoraTag).kerning(1.2)
                        .foregroundStyle(Theme.cream)
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(Capsule().fill(Theme.signal.opacity(0.85)))
                }
                Spacer()
                Image(uiImage: image).resizable().scaledToFit()
                    .padding(.horizontal, 12)
                Spacer()
            }
            .padding(.top, 12)
        }
        .onTapGesture { dismiss() }
    }
}
