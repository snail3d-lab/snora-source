import SwiftUI
import UIKit

/// ── the Photos tab: everything the studio (or AI assist) generated ────
struct PhotosView: View {
    @ObservedObject private var store = PhotoStore.shared
    @State private var showSettings = false
    @State private var selected: PhotoRecord?

    private let cols = [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)]

    var body: some View {
        NavigationStack {
            ZStack {
                SnoraBackground()
                if store.photos.isEmpty { emptyState } else { grid }
            }
            .navigationTitle("Photos")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: {
                        Image(systemName: "gearshape").foregroundStyle(Theme.sand)
                    }
                }
            }
            .refreshable { await store.syncMissing() }
            .sheet(item: $selected) { rec in PhotoDetailSheet(record: rec) }
            .sheet(isPresented: $showSettings) { SettingsSheet() }
        }
        .preferredColorScheme(.dark)
    }

    private var grid: some View {
        ScrollView {
            LazyVGrid(columns: cols, spacing: 12) {
                ForEach(store.photos) { rec in
                    PhotoCell(record: rec)
                        .contentShape(Rectangle())
                        .onTapGesture { selected = rec }
                }
            }
            .padding(16)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 18) {
            PlayBadge(size: 78)
            Text("no photos yet")
                .font(.title3.weight(.semibold))
                .foregroundStyle(Theme.cream)
            Text("ask AI assist on the create tab to make one")
                .font(.snoraLabel)
                .foregroundStyle(Theme.sand)
                .kerning(0.6)
                .textCase(.uppercase)
                .multilineTextAlignment(.center)
        }
        .padding()
    }
}

/// one grid cell: thumb + prompt snippet + engine tag + relative time
private struct PhotoCell: View {
    let record: PhotoRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            LocalPhotoThumb(record: record)
                .frame(height: 140)
                .frame(maxWidth: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))
            Text(record.prompt)
                .font(.caption)
                .foregroundStyle(Theme.cream)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 5) {
                MetaTag(text: record.engine)
                Spacer(minLength: 0)
                Text(relativeTime(record.createdAt))
                    .font(.snoraLabel)
                    .foregroundStyle(Theme.sand.opacity(0.85))
            }
        }
        .padding(10)
        .snoraCard()
    }
}

/// thumbnail from the local file; auto-fetches from the studio when missing
struct LocalPhotoThumb: View {
    let record: PhotoRecord
    @ObservedObject private var store = PhotoStore.shared
    @State private var img: UIImage?
    @State private var failed = false

    /// re-run the load when the file's presence flips (pull-to-refresh re-download)
    private var key: String {
        record.id.uuidString + (store.hasLocal(record) ? "1" : "0")
    }

    var body: some View {
        ZStack {
            Theme.bgBottom.opacity(0.45)
            if let img {
                Image(uiImage: img).resizable().scaledToFill()
            } else if failed {
                Image(systemName: "photo.badge.exclamationmark")
                    .font(.title2)
                    .foregroundStyle(Theme.sand.opacity(0.5))
            } else {
                ProgressView().tint(Theme.signal.opacity(0.8))
            }
        }
        .task(id: key) {
            if img == nil, let fetched = await store.imageWithRemoteFallback(record) {
                img = fetched
            } else if img == nil {
                failed = true
            }
        }
    }
}

/// ── tap a photo: full image + USE FOR VIDEO / SAVE / DELETE ────────────
struct PhotoDetailSheet: View {
    let record: PhotoRecord
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var store = PhotoStore.shared
    @ObservedObject private var bridge = PhotoBridge.shared
    @State private var img: UIImage?
    @State private var note = ""
    @State private var showEdit = false

