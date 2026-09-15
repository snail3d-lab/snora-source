import AVFoundation
import Foundation
import UIKit

struct JobProgress: Codable {
    let pct: Int?
    let stage: String?
    let eta: Int?

    var clamped: Double { Double(max(0, min(100, pct ?? 0))) }

    /// honest ETA: the farm's percentages restart each pipeline step,
    /// so this reads “seconds left in THIS step”
    var etaText: String? {
        guard let e = eta, e >= 0 else { return nil }
        return e >= 60 ? String(format: "~%dm%02ds left", e / 60, e % 60)
                       : "~\(e)s left"
    }
}

struct Job: Identifiable, Codable {
    let id: String
    let prompt: String
    let status: String
    let quality: String?
    let aspect: String?
    let duration: Int?
    let created: Double?
    let started: Double?
    let finished: Double?
    let failed_reason: String?
    let has_image: Bool?
    let has_voice: Bool?
    let backend: String?
    let ref_mode: String?
    let ref_count: Int?
    let error: String?
    var url: String?

    var videoURL: URL? {
        url.flatMap { URL(string: "https://YOUR-BACKEND.example.com\($0)") }
    }

    var isDone: Bool {
        status == "done"
    }

    var isWorking: Bool {
        status == "queued" || status == "rendering"
    }

    /// the farm's own words about a failure — `error` is what the service
    /// reports now; `failed_reason` is the older name. either will do.
    var failureReason: String? {
        error ?? failed_reason
    }

    /// how many reference pictures this video was drawn from. older job
    /// records only said "has a picture" — count that as one.
    var refCount: Int {
        if let c = ref_count { return c }
        return has_image == true ? 1 : 0
    }

    /// canvas ratio for playback (approximate — matches the farm's aspect dials)
    var ratio: CGFloat {
        switch aspect {
        case "portrait": return 480.0 / 704.0
        case "square": return 1.0
        default: return 832.0 / 480.0
        }
    }

    /// how long the farm took to make this clip — render time when we know when
    /// it started, otherwise created→finished (queue + render).
    var genSeconds: Double? {
        guard let f = finished else { return nil }
        return max(0, f - (started ?? created ?? f))
    }
}

/// the real reason a call failed — surfaced in the UI instead of a dead-end message
struct SubmitError: LocalizedError {
    let status: Int?
    let detail: String
    var errorDescription: String? {
        if let s = status {
            return detail.isEmpty ? "server said HTTP \(s)" : "server said HTTP \(s) — \(detail)"
        }
        return detail
    }
}

/// one reference picture on a job, as the farm lists it:
/// an index into the job's own pictures, its name, and its byte size.
struct RefInfo: Codable, Identifiable {
    let i: Int
    let name: String
    let bytes: Int
    var id: Int { i }
}

/// the shape of `GET /api/sora/refs/<id>`
struct RefList: Codable {
    let refs: [RefInfo]
}

enum API {
    static let base = "https://YOUR-BACKEND.example.com/api/sora"

    /// baked farm token — the single source of truth.
    /// never read from UserDefaults: a stale unlock-era token used to silently override it.
    static let token = "REPLACE_WITH_YOUR_SNORA_TOKEN"

