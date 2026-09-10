import Foundation
import UIKit

/// one generated photo: its studio path, its prompt, and its local file
struct PhotoRecord: Identifiable, Codable {
    let id: UUID
    let remotePath: String      // e.g. "/generated/ernie_1699.png"
    let prompt: String
    let engine: String          // ERNIE | FLUX | IMG2IMG
    let createdAt: Date
    let localFilename: String   // inside Documents/
}

/// the Photos tab's local persistence: Documents/studio_photos.json + image files.
/// The studio keeps its own copy; DELETE here is local-record-only by design.
@MainActor
final class PhotoStore: ObservableObject {
    static let shared = PhotoStore()

    @Published private(set) var photos: [PhotoRecord] = []   // newest first

    private let dir: URL
    private var file: URL { dir.appendingPathComponent("studio_photos.json") }
    private let cache = NSCache<NSString, UIImage>()

    init() {
        dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        load()
    }

    func localURL(_ r: PhotoRecord) -> URL {
        dir.appendingPathComponent(r.localFilename)
    }

    func hasLocal(_ r: PhotoRecord) -> Bool {
        FileManager.default.fileExists(atPath: localURL(r).path)
    }

    /// register a freshly generated studio photo (already downloaded as UIImage)
    @discardableResult
    func add(remotePath: String, prompt: String, engine: String, image: UIImage) -> PhotoRecord? {
        guard let data = image.pngData() else { return nil }
        let id = UUID()
        let name = "photo_\(id.uuidString).png"
        do {
            try data.write(to: dir.appendingPathComponent(name), options: .atomic)
        } catch {
            return nil
        }
        let rec = PhotoRecord(id: id, remotePath: remotePath, prompt: prompt,
                              engine: engine, createdAt: Date(), localFilename: name)
        photos.insert(rec, at: 0)
        cache.setObject(image, forKey: id.uuidString as NSString)
        save()
        return rec
    }

    /// local record only — the studio copy stays on the Mac
    func delete(_ r: PhotoRecord) {
        photos.removeAll { $0.id == r.id }
        try? FileManager.default.removeItem(at: localURL(r))
        cache.removeObject(forKey: r.id.uuidString as NSString)
        save()
    }

    /// pull-to-refresh: re-download any local file that went missing
    @discardableResult
    func syncMissing() async -> Int {
        var fetched = 0
        for r in photos where !hasLocal(r) {
            if (try? await fetchAndStore(r)) != nil { fetched += 1 }
        }
        objectWillChange.send()
        return fetched
    }

    /// local file if present, else fetch from the studio and keep a copy
    func imageWithRemoteFallback(_ r: PhotoRecord) async -> UIImage? {
        (try? await fetchAndStore(r)) ?? nil
    }

    // ── internals ──────────────────────────────────────────────────────

    private func fetchAndStore(_ r: PhotoRecord) async throws -> UIImage {
        if let local = cached(r) { return local }
        let img = try await StudioAPI.fetchImage(path: r.remotePath)
        guard let data = img.pngData() else {
            throw StudioError(status: nil, detail: "couldn't store the photo")
        }
        try data.write(to: localURL(r), options: .atomic)
        cache.setObject(img, forKey: r.id.uuidString as NSString)
        return img
    }

    private func cached(_ r: PhotoRecord) -> UIImage? {
        if let c = cache.object(forKey: r.id.uuidString as NSString) { return c }
        guard let d = try? Data(contentsOf: localURL(r)), let img = UIImage(data: d) else { return nil }
        cache.setObject(img, forKey: r.id.uuidString as NSString)
        return img
    }

    private func load() {
        guard let data = try? Data(contentsOf: file),
              let list = try? JSONDecoder().decode([PhotoRecord].self, from: data) else { return }
        photos = list
    }

    private func save() {
        if let data = try? JSONEncoder().encode(photos) {
            try? data.write(to: file, options: .atomic)
        }
    }
}