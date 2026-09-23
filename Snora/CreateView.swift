import AVFoundation
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct CreateView: View {
    let token = API.token
    @State private var prompt = ""
    @State private var quality = "standard"
    @State private var aspect = "wide"
    @State private var duration = 5
    @State private var photos: [UIImage] = []
    @State private var lastIsEnding = false
    @State private var refMode = "scene"
    @State private var voiceURL: URL?
    @State private var voiceLabel = ""
    @State private var isRecording = false
    @State private var recordSeconds = 0
    @State private var recorder: AVAudioRecorder?
    @State private var recordTimer: Timer?
    @State private var audioPlayer: AVAudioPlayer?
    @State private var showPicker = false
    @State private var showCamera = false
    @State private var showAudioPicker = false
    @State private var busy = false
    @State private var msg = ""
    @FocusState private var promptFocused: Bool
    @ObservedObject private var bridge = PhotoBridge.shared
    @StateObject private var ear = AssistEar()
    @State private var assistBusy = false
    @State private var assistLine = ""
    /// the words the ear actually handed us, kept on screen so a bad plan can be
    /// traced to either the recognizer or the planner
    @State private var assistHeard = ""
    @State private var editingPhoto: FrameImage?

    private let maxPhotos = 9

    var body: some View {
        NavigationStack {
            ZStack {
                SnoraBackground()
                ScrollView {
                    VStack(spacing: 14) {
                        ideaCard
                        if ear.listening || ear.stopping || assistBusy || !assistLine.isEmpty {
                            assistCard
                        }
                        photoCard
                        lookCard
                        voiceCard
                        Button { submit() } label: {
                            HStack(spacing: 8) {
                                if busy {
                                    ProgressView().tint(Theme.cream)
                                } else {
                                    Image(systemName: "play.fill")
                                    Text("RENDER IT").kerning(1.5)
                                }
                            }
                            .font(.system(.body, design: .monospaced).weight(.bold))
                        }
                        .buttonStyle(SignalButtonStyle())
                        .opacity(busy || prompt.trimmingCharacters(in: .whitespaces).isEmpty ? 0.5 : 1)
                        .disabled(busy || prompt.trimmingCharacters(in: .whitespaces).isEmpty)
                        if !msg.isEmpty {
                            Text(msg)
                                .font(.footnote)
                                .foregroundStyle(Theme.sand)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 4)
                        }
                    }
                    .padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
                .toolbar {
                    ToolbarItemGroup(placement: .keyboard) {
                        Spacer()
                        Button { promptFocused = false } label: {
                            Text("DONE")
                                .font(.snoraTag).kerning(1.2)
                                .foregroundStyle(Theme.cream)
                                .padding(.horizontal, 12).padding(.vertical, 6)
                                .background(Capsule().fill(Theme.signal.opacity(0.85)))
                        }
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        micDial
                    }
                }
            }
            .navigationTitle("Create")
            .sheet(isPresented: $showPicker) { MultiPhotoPicker(limit: maxPhotos - photos.count) { photos.append(contentsOf: $0) } }
            .sheet(isPresented: $showCamera) { CameraPicker { photos.append($0) } }
            .sheet(isPresented: $showAudioPicker) { AudioFilePicker { url in useAudio(url) } }
            .sheet(item: $editingPhoto) { target in
                PhotoEditView(initialImage: target.image)
            }
            .onAppear { consumeBridge() }
            .onChange(of: bridge.pendingReference != nil) { _, _ in consumeBridge() }
            .onDisappear { stopRecording(save: false); ear.cancel() }
        }
        .preferredColorScheme(.dark)
    }

    /// ── the idea ──────────────────────────────────────────────────────
    private var ideaCard: some View {
        SectionCard(title: "the idea",
                    footnote: "say it naturally — the assistant tidies your words, then picks the right engine.") {
            ZStack(alignment: .topLeading) {
                if prompt.isEmpty {
                    Text("a barn at golden hour, baby snail in a tiny hat…")
                        .font(.body).foregroundStyle(Theme.sand.opacity(0.5))
                        .padding(.top, 8).padding(.leading, 5)
                        .allowsHitTesting(false)
                }
                TextEditor(text: $prompt)
                    .scrollContentBackground(.hidden)
                    .foregroundStyle(Theme.cream)
                    .frame(minHeight: 90)
                    .focused($promptFocused)
            }
        }
    }

    /// ── the dials ─────────────────────────────────────────────────────
    private var lookCard: some View {
        SectionCard(title: "look") {
            VStack(alignment: .leading, spacing: 14) {
                ChipRow(label: "quality", options: [
                    ("fast", "fast"), ("standard", "standard"), ("best", "best"),
                ], selection: $quality)
                ChipRow(label: "shape", options: [
                    ("wide", "wide"), ("tall", "portrait"), ("square", "square"),
                ], selection: $aspect)
                ChipRow(label: "length", options: [
                    ("3s", 3), ("5s", 5), ("10s", 10), ("15s", 15),
                ], selection: $duration)
            }
        }
    }

    /// ── photos: pick, mark up, and say what they mean ─────────────────
    private var photoCard: some View {
        SectionCard(title: "photos  \(photos.count)/\(maxPhotos)",
                    footnote: "tap a photo to circle and change it. first photo anchors the video; the rest are extra refs.")
        {
            if !photos.isEmpty {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 72), spacing: 8)], spacing: 10) {
                    ForEach(photos.indices, id: \.self) { i in
                        Image(uiImage: photos[i])
                            .resizable().scaledToFill()
                            .frame(width: 72, height: 72)
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                            .overlay(RoundedRectangle(cornerRadius: 10)
                                .stroke(Theme.cardStroke, lineWidth: 1))
                            .overlay(alignment: .topTrailing) {
                                Button { _ = photos.remove(at: i) } label: {
                                    Image(systemName: "xmark.circle.fill")
                                        .font(.system(size: 17))
                                        .foregroundStyle(Theme.signal, Theme.bgBottom)
                                }
                                .offset(x: 6, y: -6)
                            }
                            .overlay(alignment: .bottomTrailing) {
                                Image(systemName: "pencil.tip.crop.circle")
                                    .font(.system(size: 16, weight: .semibold))
                                    .foregroundStyle(Theme.cream)
                                    .padding(4)
                                    .background(Circle().fill(Theme.card.opacity(0.85)))
                                    .padding(4)
                            }
                            .contentShape(RoundedRectangle(cornerRadius: 10))
                            .onTapGesture {
                                promptFocused = false
                                editingPhoto = FrameImage(image: photos[i])
                            }
                            .contextMenu {
                                Button {
                                    editingPhoto = FrameImage(image: photos[i])
                                } label: {
                                    Label("mark up", systemImage: "pencil.tip.crop.circle")
                                }
                                Button(role: .destructive) {
                                    _ = photos.remove(at: i)
                                } label: {
                                    Label("remove", systemImage: "trash")
                                }
                            }
                    }
                }
            }
            HStack(spacing: 10) {
                Button { promptFocused = false; showCamera = true } label: {
                    Label("camera", systemImage: "camera")
                }
                .buttonStyle(GhostButtonStyle())
                .disabled(photos.count >= maxPhotos || !UIImagePickerController.isSourceTypeAvailable(.camera))
                Button { promptFocused = false; showPicker = true } label: {
                    Label("library", systemImage: "photo.on.rectangle")
                }
                .buttonStyle(GhostButtonStyle())
                .disabled(photos.count >= maxPhotos)
            }
            if !photos.isEmpty {
                VStack(spacing: 8) {
                    OptionRow(title: "the photo is the scene",
                              subtitle: "start in the photo's setting",
                              selected: refMode == "scene") {
                        withAnimation { refMode = "scene" }
                    }
                    OptionRow(title: "the photo is who it is",
                              subtitle: "the photo defines the subject — scene comes from the idea",
                              selected: refMode == "identity") {
                        withAnimation { refMode = "identity" }
                    }
                }
                .padding(.top, 4)
            }
            if photos.count >= 2 {
                Toggle(isOn: $lastIsEnding) {
                    Text(lastIsEnding
                         ? "begin\u{2192}end \u{2014} photo 1 opens the clip, the last photo closes it (photos between are ignored)"
                         : "last photo is the ending frame")
                        .font(.footnote).foregroundStyle(Theme.sand)
                }
                .tint(Theme.signal)
            }
        }
    }

    /// ── voice ────────────────────────────────────────────────────────
    private var voiceCard: some View {
        SectionCard(title: "voice (optional)",
                    footnote: (voiceURL != nil || isRecording)
                        ? "voice rides on a person in your photos — add at least one photo above"
                        : nil)
        {
            VStack(spacing: 10) {
                if isRecording {
                    HStack(spacing: 10) {
                        BlinkDot(color: Theme.ember)
                        Text(String(format: "REC  %02d / 15", recordSeconds))
                            .font(.snoraTag).kerning(1.2).foregroundStyle(Theme.ember)
                        Spacer()
                        Button("stop") { stopRecording(save: true) }
                            .buttonStyle(GhostButtonStyle())
                            .fixedSize()
                    }
                } else if let vu = voiceURL {
                    HStack(spacing: 10) {
                        Button { togglePlay() } label: {
                            Image(systemName: (audioPlayer?.isPlaying ?? false) ? "stop.circle.fill" : "play.circle.fill")
                                .font(.title2)
                                .foregroundStyle(Theme.signal)
                        }
                        Text(voiceLabel.isEmpty ? vu.lastPathComponent : voiceLabel)
                            .font(.subheadline).foregroundStyle(Theme.cream)
                            .lineLimit(1)
                        Spacer()
                        Button(role: .destructive) {
                            audioPlayer?.stop(); audioPlayer = nil
                            try? FileManager.default.removeItem(at: vu)
                            voiceURL = nil; voiceLabel = ""
                        } label: {
                            Image(systemName: "trash").foregroundStyle(Theme.ember)
                        }
                    }
                }
                HStack(spacing: 10) {
                    Button { startRecording() } label: { Label("record", systemImage: "mic") }
                        .buttonStyle(GhostButtonStyle())
                        .disabled(isRecording)
                    Button { promptFocused = false; showAudioPicker = true } label: { Label("audio file", systemImage: "music.note") }
                        .buttonStyle(GhostButtonStyle())
                }
            }
        }
    }

    /// ── recording ─────────────────────────────────────────────────────
    private func startRecording() {
        AVAudioApplication.requestRecordPermission { granted in
            Task { @MainActor in
                guard granted else {
                    msg = "microphone access denied — enable it in Settings"
                    return
                }
                do {
                    let session = AVAudioSession.sharedInstance()
                    try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
                    try session.setActive(true)
                    let url = FileManager.default.temporaryDirectory
                        .appendingPathComponent("snora_voice_\(Int(Date().timeIntervalSince1970)).m4a")
                    let rec = try AVAudioRecorder(url: url, settings: [
                        AVFormatIDKey: kAudioFormatMPEG4AAC,
                        AVSampleRateKey: 44100,
                        AVNumberOfChannelsKey: 1,
                        AVEncoderBitRateKey: 96000,
                        AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
                    ])
                    recorder = rec
                    voiceURL = url
                    voiceLabel = ""
                    recordSeconds = 0
                    rec.record()
                    isRecording = true
                    recordTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
                        recordSeconds += 1
                        if recordSeconds >= 15 {
                            stopRecording(save: true)
                        }
                    }
                } catch {
                    msg = "couldn't start recording: \(error.localizedDescription)"
                }
            }
        }
    }

    private func stopRecording(save: Bool) {
        recordTimer?.invalidate(); recordTimer = nil
        guard isRecording, let rec = recorder else { return }
        rec.stop()
        isRecording = false
        if save {
            voiceLabel = "recording — \(max(recordSeconds, 1))s"
        } else if let u = recorder?.url, voiceURL == nil || voiceLabel.isEmpty {
            try? FileManager.default.removeItem(at: u)
            voiceURL = nil
        }
        recorder = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func useAudio(_ url: URL) {
        guard let d = try? Data(contentsOf: url) else { msg = "couldn't read that file"; return }
        if d.count > 1_100_000 {
            msg = "audio file too big — 15 seconds of voice is plenty, trim it down"
            return
        }
        audioPlayer?.stop(); audioPlayer = nil
        voiceURL = url
        voiceLabel = url.lastPathComponent
    }

    private func togglePlay() {
        if let p = audioPlayer, p.isPlaying {
            p.stop(); audioPlayer = nil; return
        }
        guard let u = voiceURL else { return }
        audioPlayer = try? AVAudioPlayer(contentsOf: u)
        audioPlayer?.play()
    }

    /// ── AI ASSIST: a round tap-toggle mic up in the bar ───────────────
    /// Tap once to talk, tap again when you're done. Hold-to-talk was too
    /// finicky — a sliding thumb lost whole sentences.
    private var micDial: some View {
        Button { toggleAssist() } label: {
            ZStack {
                Circle()
                    .fill(ear.listening ? Theme.signal : Theme.card.opacity(0.85))
                    .frame(width: 34, height: 34)
                Circle()
                    .stroke(ear.listening ? Theme.signal.opacity(0.45) : Theme.cardStroke, lineWidth: 1)
                if assistBusy || ear.stopping {
                    ProgressView().tint(Theme.cream).scaleEffect(0.7)
                } else {
                    Image(systemName: ear.listening ? "stop.fill" : "mic.fill")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(ear.listening ? Theme.cream : Theme.sand)
                }
            }
        }
        .disabled(assistBusy || ear.stopping)
        .accessibilityLabel(ear.listening ? "stop talking" : ear.stopping ? "finishing" : "talk to the assistant")
    }

    private func toggleAssist() {
        promptFocused = false
        AssistTrace.note("mic tapped — listening=\(ear.listening) stopping=\(ear.stopping) photos=\(photos.count)")
        if ear.listening { endAssist() } else { beginAssist() }
    }

    private var assistCard: some View {
        SectionCard(title: "ai assist") {
            VStack(alignment: .leading, spacing: 8) {
                if ear.stopping {
                    HStack(alignment: .top, spacing: 8) {
                        ProgressView().tint(Theme.signal).scaleEffect(0.8)
                        Text("finishing your words… “\(ear.partial.isEmpty ? "…" : ear.partial)”")
                            .font(.subheadline)
                            .foregroundStyle(Theme.cream)
                            .lineLimit(3)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else if ear.listening {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .top, spacing: 8) {
                            BlinkDot()
                            Text(ear.partial.isEmpty
                                ? "speaking freely — no rush, tap the square when you're done"
                                : "“\(ear.partial)”")
                                .font(.subheadline)
                                .foregroundStyle(Theme.cream)
                                .lineLimit(4)
                        }
                        // a nudge if the mic is hearing nothing, so nobody talks
                        // into a dead microphone for a minute wondering
                        if let hint = ear.hint {
                            Text(hint)
                                .font(.caption)
                                .foregroundStyle(Theme.sand.opacity(0.8))
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else if assistBusy {
                    HStack(alignment: .top, spacing: 8) {
                        ProgressView().tint(Theme.signal).scaleEffect(0.8)
                        Text(assistLine.isEmpty ? "working…" : assistLine)
                            .font(.subheadline)
                            .foregroundStyle(Theme.cream)
                            .lineLimit(4)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    Text(assistLine)
                        .font(.footnote)
                        .foregroundStyle(Theme.sand)
                        .lineLimit(5)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if !assistHeard.isEmpty && !ear.listening && !ear.stopping {
                    // "heard" is what the phone decided, not a complaint — say
                    // what it did with it
                    Text("you said: “\(assistHeard)”")
                        .font(.caption)
                        .foregroundStyle(Theme.sand.opacity(0.85))
                        .lineLimit(3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                // one tap to send the mic log when a take went wrong
                Button {
                    UIPasteboard.general.string = AssistAPI.logUrl
                    msg = "log link copied — paste it to me"
                    AssistTrace.note("log link copied")
                } label: {
                    Text("copy the mic log link")
                        .font(.caption2)
                        .foregroundStyle(Theme.sand.opacity(0.6))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func beginAssist() {
        guard !assistBusy, !ear.listening, !ear.stopping else { return }
        promptFocused = false
        assistLine = ""
        assistHeard = ""
        AssistTrace.note("begin requested (photos=\(photos.count))")
        Task {
            do {
                try await ear.begin()
                AssistTrace.note("ear live")
            } catch {
                assistLine = "listening won't start — \(error.localizedDescription)"
                AssistTrace.note("begin FAILED: \(AssistTrace.clip(error.localizedDescription))")
            }
        }
    }

    private func endAssist() {
        Task {
            guard let transcript = await ear.finish() else {
                if !assistBusy {
                    // prefer the recognizer's own reason (didn't hear, needs the
                    // on-device model, restricted) over a generic nag
                    assistLine = ear.diagnostic ?? "didn't catch any words — tap the mic and talk right after"
                }
                AssistTrace.note("finish → no words (diagnostic: \(AssistTrace.clip(ear.diagnostic ?? "none")))")
                return
            }
            assistHeard = transcript
            AssistTrace.note("finish → heard \"\(AssistTrace.clip(transcript))\"")
            await planAndQueue(transcript: transcript)
        }
    }

    /// transcript → planner → run every job in order, one status line the whole way
    @MainActor private func planAndQueue(transcript: String) async {
        assistBusy = true
        assistLine = "thinking about it…"
        defer { assistBusy = false }
        do {
            AssistTrace.note("plan sent: \(AssistTrace.clip(transcript)) | photos=\(photos.count) voice=\(voiceURL != nil)")
            let plan = try await AssistAPI.plan(
                transcript: transcript,
                hasPhotos: photos.count,
                hasVoice: voiceURL != nil,
                current: AssistAPI.FormState(
                    prompt: prompt, quality: quality, aspect: aspect,
                    duration: duration, refMode: refMode))
            AssistTrace.note("plan back: \(plan.jobs.count) jobs [\(plan.jobs.map(\.kind).joined(separator: ","))] msg=\"\(AssistTrace.clip(plan.message))\"")
            if plan.jobs.isEmpty {
                assistLine = plan.message.isEmpty ? "nothing to queue" : plan.message
                return
            }
            assistLine = "queueing \(plan.jobs.count) render\(plan.jobs.count == 1 ? "" : "s")…"
            var queued = 0
            var failures: [String] = []
            for (i, job) in plan.jobs.enumerated() {
                assistLine = "queueing \(i + 1) of \(plan.jobs.count)…"
                do {
                    try await executeAssistJob(job)
                    queued += 1
                    AssistTrace.note("job \(i + 1) [\(job.kind)] ok")
                } catch {
                    let why = jobReason(error, kind: job.kind)
                    failures.append("#\(i + 1) failed: \(why)")
                    AssistTrace.note("job \(i + 1) [\(job.kind)] FAILED: \(AssistTrace.clip(why))")
                }
            }
            if failures.isEmpty {
                assistLine = plan.message.isEmpty ? "queued \(plan.jobs.count) — check the reel"
                    : "queued \(plan.jobs.count) — \(plan.message)"
            } else {
                assistLine = "\(queued) of \(plan.jobs.count) queued — " + failures.joined(separator: " · ")
            }
        } catch let e as URLError {
            assistLine = AssistAPI.friendly(e)
            AssistTrace.note("plan FAILED: \(AssistTrace.clip(e.localizedDescription))")
        } catch {
            assistLine = "assist failed — \(error.localizedDescription)"
            AssistTrace.note("assist FAILED: \(AssistTrace.clip(error.localizedDescription))")
        }
    }

    /// one planned job → the right farm. Video via the sora worker, photos via the studio.
    @MainActor private func executeAssistJob(_ job: AssistJob) async throws {
        switch job.kind {
        case "video":
            let usePhotos = job.use_form_photos ?? true
            let useVoice = job.use_form_voice ?? true
            let useEnding = usePhotos && lastIsEnding && photos.count >= 2
            _ = try await API.submit(
                token: token,
                prompt: job.prompt,
                quality: job.quality ?? quality,
                aspect: job.aspect ?? aspect,
                duration: job.duration.map(Int.init) ?? duration,
                photos: useEnding ? Array(photos.dropLast()) : (usePhotos ? photos : []),
                endingPhoto: useEnding ? photos.last : nil,
                voiceURL: (useVoice && !photos.isEmpty) ? voiceURL : nil,
                refMode: job.ref_mode ?? refMode)
        case "photo_qwen":
            try await runPhotoJob(job, engine: "QWEN")
        case "qwen_edit":
            try await runPhotoJob(job, engine: "QWEN_EDIT")
        case "qwen_multi":
            try await runPhotoJob(job, engine: "QWEN_MULTI")
        case "qwen_sticker":
            try await runPhotoJob(job, engine: "QWEN_STICKER")
        case "photo_flux":
            try await runPhotoJob(job, engine: "FLUX")
        case "img2img":
            try await runPhotoJob(job, engine: "IMG2IMG")
        default:
            throw StudioError(status: nil, detail: "unknown job kind \"\(job.kind)\"")
        }
    }

    /// photo job → studio (GPU-guarded note) → download → the reel
    @MainActor private func runPhotoJob(_ job: AssistJob, engine: String) async throws {
        let pend = PhotoStore.shared.beginPending(prompt: job.prompt, engine: engine)
        defer { PhotoStore.shared.endPending(pend) }
        let asp = job.aspect ?? aspect
        // one GPU, one job — wait out any running video render so stills and
        // clips flow through the same line instead of fighting for the Mac
        var waited = 0
        while true {
            guard let live = try? await API.jobs(token: token),
                  live.contains(where: { $0.isWorking }) else { break }
            if waited == 0 { assistLine = "waiting for the video render to finish…" }
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            waited += 8
            if waited > 1800 { break }
        }
        let q = job.quality ?? quality
        let result: StudioGenResult
        switch engine {
        case "QWEN":
            result = try await StudioAPI.generateQwen(
                prompt: job.prompt, aspect: asp, quality: q,
                transparent: job.transparent ?? false)
        case "QWEN_STICKER":
            result = try await StudioAPI.generateQwen(
                prompt: job.prompt, aspect: asp, quality: q, transparent: true)
        case "QWEN_EDIT":
            guard let ref = photos.first else {
                throw StudioError(status: nil, detail: "qwen edit needs a reference photo — add one above first")
            }
            result = try await StudioAPI.generateQwen(
                prompt: job.prompt, aspect: asp, quality: q,
                transparent: job.transparent ?? false, references: [ref])
        case "QWEN_MULTI":
            guard photos.count >= 2 else {
                throw StudioError(status: nil, detail: "qwen multi needs at least two reference photos")
            }
            result = try await StudioAPI.generateQwen(
                prompt: job.prompt, aspect: asp, quality: q,
                transparent: job.transparent ?? false, references: Array(photos.prefix(10)))
        case "ERNIE":
            result = try await StudioAPI.generateErnie(prompt: job.prompt, aspect: asp)
        case "IMG2IMG":
            guard let ref = photos.first else {
                throw StudioError(status: nil, detail: "img2img needs a reference photo — add one above first")
            }
            result = try await StudioAPI.generateFluxImg2Img(
                image: ref, prompt: job.prompt,
                strength: job.strength ?? 0.35, aspect: asp)
        default:
            result = try await StudioAPI.generateFlux(prompt: job.prompt, aspect: asp)
        }
        assistLine = "downloading photo…"
        let img = try await StudioAPI.fetchImage(path: result.image)
        PhotoStore.shared.add(remotePath: result.image, prompt: job.prompt,
                              engine: engine.replacingOccurrences(of: "_", with: " "), image: img)
    }

    private func jobReason(_ e: Error, kind: String) -> String {
        if let s = e as? StudioError { return s.localizedDescription }
        if let u = e as? URLError {
            return kind == "video" ? API.friendlyURLError(u)
                : "can't reach the studio — is your Mac on?"
        }
        return e.localizedDescription
    }

    /// a photo tapped USE FOR VIDEO in the reel — set it as reference #1
    private func consumeBridge() {
        guard let img = bridge.pendingReference else { return }
        bridge.pendingReference = nil
        if photos.count < maxPhotos {
            photos.insert(img, at: 0)
        } else {
            photos[0] = img
        }
        msg = "photo added as your first reference"
    }

    /// ── submit ────────────────────────────────────────────────────────
    // Every typed idea goes through the assistant first: it tidies the words,
    // picks video vs. still, and honors the form dials — same brain as the mic.
    private func submit() {
        msg = ""
        promptFocused = false
        let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if voiceURL != nil, photos.isEmpty {
            msg = "a voice needs a reference photo — the voice rides on someone"
            return
        }
        busy = true
        Task {
            await planAndQueue(transcript: text)
            busy = false
        }
    }
}

/// ── multi-photo picker (no permission needed) ───────────────────────
struct MultiPhotoPicker: UIViewControllerRepresentable {
    var limit: Int
    var onPicked: ([UIImage]) -> Void

    func makeUIViewController(context: Context) -> PHPickerViewController {
        var cfg = PHPickerConfiguration()
        cfg.filter = .images
        cfg.selectionLimit = max(limit, 1)
        let p = PHPickerViewController(configuration: cfg)
        p.delegate = context.coordinator
        return p
    }

    func updateUIViewController(_: PHPickerViewController, context _: Context) {}
    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    final class Coordinator: NSObject, PHPickerViewControllerDelegate {
        let parent: MultiPhotoPicker
        init(_ p: MultiPhotoPicker) {
            parent = p
        }

        func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
            picker.dismiss(animated: true)
            let group = DispatchGroup()
            var found: [UIImage] = []
            let lock = NSLock()
            for r in results where r.itemProvider.canLoadObject(ofClass: UIImage.self) {
                group.enter()
                r.itemProvider.loadObject(ofClass: UIImage.self) { obj, _ in
                    if let img = obj as? UIImage {
                        lock.lock(); found.append(img); lock.unlock()
                    }
                    group.leave()
                }
            }
            group.notify(queue: .main) { [weak self] in
                guard let self else { return }
                self.parent.onPicked(found)
            }
        }
    }
}

/// ── camera ──────────────────────────────────────────────────────────
struct CameraPicker: UIViewControllerRepresentable {
    var onPicked: (UIImage) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController()
        p.sourceType = .camera
        p.delegate = context.coordinator
        return p
    }
    func updateUIViewController(_: UIImagePickerController, context _: Context) {}
    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraPicker
        init(_ p: CameraPicker) {
            parent = p
        }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any])
        {
            if let img = info[.originalImage] as? UIImage {
                parent.onPicked(img)
            }
            picker.dismiss(animated: true)
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            picker.dismiss(animated: true)
        }
    }
}

/// ── audio file picker ───────────────────────────────────────────────
struct AudioFilePicker: UIViewControllerRepresentable {
    var onPicked: (URL) -> Void

    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let p = UIDocumentPickerViewController(forOpeningContentTypes: [.audio], asCopy: true)
        p.delegate = context.coordinator
        return p
    }

    func updateUIViewController(_: UIDocumentPickerViewController, context _: Context) {}
    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let parent: AudioFilePicker
        init(_ p: AudioFilePicker) {
            parent = p
        }

        func documentPicker(_: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            if let u = urls.first {
                parent.onPicked(u)
            }
        }
    }
}