    var body: some View {
        ZStack {
            SnoraBackground()
            VStack(spacing: 14) {
                HStack {
                    Button { dismiss() } label: {
                        Text("CLOSE")
                            .font(.snoraTag).kerning(1.2)
                            .foregroundStyle(Theme.cream)
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .background(Capsule().fill(Theme.signal.opacity(0.85)))
                    }
                    Spacer()
                    MetaTag(text: record.engine)
                }
                .padding(.horizontal)
                Group {
                    if let img {
                        Image(uiImage: img).resizable().scaledToFit()
                    } else {
                        ProgressView().tint(Theme.signal).frame(minHeight: 220)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.cardStroke, lineWidth: 1))
                .padding(.horizontal)
                Text(record.prompt)
                    .font(.footnote)
                    .foregroundStyle(Theme.sand)
                    .lineLimit(4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                Button {
                    showEdit = true
                } label: {
                    Label("CHANGE IT", systemImage: "wand.and.stars")
                        .kerning(1.2)
                        .font(.system(.body, design: .monospaced).weight(.bold))
                }
                .buttonStyle(SignalButtonStyle())
                .disabled(img == nil)
                .padding(.horizontal)
                Button {
                    bridge.pendingReference = img   // RootView switches to Create
                    dismiss()
                } label: {
                    Label("USE FOR VIDEO", systemImage: "arrow.triangle.2.circlepath")
                        .kerning(1.2)
                        .font(.system(.body, design: .monospaced).weight(.bold))
                }
                .buttonStyle(GhostButtonStyle())
                .disabled(img == nil)
                .padding(.horizontal)
                HStack(spacing: 10) {
                    Button { saveToLibrary() } label: {
                        Label("save to photos", systemImage: "square.and.arrow.down")
                    }
                    .buttonStyle(GhostButtonStyle())
                    .disabled(img == nil)
                    Button { store.delete(record); dismiss() } label: {
                        Label("delete", systemImage: "trash")
                            .foregroundStyle(Theme.ember)
                    }
                    .buttonStyle(GhostButtonStyle())
                }
                .padding(.horizontal)
                if !note.isEmpty {
                    Text(note).font(.footnote).foregroundStyle(Theme.sand)
                }
                Spacer()
            }
            .padding(.top, 8)
        }
        .task(id: record.id) { img = await store.imageWithRemoteFallback(record) }
        .sheet(isPresented: $showEdit) {
            if let img { PhotoEditView(initialImage: img, sourceRecord: record) }
        }
    }

    private func saveToLibrary() {
        guard let img else { return }
        UIImageWriteToSavedPhotosAlbum(img, nil, nil, nil)
        note = "saved to your photo library"
    }
}

/// ── settings: studio host + assist model, with a live TEST button ──────
private struct SettingsSheet: View {
    @Environment(\.dismiss) private var dismiss
    @AppStorage("studio_host") private var studioHost = StudioAPI.defaultHost
    @AppStorage("assist_model") private var assistModel = "snail-top"
    @State private var testResult = ""
    @State private var testing = false

    var body: some View {
        NavigationStack {
            ZStack {
                SnoraBackground()
                ScrollView {
                    VStack(spacing: 14) {
                        SectionCard(title: "studio host",
                                    footnote: "SnailStudio on your Mac, over Tailscale. Simulator tests use http://127.0.0.1:7778") {
                            field(text: $studioHost, placeholder: "http://127.0.0.1:7778")
                            Button { testStudio() } label: {
                                HStack(spacing: 8) {
                                    if testing {
                                        ProgressView().tint(Theme.cream).scaleEffect(0.8)
                                    } else {
                                        Image(systemName: "dot.radiowaves.left.and.right")
                                    }
                                    Text("TEST CONNECTION").kerning(1.0)
                                }
                            }
                            .buttonStyle(GhostButtonStyle())
                            .disabled(testing || studioHost.trimmingCharacters(in: .whitespaces).isEmpty)
                            if !testResult.isEmpty {
                                Text(testResult)
                                    .font(.footnote)
                                    .foregroundStyle(testResult.hasPrefix("ok") ? Theme.crt : Theme.ember)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                        SectionCard(title: "assist model",
                                    footnote: "which snail-api slot plans your AI assist jobs") {
                            field(text: $assistModel, placeholder: "snail-top")
                        }
                    }
                    .padding(16)
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: {
                        Text("DONE").font(.snoraTag).foregroundStyle(Theme.cream)
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
    }

    private func field(text: Binding<String>, placeholder: String) -> some View {
        TextField(placeholder, text: text)
            .keyboardType(.URL)
            .autocapitalization(.none)
            .autocorrectionDisabled()
            .foregroundStyle(Theme.cream)
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.bgBottom.opacity(0.6)))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.cardStroke, lineWidth: 1))
    }

    private func testStudio() {
        testing = true
        testResult = ""
        Task {
            do {
                try await StudioAPI.ping()
                testResult = "ok — the studio answered"
            } catch {
                testResult = "no — \(error.localizedDescription)"
            }
            testing = false
        }
    }
}

/// "3 min. ago" style stamp for the grid
func relativeTime(_ date: Date) -> String {
    let f = RelativeDateTimeFormatter()
    f.unitsStyle = .abbreviated
    return f.localizedString(for: date, relativeTo: Date())
}