    static func unlock(_ pw: String) async throws -> String {
        var req = URLRequest(url: URL(string: "\(base)/unlock")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(["password": pw])
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard Self.ok(resp) else { throw URLError(.userAuthenticationRequired) }
        let out = try JSONDecoder().decode([String: String].self, from: data)
        guard let t = out["token"] else { throw URLError(.badServerResponse) }
        return t
    }

    static func jobs(token: String) async throws -> [Job] {
        var req = URLRequest(url: URL(string: base)!)
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        let (data, _) = try await URLSession.shared.data(for: req)
        let raw = try JSONDecoder().decode([String: [Job]].self, from: data)
        return (raw["jobs"] ?? []).sorted { ($0.created ?? 0) > ($1.created ?? 0) }
    }

    /// worker returns 201 Created on success — accept any 2xx
    static func ok(_ resp: URLResponse) -> Bool {
        guard let h = resp as? HTTPURLResponse else { return false }
        return (200 ..< 300).contains(h.statusCode)
    }

    /// human phrasing for network failures so the UI can show a real reason
    static func friendlyURLError(_ e: URLError) -> String {
        switch e.code {
        case .timedOut: return "timed out — this connection is too slow for the photos, try wi-fi"
        case .notConnectedToInternet: return "no internet connection"
        case .cannotFindHost, .cannotConnectToHost, .dnsLookupFailed: return "can't reach the server"
        case .networkConnectionLost: return "connection dropped mid-upload"
        default: return e.localizedDescription
        }
    }

    /// downscale + compress until the base64 data URL fits the worker's 500K-char cap
    static func imageDataURL(_ image: UIImage) -> String? {
        let maxDim: CGFloat = 1024
        let scale = min(1, maxDim / max(image.size.width, image.size.height))
        let target = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let fmt = UIGraphicsImageRendererFormat.default()
        fmt.scale = 1
        let scaled = UIGraphicsImageRenderer(size: target, format: fmt).image { _ in image.draw(in: CGRect(origin: .zero, size: target)) }
        for q in [0.8, 0.6, 0.45, 0.3] {
            guard let d = scaled.jpegData(compressionQuality: q) else { return nil }
            let b64 = d.base64EncodedString()
            if b64.count < 450_000 {
                return "data:image/jpeg;base64," + b64
            }
        }
        return nil
    }

    /// mime for a voice file — unknown extensions ride as m4a (server sniffs content)
    static func voiceDataURL(url: URL, data: Data) -> String? {
        let b64 = data.base64EncodedString()
        guard b64.count < 1_400_000 else { return nil } // worker cap is 1.5M chars
        let mime: String
        switch url.pathExtension.lowercased() {
        case "wav": mime = "audio/wav"
        case "mp3": mime = "audio/mpeg"
        default: mime = "audio/m4a"
        }
        return "data:\(mime);base64," + b64
    }

    static func submit(token: String, prompt: String, quality: String, aspect: String, duration: Int,
                       photos: [UIImage], voiceURL: URL?, refMode: String) async throws -> Job
    {
        var req = URLRequest(url: URL(string: base)!)
        req.httpMethod = "POST"
        req.timeoutInterval = 180 // multi-photo uploads on cellular can be slow
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["prompt": prompt, "quality": quality, "aspect": aspect, "duration": duration]
        let images = photos.compactMap { API.imageDataURL($0) }
        if !images.isEmpty {
            body["images"] = images
        }
        if refMode == "identity" {
            body["ref_mode"] = refMode
        }
        if let vu = voiceURL, let d = try? Data(contentsOf: vu) {
            if let v = voiceDataURL(url: vu, data: d) {
                body["voice"] = v
            }
        }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard Self.ok(resp) else {
            if let h = resp as? HTTPURLResponse, h.statusCode == 429,
               let e = try? JSONDecoder().decode([String: String].self, from: data), let m = e["error"]
            {
                throw URLError(.resourceUnavailable, userInfo: [NSLocalizedDescriptionKey: m])
            }
            let status = (resp as? HTTPURLResponse)?.statusCode
            let body = String(data: data.prefix(200), encoding: .utf8) ?? ""
            throw SubmitError(status: status, detail: body)
        }
        return try JSONDecoder().decode(Job.self, from: data)
    }

    // videos need the token header → download to a temp file for AVPlayer
    static var videoCache: [String: URL] = [:]
    static func videoFile(token: String, id: String, remote: String) async throws -> URL {
        if let u = videoCache[id], FileManager.default.fileExists(atPath: u.path) {
            return u
        }
        var req = URLRequest(url: URL(string: "https://YOUR-BACKEND.example.com\(remote)")!)
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw SubmitError(status: (resp as? HTTPURLResponse)?.statusCode ?? -1, detail: "video download failed")
        }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("snora_\(id).mp4")
        try data.write(to: url, options: .atomic)
        videoCache[id] = url
        return url
    }

    /// first frame of a downloaded clip, for the reel posters.
    /// NOTE: the simulator cannot decode video (OSStatus -17913) — verify on a real device.
    static func firstFrame(of url: URL) async throws -> UIImage {
        let asset = AVURLAsset(url: url)
        let gen = AVAssetImageGenerator(asset: asset)
        gen.appliesPreferredTrackTransform = true
        gen.maximumSize = CGSize(width: 900, height: 900)
        let entry = try await gen.image(at: CMTime(seconds: 0.5, preferredTimescale: 600))
        // UIImage(cgImage:) — drawing entry.image into a UIGraphicsImageRenderer
        // would land it upside down (that context is y-flipped for UIKit)
        return UIImage(cgImage: entry.image)
    }

