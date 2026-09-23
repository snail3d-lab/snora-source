import Foundation
import UIKit

/// one generation result from SnailStudio: {ok, image:"/generated/x.png", width, height}
struct StudioGenResult: Codable {
    let image: String
    let width: Int?
    let height: Int?
}

/// the real reason a studio call failed — surfaced in the UI, same honesty as SubmitError
struct StudioError: LocalizedError {
    let status: Int?
    let detail: String
    var errorDescription: String? {
        if let s = status {
            return detail.isEmpty ? "studio said HTTP \(s)" : "studio said HTTP \(s) — \(detail)"
        }
        return detail.isEmpty ? "the studio didn't answer" : detail
    }
}

/// SnailStudio on the user's Mac, direct over Tailscale (no auth). Photos take minutes
/// by design — long timeouts, typed errors, never a crash.
enum StudioAPI {
    /// the Mac's tailnet address (verified 2026-09-09). The simulator shares the
    /// Mac's loopback, so `http://127.0.0.1:7778` works there too.
    static let defaultHost = "http://127.0.0.1:7778"

    /// precedence: STUDIO_HOST launch-arg/env seam (sim tests) → saved setting → baked default
    static var host: String {
        if let env = ProcessInfo.processInfo.environment["STUDIO_HOST"], !env.isEmpty { return env }
        if let saved = UserDefaults.standard.string(forKey: "studio_host"), !saved.isEmpty {
            return saved.hasSuffix("/") ? String(saved.dropLast()) : saved
        }
        return defaultHost
    }

    /// canvas for an aspect word — matches the studio's own defaults
    static func size(for aspect: String) -> (w: Int, h: Int) {
        switch aspect {
        case "portrait": return (576, 1024)
        case "square": return (1024, 1024)
        default: return (1024, 576)
        }
    }

    /// cheap liveness probe for the settings TEST button — any HTTP 2xx = studio up
    static func ping() async throws {
        var req = URLRequest(url: URL(string: "\(host)/")!)
        req.timeoutInterval = 8
        let (_, resp) = try await URLSession.shared.data(for: req)
        guard let h = resp as? HTTPURLResponse, (200 ..< 300).contains(h.statusCode) else {
            throw StudioError(status: (resp as? HTTPURLResponse)?.statusCode, detail: "the studio root didn't answer")
        }
    }

    /// ERNIE-Image: text→photo. Synchronous on the server — minutes. 300s timeout.
    static func generateErnie(prompt: String, aspect: String = "wide",
                               steps: Int = 8, seed: Int = -1) async throws -> StudioGenResult
    {
        let s = size(for: aspect)
        return try await post("generate-ernie", body: [
            "prompt": prompt, "width": s.w, "height": s.h, "steps": steps, "seed": seed,
        ], timeout: 300)
    }

    /// Flux: text→photo via draw-things-cli broll.
    static func generateFlux(prompt: String, aspect: String = "wide",
                             steps: Int = 20, seed: Int = -1) async throws -> StudioGenResult
    {
        let s = size(for: aspect)
        return try await post("generate-broll", body: [
            "prompt": prompt, "width": s.w, "height": s.h, "steps": steps, "seed": seed,
        ], timeout: 180)
    }

    /// Flux img2img: reference photo + prompt. The studio decodes the data URL to a
    /// temp PNG and passes `--image <path> --strength <0..1>` to draw-things-cli.
    static func generateFluxImg2Img(image: UIImage, prompt: String, strength: Double = 0.35,
                                     aspect: String = "wide",
                                     steps: Int = 20, seed: Int = -1) async throws -> StudioGenResult
    {
        let s = size(for: aspect)
        guard let png = image.pngData() else {
            throw StudioError(status: nil, detail: "couldn't encode the reference photo")
        }
        return try await post("generate-broll", body: [
            "prompt": prompt, "width": s.w, "height": s.h, "steps": steps, "seed": seed,
            "image": "data:image/png;base64," + png.base64EncodedString(),
            "strength": strength,
        ], timeout: 180)
    }

