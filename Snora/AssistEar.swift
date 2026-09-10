import AVFoundation
import Speech

/// Thread-safe holder so the audio thread can keep appending while the main
/// actor swaps which recognition request is live (segment rollover).
private final class AudioFeeder {
    private let lock = NSLock()
    private var request: SFSpeechAudioBufferRecognitionRequest?

    func set(_ request: SFSpeechAudioBufferRecognitionRequest?) {
        lock.lock(); self.request = request; lock.unlock()
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock(); let r = request; lock.unlock()
        r?.append(buffer)
    }
}

/// tap-to-talk ear: SFSpeechRecognizer live transcription of one utterance.
/// One take = begin() … finish().
///
/// Built around what Apple's on-device recognizer really does (the user's phone log,
/// 2026-09-09: 18-54 second takes came back with zero characters while the words
/// had been on screen): an `isFinal` can arrive **empty**, and `formattedString`
/// can restart mid-take. So the ear banks finished segments, ignores empty
/// hypotheses, and rolls the recognition task over every `segmentSeconds` before
/// the long-take limits bite. Words you saw are words you keep.
@MainActor
final class AssistEar: ObservableObject {
    @Published private(set) var listening = false
    /// true from the stop tap until the final words land
    @Published private(set) var stopping = false
    @Published private(set) var partial = ""
    /// why the last take produced nothing — surfaced instead of a blank "no words"
    @Published private(set) var diagnostic: String?
    /// live nudge while listening, so nobody talks into a dead microphone
    @Published private(set) var hint: String?

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let feeder = AudioFeeder()
    private var engine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var acc = TakeAccumulator()
    private var finishCont: CheckedContinuation<String, Never>?
    private var resumed = false
    private var stopRequested = false
    private var segmentTimer: Timer?
    private var silenceTimer: Timer?

    /// how long one recognition segment runs before we hand it a fresh task
    private let segmentSeconds: TimeInterval = 12
    /// how long we'll wait after the stop tap for the recognizer to finalize
    private let finishGrace: TimeInterval = 6.0
    /// say something within this many seconds or we nudge — no silent dead air
    private let silenceNudge: TimeInterval = 6.0
    /// a take longer than this is finished for you (mic light off, words kept)
    private let takeCeiling: TimeInterval = 180

    /// tap #1 — permissions, mic tap, live partials into `partial`.
    func begin() async throws {
        guard !listening, !stopping else { return }
        listening = true
        stopping = false
        stopRequested = false
        diagnostic = nil
        hint = nil
        partial = ""
        acc = TakeAccumulator()
        takeStarted = Date()
        resumed = false
        do {
            try await authorize()
            try startEngine()
            // a stop tap that landed during setup is a real take: end the audio
            // as soon as the mic is up; finish() is already waiting on it.
            if stopRequested { request?.endAudio() }
        } catch {
            resumeFinish(acc.best)
            teardown()
            listening = false
            stopping = false
            throw error
        }
    }

    /// tap #2 — ends audio, waits for the final words. Safe to hit during setup,
    /// and safe if it lands a beat before begin() has actually started.
    /// Returns nil only when the take genuinely had no words in it.
    func finish() async -> String? {
        // a stop tap that beat begin() still has to mean "end the take"
        stopRequested = true
        guard listening || stopping else { return nil }
        listening = false
        stopping = true
        if request != nil && engine != nil {
            request?.endAudio()   // begin() ends it for us if the mic isn't up yet
        }
        let text = await withCheckedContinuation { c in
            if resumed {
                c.resume(returning: acc.best)
            } else {
                finishCont = c
                // never hang, and never lose words we already saw
                DispatchQueue.main.asyncAfter(deadline: .now() + finishGrace) { [weak self] in
                    guard let self, !self.resumed else { return }
                    self.resumeFinish(self.acc.best)
                }
            }
        }
        stopping = false
        teardown()
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        AssistTrace.note("ear finished: \(t.count) chars, banked=\(!acc.banked.isEmpty), sawWords=\(acc.sawWords)")
        if t.isEmpty {
            diagnostic = acc.sawWords
                ? "the recognizer dropped the words it was showing — one more try usually lands it"
                : (diagnostic ?? "couldn't hear speech — tap the mic, then talk right away")
            AssistTrace.note("ear empty take → \(AssistTrace.clip(diagnostic ?? "none"))")
            return nil
        }
        return t
    }

    /// abandon the take without waiting (tab changed, sheet opened, …)
    func cancel() {
        guard listening || stopping else { return }
        listening = false
        stopping = false
        stopRequested = true
        request?.endAudio()
        task?.cancel()
        resumeFinish("")
        teardown()
    }

    // ── internals ──────────────────────────────────────────────────────