    static func cancel(token: String, id: String) async throws {
        var req = URLRequest(url: URL(string: "\(base)/cancel")!)
        req.httpMethod = "POST"
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["id": id])
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let h = resp as? HTTPURLResponse else {
            throw SubmitError(status: nil, detail: "no response from the farm")
        }
        guard (200 ..< 300).contains(h.statusCode) else {
            // e.g. "cannot cancel a rendering job" — the honest reason, on screen
            let detail = String(data: data.prefix(160), encoding: .utf8) ?? "empty reply"
            throw SubmitError(status: h.statusCode, detail: detail)
        }
    }

    /// the farm's own words from an error body — prefer the `error` field
    /// the farm writes, fall back to the raw reply.
    static func farmDetail(_ data: Data) -> String {
        if let e = try? JSONDecoder().decode([String: String].self, from: data),
           let m = e["error"] {
            return m
        }
        return String(data: data.prefix(200), encoding: .utf8) ?? "empty reply"
    }

    /// the list of reference pictures a job was drawn from (names + sizes;
    /// the picture bytes themselves come one at a time from refImage below).
    static func refs(token: String, id: String) async throws -> [RefInfo] {
        var req = URLRequest(url: URL(string: "\(base)/refs/\(id)")!)
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard Self.ok(resp) else {
            throw SubmitError(status: (resp as? HTTPURLResponse)?.statusCode, detail: farmDetail(data))
        }
        return try JSONDecoder().decode(RefList.self, from: data).refs
    }

    /// one reference picture's bytes, cached — a card can flip open twice
    /// without the farm sending the photo twice.
    static var refImageCache: [String: Data] = [:]

    static func refImage(token: String, id: String, i: Int) async throws -> Data {
        let key = "\(id)/\(i)"
        if let d = refImageCache[key] { return d }
        var req = URLRequest(url: URL(string: "\(base)/ref/\(id)/\(i)")!)
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard Self.ok(resp) else {
            throw SubmitError(status: (resp as? HTTPURLResponse)?.statusCode, detail: farmDetail(data))
        }
        refImageCache[key] = data
        return data
    }

    /// pull every waiting job out of line — the one being drawn keeps going.
    /// Returns the job ids that came out, so the screen can report the count.
    static func clearQueue(token: String) async throws -> [String] {
        var req = URLRequest(url: URL(string: "\(base)/clear_queue")!)
        req.httpMethod = "POST"
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard Self.ok(resp) else {
            throw SubmitError(status: (resp as? HTTPURLResponse)?.statusCode, detail: farmDetail(data))
        }
        struct Cleared: Codable { let removed: [String] }
        return try JSONDecoder().decode(Cleared.self, from: data).removed
    }

    static func dismiss(token: String, id: String) async throws {
        var req = URLRequest(url: URL(string: "\(base)/dismiss")!)
        req.httpMethod = "POST"
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["id": id])
        _ = try await URLSession.shared.data(for: req)
    }

    /// permanent delete — the farm drops its record + the clip on the Mac and
    /// the cloud copy goes with it. Surfaces a real reason on failure.
    static func delete(token: String, id: String) async throws {
        var req = URLRequest(url: URL(string: "\(base)/delete")!)
        req.httpMethod = "POST"
        req.setValue(token, forHTTPHeaderField: "X-Sora-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["id": id])
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let h = resp as? HTTPURLResponse else {
            throw SubmitError(status: nil, detail: "no response from the farm")
        }
        guard (200 ..< 300).contains(h.statusCode) else {
            let detail = String(data: data.prefix(160), encoding: .utf8) ?? "empty reply"
            throw SubmitError(status: h.statusCode, detail: detail)
        }
    }
}

import BackgroundTasks

// ── polling + notifications ───────────────────────────────────────────
import UserNotifications

final class Poller: ObservableObject {
    static let shared = Poller()
    static let refreshId = "com.snail3d.snora.refresh"
    @Published var lastSeenDone: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "seenDone") ?? [])
    private var timer: Timer?

    func start(token: String) {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { _ in
            Task { await self.check(token: token) }
        }
    }

    func stop() {
        timer?.invalidate(); timer = nil
    }

    @MainActor func check(token: String) async {
        guard let jobs = try? await API.jobs(token: token) else { return }
        let fresh = jobs.filter { $0.isDone && !lastSeenDone.contains($0.id) }
        for j in fresh.prefix(4) {
            let n = UNMutableNotificationContent()
            n.title = "render done"
            n.body = String(j.prompt.prefix(80))
            n.sound = .default
            try? await UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: j.id, content: n, trigger: nil)
            )
        }
        if !fresh.isEmpty {
            lastSeenDone.formUnion(fresh.map(\.id))
            UserDefaults.standard.set(Array(lastSeenDone.suffix(200)), forKey: "seenDone")
        }
        // drop seen-ids for jobs that got dismissed/failed
        let doneIds = Set(jobs.filter(\.isDone).map(\.id))
        lastSeenDone = lastSeenDone.intersection(doneIds)
    }

    func scheduleRefresh() {
        // register MUST come before submit or BGTaskScheduler aborts the app
        BGTaskScheduler.shared.register(forTaskWithIdentifier: Self.refreshId, using: nil) { task in
            let token = API.token
            Task { await Poller.shared.check(token: token); task.setTaskCompleted(success: true) }
            Poller.shared.scheduleRefresh()
        }
        let req = BGAppRefreshTaskRequest(identifier: Self.refreshId)
        req.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(req)
    }
}