    /// Qwen-Image-2.1 canvases — the official 2K ratios, scaled by quality
    /// (fast 0.5, standard 0.75, best 1.0) and snapped to /32.
    static func qwenSize(for aspect: String, quality: String = "standard") -> (w: Int, h: Int) {
        let base: (Int, Int)
        switch aspect {
        case "portrait": base = (1536, 2752)   // 9:16
        case "square":   base = (2048, 2048)   // 1:1
        default:         base = (2752, 1536)   // 16:9
        }
        let scale: Double = quality == "fast" ? 0.5 : (quality == "best" ? 1.0 : 0.75)
        func snap(_ v: Int) -> Int { max(512, Int((Double(v) * scale / 32).rounded()) * 32) }
        return (snap(base.0), snap(base.1))
    }

    /// Qwen-Image-2.1: text→image, image editing, and multi-reference creation
    /// (up to 10 photos), plus native transparent RGBA. Minutes per image at 2K,
    /// so the timeout is generous.
    static func generateQwen(prompt: String, aspect: String = "square",
                             quality: String = "standard", steps: Int = 40, seed: Int = -1,
                             transparent: Bool = false,
                             references: [UIImage] = []) async throws -> StudioGenResult
    {
        let s = qwenSize(for: aspect, quality: quality)
        var body: [String: Any] = [
            "prompt": prompt, "width": s.w, "height": s.h,
            "steps": steps, "seed": seed, "transparent": transparent,
        ]
        if !references.isEmpty {
            var urls: [String] = []
            for img in references.prefix(10) {
                guard let png = img.pngData() else {
                    throw StudioError(status: nil, detail: "couldn't encode a reference photo")
                }
                urls.append("data:image/png;base64," + png.base64EncodedString())
            }
            body["images"] = urls
        }
        return try await post("generate-qwen", body: body, timeout: 900)
    }

    /// Qwen edit sized to the input picture's own aspect, so the frame is kept.
    static func qwenEditSize(for image: UIImage, target: Int = 1536) -> (w: Int, h: Int) {
        let ar = image.size.width / max(1, image.size.height)
        let w: Int, h: Int
        if ar >= 1 { w = target; h = Int((Double(target) / Double(ar)).rounded()) }
        else       { h = target; w = Int((Double(target) * Double(ar)).rounded()) }
        func snap(_ v: Int) -> Int { max(512, Int((Double(v) / 32).rounded()) * 32) }
        return (snap(w), snap(h))
    }

    /// Qwen-Image-2.1 edit: the annotated picture is the single reference and the
    /// canvas follows its aspect (a circle drawn on the photo, described in words).
    static func generateQwenEdit(image: UIImage, prompt: String,
                                 steps: Int = 40, seed: Int = -1) async throws -> StudioGenResult
    {
        let s = qwenEditSize(for: image)
        guard let png = image.pngData() else {
            throw StudioError(status: nil, detail: "couldn't encode the picture")
        }
        return try await post("generate-qwen", body: [
            "prompt": prompt, "width": s.w, "height": s.h, "steps": steps, "seed": seed,
            "transparent": false,
            "images": ["data:image/png;base64," + png.base64EncodedString()],
        ], timeout: 900)
    }

    /// GET {host}/generated/<name> → UIImage (the studio serves these as static files)
    static func fetchImage(path: String) async throws -> UIImage {
        var p = path
        if !p.hasPrefix("/") { p = "/" + p }
        guard let url = URL(string: host + p) else {
            throw StudioError(status: nil, detail: "bad image path")
        }
        var req = URLRequest(url: url, timeoutInterval: 60)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw StudioError(status: (resp as? HTTPURLResponse)?.statusCode, detail: "image download failed")
        }
        guard let img = UIImage(data: data) else {
            throw StudioError(status: nil, detail: "the studio sent something that isn't an image")
        }
        return img
    }

    /// POST {host}/api/editor/ai/<action> with JSON; errors come back typed
    static func post(_ action: String, body: [String: Any], timeout: TimeInterval) async throws -> StudioGenResult {
        guard let url = URL(string: "\(host)/api/editor/ai/\(action)") else {
            throw StudioError(status: nil, detail: "bad studio host setting")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = timeout
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.data(for: req)
        if let h = resp as? HTTPURLResponse, !(200 ..< 300).contains(h.statusCode) {
            let err = (try? JSONDecoder().decode([String: String].self, from: data))?["error"]
                ?? String(data: data.prefix(200), encoding: .utf8) ?? ""
            throw StudioError(status: h.statusCode, detail: err)
        }
        do {
            return try JSONDecoder().decode(StudioGenResult.self, from: data)
        } catch {
            throw StudioError(status: (resp as? HTTPURLResponse)?.statusCode, detail: "the studio's reply wasn't understood")
        }
    }
}