    private func authorize() async throws {
        let speech = await withCheckedContinuation { (c: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
            SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) }
        }
        guard speech == .authorized else {
            throw StudioError(status: nil, detail: "speech recognition is off — enable it in Settings › sNora")
        }
        let mic = await AVAudioApplication.requestRecordPermission()
        guard mic else {
            throw StudioError(status: nil, detail: "microphone access denied — enable it in Settings › sNora")
        }
    }

    private func startEngine() throws {
        guard let recognizer, recognizer.isAvailable else {
            throw StudioError(status: nil, detail: "speech recognition isn't available right now")
        }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .default, options: [.duckOthers])
        try session.setActive(true)

        // one engine, one tap, for the whole take — rollover swaps the request
        // the tap feeds, never the engine
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let fmt = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 2048, format: fmt) { [weak self] buf, _ in
            self?.feeder.append(buf)
        }
        engine.prepare()
        try engine.start()
        self.engine = engine

        AssistTrace.note("ear live (onDevice=\(recognizer.supportsOnDeviceRecognition ? "yes" : "no"))")
        startSegment()

        silenceTimer = Timer.scheduledTimer(withTimeInterval: silenceNudge, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.listening, !self.acc.sawWords else { return }
                self.hint = "still silence — speak right after you tap"
            }
        }
        segmentTimer = Timer.scheduledTimer(withTimeInterval: segmentSeconds, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.rollover() }
        }
    }

    /// a fresh request + task; the audio tap keeps feeding whatever is current
    private func startSegment() {
        guard let recognizer else { return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.addsPunctuation = true
        if recognizer.supportsOnDeviceRecognition {
            req.requiresOnDeviceRecognition = true   // no round-trip, works off-grid
        }
        request = req
        feeder.set(req)
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            // results from a segment we already retired are stale — drop them
            Task { @MainActor in
                guard let self, req === self.request else { return }
                self.handle(result: result, error: error)
            }
        }
    }

    /// bank this segment and start a clean one, without dropping the mic
    private func rollover() {
        guard listening, let old = request else { return }
        acc.sealSegment()
        partial = acc.best
        AssistTrace.note("segment rolled over at \(Int(Date().timeIntervalSince1970) % 10000)s, keeping \(acc.banked.count) chars")
        old.endAudio()
        startSegment()
        if takeElapsedTooLong() { finishForUser() }
    }

    private var takeStarted = Date()

    private func takeElapsedTooLong() -> Bool {
        Date().timeIntervalSince(takeStarted) > takeCeiling
    }

    /// the ceiling was hit: stop recording, keep everything heard so far
    private func finishForUser() {
        AssistTrace.note("take hit the \(Int(takeCeiling))s ceiling — finishing it for him")
        hint = "that's a long one — wrapping it up"
        request?.endAudio()
        stopTimers()
        resumeFinish(acc.best)
    }

    private func handle(result: SFSpeechRecognitionResult?, error: Error?) {
        if let r = result {
            acc.note(r.bestTranscription.formattedString)
            partial = acc.best
            if acc.sawWords { hint = nil }
            if r.isFinal { resumeFinish(acc.best) }
        }
        if error != nil {
            // don't swallow it — "no words" without a reason is undebuggable
            diagnostic = Self.describe(error!)
            AssistTrace.note("recognizer error: \(AssistTrace.clip(diagnostic ?? "?"))")
            resumeFinish(acc.best)
        }
    }

    private func resumeFinish(_ s: String) {
        guard !resumed else { return }
        resumed = true
        stopTimers()
        finishCont?.resume(returning: s)
        finishCont = nil
    }

    private func stopTimers() {
        segmentTimer?.invalidate(); segmentTimer = nil
        silenceTimer?.invalidate(); silenceTimer = nil
    }

    private func teardown() {
        stopTimers()
        if let engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        feeder.set(nil)
        engine = nil
        task = nil
        request = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// human phrasing for the recognizer's opaque errors
    private static func describe(_ e: Error) -> String {
        let ns = e as NSError
        switch (ns.domain, ns.code) {
        case ("kAFAssistantErrorDomain", 1110): return "couldn't hear speech — tap the mic, then talk right away"
        case ("kAFAssistantErrorDomain", 209): return "the recognizer needs the on-device speech model — one short retry usually fixes it"
        case ("kAFAssistantErrorDomain", 1101): return "speech recognition timed out — try again"
        case ("kAFAssistantErrorDomain", 216): return "speech recognition is not available right now — try again"
        case ("kAFAssistantErrorDomain", 203): return "Siri & Dictation is restricted — check Settings › Screen Time"
        default: return "recognizer said: \(ns.localizedDescription) (domain \(ns.domain), code \(ns.code))"
        }
    }
}
